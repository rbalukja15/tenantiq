/**
 * Finish the OIDC login: `GET /api/auth/callback?code&state` (#18, ADR-0013 §1).
 *
 * Two invariants shape this handler:
 *
 * 1. **Every exit path clears the transaction cookie** — success, missing cookie, malformed cookie,
 *    state mismatch, failed exchange, mismatched claims. Nothing holding a live PKCE verifier may
 *    outlive the attempt that created it.
 * 2. **Every failure looks the same**: `303 -> /login?error=retry`. A state mismatch must be
 *    indistinguishable from any other failure, and a uniform bounce also dissolves the multi-tab
 *    race — two tabs share one cookie name, so the loser simply lands back on the login form and
 *    one click succeeds. That is cheaper and more honest than per-attempt cookie names.
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
import { createSession, csrfCookieOptions, sessionCookieOptions } from "@/lib/session";

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

/** Uniform failure: bounce to the login form, and always drop the transaction cookie. */
function retry(request: NextRequest): NextResponse {
  const response = NextResponse.redirect(new URL("/login?error=retry", request.url), 303);
  response.cookies.delete(cookieNames().tx);
  return response;
}

export async function GET(request: NextRequest): Promise<NextResponse> {
  const tx = parseTransaction(request.cookies.get(cookieNames().tx)?.value);
  const code = request.nextUrl.searchParams.get("code");
  const state = request.nextUrl.searchParams.get("state");

  if (!tx || !code || !state || state !== tx.state) return retry(request);

  try {
    // Re-derive the issuer and client id from Django rather than trusting the cookie: this is the
    // authoritative answer, and it is the reason a planted transaction cookie cannot redirect the
    // token exchange to an attacker's endpoint.
    const tenant = await discoverTenant(tx.slug);
    if (!tenant) return retry(request);

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

    const response = NextResponse.redirect(new URL("/", request.url), 303);
    response.cookies.set(cookieNames().session, id, sessionCookieOptions());
    // The readable half of the double-submit pair, minted once per session.
    response.cookies.set(cookieNames().csrf, mintCsrfToken(), csrfCookieOptions());
    response.cookies.delete(cookieNames().tx);
    return response;
  } catch {
    // Deliberately swallowed: the reason a login failed is not something to render to whoever
    // triggered it, and every failure mode here is already a reason to start over.
    return retry(request);
  }
}
