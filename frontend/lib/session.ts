/**
 * The BFF session: an opaque id in a cookie, the tokens on the server (#18, ADR-0013 §1).
 *
 * **Why the cookie does not contain the token.** It is tempting — it is stateless and it survives a
 * restart. But a browser caps a cookie at roughly 4 KB and *silently drops* an oversized
 * `Set-Cookie`: no error, no warning. A Keycloak realm that puts a few roles and groups in its
 * access token, plus a refresh token and an ID token, crosses that line easily. The failure is a
 * server-side login that "succeeds" and then an invisible redirect loop, on that tenant only, in
 * production only. Storing an opaque id removes the whole class: the cookie is a fixed ~36 bytes
 * whatever the IdP emits.
 *
 * **The store is an in-process `Map` pinned on `globalThis`.** Pinning matters for two reasons: it
 * survives dev hot-reload, and it guarantees one instance even if Next compiles the route handlers
 * and the proxy into separate bundles. The honest limits: sessions do not survive a frontend
 * restart, and a second frontend container would not share them. Both degrade to one extra
 * redirect through a still-valid IdP SSO session, not a broken app. Swapping in Redis is one file
 * behind this interface — which is why the interface, not the `Map`, is what the rest of the app
 * touches.
 *
 * **This file must never import `next/headers`.** `cookies()` throws outside a request scope, which
 * makes every importer untestable under vitest, and the only escape is module patching — which
 * ADR-0013 §5 rules out on purpose. Route handlers take a `NextRequest` and are plain functions of
 * it. The one Server Component seam lives in `lib/session-server.ts` and is two lines long.
 */

import type { NextRequest } from "next/server";

import { cookieNames, cookieSecure } from "@/lib/config";

/** Eight hours: a working day, after which a re-login through the IdP is not an imposition. */
export const SESSION_TTL_MS = 8 * 60 * 60 * 1000;

/** How early (in seconds) to refresh before the access token actually expires. */
export const REFRESH_SKEW_SECONDS = 30;

export type SessionRecord = {
  accessToken: string;
  refreshToken?: string;
  idToken?: string;
  /** Absolute expiry of `accessToken`, epoch **seconds** (matching OIDC's `expires_in` units). */
  expiresAt: number;
  /** Cached from the discovery document at login, so logout works even if the IdP is unreachable. */
  tokenEndpoint: string;
  endSessionEndpoint?: string;
  issuer: string;
  clientId: string;
  tenantSlug: string;
  createdAt: number;
};

type Store = Map<string, SessionRecord>;

const globalStore = globalThis as typeof globalThis & { __tiqSessions?: Store };

function store(): Store {
  return (globalStore.__tiqSessions ??= new Map());
}

export function createSession(record: SessionRecord): string {
  const id = crypto.randomUUID();
  store().set(id, record);
  return id;
}

export function getSession(id: string | undefined): SessionRecord | undefined {
  if (!id) return undefined;
  const record = store().get(id);
  if (!record) return undefined;
  // Lazy expiry: an absolute cap (rather than only the access token's expiry) bounds how long a
  // stolen id stays useful.
  if (Date.now() - record.createdAt > SESSION_TTL_MS) {
    store().delete(id);
    return undefined;
  }
  sweepExpired();
  return record;
}

/**
 * Drop every record past its absolute lifetime.
 *
 * Checking expiry only on lookup is not enough on its own: a session whose cookie the user simply
 * discards — closing the browser, clearing cookies, or any login that replaces the cookie without
 * ending the old session — is never looked up again, so its record (holding a **live refresh token**)
 * would sit in the map until the process restarts. Sweeping on access keeps the map proportional to
 * live sessions without a timer, which matters because a timer would keep the process alive.
 *
 * Amortised: the map holds one small object per active session, so a full pass is cheap at this
 * scale. If it ever is not, that is the point at which the store belongs in Redis with a TTL.
 */
function sweepExpired(now = Date.now()): void {
  for (const [id, record] of store()) {
    if (now - record.createdAt > SESSION_TTL_MS) store().delete(id);
  }
}

export function updateSession(id: string, patch: Partial<SessionRecord>): void {
  const record = store().get(id);
  if (record) store().set(id, { ...record, ...patch });
}

export function deleteSession(id: string | undefined): void {
  if (id) store().delete(id);
}

/** Test seam: drop every session so one test cannot observe another's state. */
export function resetSessionsForTest(): void {
  store().clear();
}

/** True when the access token is expired, or close enough that a call would race the expiry. */
export function needsRefresh(record: SessionRecord, now = Date.now()): boolean {
  return Math.floor(now / 1000) >= record.expiresAt - REFRESH_SKEW_SECONDS;
}

type CookieOptions = {
  httpOnly: boolean;
  secure: boolean;
  sameSite: "lax";
  path: string;
  maxAge: number;
};

/**
 * `SameSite=Lax` is written **explicitly**, never left to the browser's default: Chrome defaults to
 * Lax and Firefox does not, so an omitted attribute is a silent cross-browser difference in exactly
 * the control that blocks cross-site POSTs. `Lax` rather than `Strict` because the IdP returns the
 * user via a top-level navigation that must arrive already carrying the session.
 */
export function sessionCookieOptions(): CookieOptions {
  return {
    httpOnly: true,
    secure: cookieSecure(),
    sameSite: "lax",
    path: "/",
    maxAge: Math.floor(SESSION_TTL_MS / 1000),
  };
}

/** Same flags, but readable by script — the client half of the double-submit pair must be. */
export function csrfCookieOptions(): CookieOptions {
  return { ...sessionCookieOptions(), httpOnly: false };
}

/** The short-lived login-transaction cookie: httpOnly, and gone in ten minutes either way. */
export function txCookieOptions(): CookieOptions {
  return { ...sessionCookieOptions(), maxAge: 600 };
}

export function readSessionId(request: NextRequest): string | undefined {
  return request.cookies.get(cookieNames().session)?.value;
}

/** The bit of `NextResponse` this module needs, so the helper stays trivially testable. */
type CookieWriter = { cookies: { set: (name: string, value: string, options: object) => unknown } };

/**
 * Expire a cookie.
 *
 * **Never use `response.cookies.delete(name)`.** Next's `ResponseCookies.delete` is
 * `set({...options, name, value: "", expires: new Date(0)})` — and when the first argument is a
 * string it drops the options argument entirely, so there is not even an escape hatch. The header it
 * emits carries no `Secure`, and RFC 6265bis §4.1.3.2 requires a browser to **ignore** a
 * `Set-Cookie` for a `__Host-`-prefixed name that lacks it.
 *
 * The result is a deletion that silently does nothing in exactly the deployment where the prefix is
 * applied — https — while every local test passes, because tests run on plain http where no prefix
 * is used. Sign-out would leave the session cookie in the jar for its full eight hours.
 *
 * Re-setting the cookie with its real options plus a zero lifetime keeps `Secure` (and `HttpOnly`,
 * `SameSite`, `Path`) on the clearing header, which is what makes the browser accept it.
 */
export function clearCookie(
  response: CookieWriter,
  name: string,
  options: CookieOptions = sessionCookieOptions(),
): void {
  response.cookies.set(name, "", { ...options, maxAge: 0, expires: new Date(0) });
}
