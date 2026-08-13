/**
 * Finish the OIDC login: `GET /api/auth/callback?code&state` (#18, ADR-0013 §1).
 *
 * Two invariants shape this handler:
 *
 * 1. **No PKCE verifier outlives the attempt that created it.** Every exit path that *owns* the
 *    transaction clears it — success, junk cookie, failed exchange, mismatched claims. The single
 *    exception is a **state mismatch**, where the cookie belongs to a different attempt: tabs share
 *    one cookie name, so a second login overwrites the first tab's transaction, and clearing it on
 *    the stale callback would break the live tab as well. Both attempts would then fail, which is
 *    precisely what the original "always clear" rule caused.
 * 2. **Every failure looks the same**: `303 -> /login?error=retry`. A state mismatch must be
 *    indistinguishable from any other failure, and the uniform bounce means the losing tab lands
 *    back on the login form where one click succeeds.
 */

import { NextResponse, type NextRequest } from "next/server";

import { appBaseUrl, cookieNames } from "@/lib/config";
import { mintCsrfToken } from "@/lib/csrf";
import {
  assertIdTokenMatches,
  discoverTenant,
  exchangeCode,
  fetchOpenIdConfiguration,
} from "@/lib/oidc";
import {
  clearCookie,
  createSession,
  csrfCookieOptions,
  sessionCookieOptions,
  txCookieOptions,
} from "@/lib/session";

type Transaction = { state: string; nonce: string; verifier: string; slug: string };

function parseTransaction(raw: string | undefined): Transaction | null {
  if (!raw) return null;
  try {
    const value = JSON.parse(raw) as Partial<Transaction>;
    if (!value.state || !value.nonce || !value.verifier || !value.slug) return null;
    return value as Transaction;
  } catch {
    return null;
  }
}

/**
 * Uniform failure: bounce to the login form.
 *
 * `clearTx` is not always true, and the exception matters. Tabs share one cookie name, so a second
 * login overwrites the first tab's transaction. When a stale callback then arrives with a state that
 * does not match, the cookie it finds belongs to a **live** attempt in another tab — deleting it
 * would break that one too, so both tabs would fail. On a state mismatch we leave the cookie alone
 * and let the live attempt finish; the stale one expires on its own within ten minutes.
 */
function retry(request: NextRequest, { clearTx }: { clearTx: boolean }): NextResponse {
  // Built from `appBaseUrl()`, never `request.url` (#81). Under compose the server is bound with
  // `-H 0.0.0.0` and Next takes `request.url`'s host from the bound address, not the Host header, so
  // `new URL(path, request.url)` sends the browser to `http://0.0.0.0:3000` — a *different origin*.
  // The session cookie is host-scoped, so it is not sent on the follow-up request and a sign-in that
  // just succeeded lands back on this form. `appBaseUrl()` is by definition the origin the browser
  // is on; the OIDC redirect_uri already used it, which is why only redirects *into our own app*
  // were broken.
  const response = NextResponse.redirect(new URL("/login?error=retry", appBaseUrl()), 303);
  if (clearTx) clearCookie(response, cookieNames().tx, txCookieOptions());
  return response;
}

export async function GET(request: NextRequest): Promise<NextResponse> {
  const tx = parseTransaction(request.cookies.get(cookieNames().tx)?.value);
  const code = request.nextUrl.searchParams.get("code");
  const state = request.nextUrl.searchParams.get("state");

  // A missing or unparseable cookie is junk worth clearing; a *mismatched* one is somebody else's.
  if (!tx) return retry(request, { clearTx: true });
  if (!code || !state || state !== tx.state) return retry(request, { clearTx: false });

  try {
    // Re-derive the issuer and client id from Django rather than trusting the cookie: this is the
    // authoritative answer, and it is the reason a planted transaction cookie cannot redirect the
    // token exchange to an attacker's endpoint.
    const tenant = await discoverTenant(tx.slug);
    if (!tenant) return retry(request, { clearTx: true });

    const provider = await fetchOpenIdConfiguration(tenant.issuer);
    const tokens = await exchangeCode({
      tokenEndpoint: provider.tokenEndpoint,
      clientId: tenant.clientId,
      code,
      redirectUri: `${appBaseUrl()}/api/auth/callback`,
      codeVerifier: tx.verifier,
    });
    assertIdTokenMatches(tokens.idToken, { nonce: tx.nonce, issuer: tenant.issuer });

    const id = createSession({
      accessToken: tokens.accessToken,
      refreshToken: tokens.refreshToken,
      idToken: tokens.idToken,
      expiresAt: Math.floor(Date.now() / 1000) + tokens.expiresIn,
      tokenEndpoint: provider.tokenEndpoint,
      endSessionEndpoint: provider.endSessionEndpoint,
      issuer: tenant.issuer,
      clientId: tenant.clientId,
      tenantSlug: tx.slug,
      createdAt: Date.now(),
    });

    const response = NextResponse.redirect(new URL("/", appBaseUrl()), 303);
    response.cookies.set(cookieNames().session, id, sessionCookieOptions());
    // The readable half of the double-submit pair, minted once per session.
    response.cookies.set(cookieNames().csrf, mintCsrfToken(), csrfCookieOptions());
    clearCookie(response, cookieNames().tx, txCookieOptions());
    return response;
  } catch {
    // Deliberately swallowed: the reason a login failed is not something to render to whoever
    // triggered it, and every failure mode here is already a reason to start over.
    return retry(request, { clearTx: true });
  }
}
