/**
 * Start the OIDC login: `POST /api/auth/login` with `tenant=<slug>` (#18, ADR-0013 §1/§2).
 *
 * **Why POST, and why the `Origin` check.** The obvious design is a `GET` link to
 * `/api/auth/login?tenant=acme`. It is also a real vulnerability: `SameSite=Lax` deliberately permits
 * top-level GET navigations, so any website could send a logged-out visitor straight into an
 * *attacker-chosen* tenant. The victim authenticates to the attacker's realm, and the next document
 * they upload lands in the attacker's tenant. Tenant isolation would hold perfectly at every layer
 * and the data would still cross. A cross-site form POST always carries a mismatched `Origin`, so
 * requiring both closes it.
 */

import { NextResponse, type NextRequest } from "next/server";

import { appBaseUrl, cookieNames } from "@/lib/config";
import { requireSameOrigin } from "@/lib/csrf";
import {
  codeChallengeFor,
  createCodeVerifier,
  discoverTenant,
  fetchOpenIdConfiguration,
  randomToken,
} from "@/lib/oidc";
import { txCookieOptions } from "@/lib/session";

/** Matches Django's `SlugField(max_length=63)`, so an invalid slug never reaches the API. */
const SLUG = /^[a-z0-9-]{1,63}$/;

function backToLogin(request: NextRequest, error: string): NextResponse {
  // Always an absolute URL: `NextResponse.redirect("/login")` throws `URL is malformed`.
  return NextResponse.redirect(new URL(`/login?error=${error}`, request.url), 303);
}

export async function POST(request: NextRequest): Promise<NextResponse> {
  if (!requireSameOrigin(request)) {
    // Fails before any cookie is set and before any upstream call: a cross-site attempt must leave
    // no trace and cost nothing.
    return NextResponse.json({ error: "forbidden" }, { status: 403 });
  }

  const form = await request.formData();
  const slug = String(form.get("tenant") ?? "")
    .trim()
    .toLowerCase();
  if (!SLUG.test(slug)) return backToLogin(request, "unknown_tenant");

  // Everything from here can fail on something outside our control — Django down or rate-limiting
  // us, the IdP unreachable, a malformed discovery document. Without this the user gets Next's
  // unhandled-error page (a raw 500) instead of the login form, which is both a worse experience and
  // a way for provider details to reach a browser.
  try {
    return await startAuthorization(request, slug);
  } catch {
    return backToLogin(request, "unavailable");
  }
}

async function startAuthorization(request: NextRequest, slug: string): Promise<NextResponse> {
  const tenant = await discoverTenant(slug);
  if (!tenant) return backToLogin(request, "unknown_tenant");

  const provider = await fetchOpenIdConfiguration(tenant.issuer);
  const state = randomToken();
  const nonce = randomToken();
  const verifier = createCodeVerifier();

  const authorize = new URL(provider.authorizationEndpoint);
  authorize.searchParams.set("response_type", "code");
  authorize.searchParams.set("client_id", tenant.clientId);
  authorize.searchParams.set("redirect_uri", `${appBaseUrl()}/api/auth/callback`);
  authorize.searchParams.set("scope", "openid profile email");
  authorize.searchParams.set("state", state);
  authorize.searchParams.set("nonce", nonce);
  authorize.searchParams.set("code_challenge", await codeChallengeFor(verifier));
  authorize.searchParams.set("code_challenge_method", "S256");

  // 303, not `NextResponse.redirect`'s default of 307: 307 preserves the method, which would make
  // the browser POST to the IdP's authorization endpoint (a GET-only endpoint).
  const response = NextResponse.redirect(authorize, 303);
  // The transaction cookie carries no issuer and no client id. httpOnly stops script *reading* a
  // cookie; it does nothing to stop anyone *writing* one, and nothing proves the cookie we read back
  // is the cookie we wrote. Which IdP to trust for a slug is a question the server can answer from
  // its own database — so the callback asks the database again rather than believing the browser.
  response.cookies.set(
    cookieNames().tx,
    JSON.stringify({ state, nonce, verifier, slug }),
    txCookieOptions(),
  );
  return response;
}
