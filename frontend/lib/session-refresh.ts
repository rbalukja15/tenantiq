/**
 * Keeping a session's access token fresh (#18).
 *
 * This lives in its own module for two reasons. It must be reachable from **both** the API proxy and
 * the Server Component render path — the original mistake was refreshing only in the proxy, which
 * meant the one page the app renders never refreshed at all and a reload a few minutes after login
 * bounced the user back to the login form while a perfectly good refresh token sat unused. And it
 * must not import `next/headers`, so it stays directly testable (see `lib/session-server.ts`).
 *
 * It touches only the server-side store, never a cookie, which is what makes it safe to call from a
 * Server Component — an RSC cannot set cookies during render.
 */

import { OidcError, refreshAccessToken } from "@/lib/oidc";
import { deleteSession, needsRefresh, updateSession, type SessionRecord } from "@/lib/session";

export type RefreshOutcome =
  /** Usable token. `refreshed` says whether it was renewed just now. */
  | { status: "ok"; record: SessionRecord; refreshed: boolean }
  /** The session is over: no refresh token, or the provider definitively rejected it. */
  | { status: "expired" }
  /** The provider is unreachable. The session is *not* destroyed — this is retryable. */
  | { status: "unavailable"; record: SessionRecord };

export async function ensureFreshSession(
  id: string,
  record: SessionRecord,
): Promise<RefreshOutcome> {
  if (!needsRefresh(record)) return { status: "ok", record, refreshed: false };
  if (!record.refreshToken) {
    deleteSession(id);
    return { status: "expired" };
  }

  try {
    const refreshed = await refreshAccessToken({
      tokenEndpoint: record.tokenEndpoint,
      clientId: record.clientId,
      refreshToken: record.refreshToken,
    });
    const patch = {
      accessToken: refreshed.accessToken,
      // A provider with refresh-token rotation returns a new one; one without returns nothing and
      // the existing token stays valid. Overwriting with `undefined` would end the session early.
      refreshToken: refreshed.refreshToken ?? record.refreshToken,
      expiresAt: Math.floor(Date.now() / 1000) + refreshed.expiresIn,
    };
    updateSession(id, patch);
    return { status: "ok", record: { ...record, ...patch }, refreshed: true };
  } catch (error) {
    // A dead refresh token ends the session; an unreachable provider must not. Collapsing the two
    // would mean a brief IdP outage silently logs out every user, with no way back except re-login.
    if (error instanceof OidcError && error.retryable) {
      return { status: "unavailable", record };
    }
    deleteSession(id);
    return { status: "expired" };
  }
}
