/**
 * The OIDC half of the BFF: PKCE, provider discovery, and the token exchange (#18, ADR-0013 §1).
 *
 * Hand-rolled rather than pulled from a library, for one reason that matters here: everything below
 * is a plain function of its arguments and its `fetch` calls, so MSW can intercept them at the
 * network boundary and the whole login flow is testable with no live Keycloak — the same property
 * the backend gets from its injectable token verifier (ADR-0002, #7).
 *
 * The BFF is a **public** OAuth client using Authorization Code + PKCE. It holds no per-tenant
 * client secret, because the only per-tenant configuration channel is the deliberately public
 * discovery endpoint; a secret would need a second, authenticated Next->Django channel with its own
 * credential. That is the RFC 9700 posture for browser-delivered flows.
 */

import { apiBaseUrl, allowInsecureIssuer, cookieSecure } from "@/lib/config";

export type TenantOidcConfig = { issuer: string; clientId: string };

export type OpenIdConfiguration = {
  issuer: string;
  authorizationEndpoint: string;
  tokenEndpoint: string;
  endSessionEndpoint?: string;
};

export type TokenResponse = {
  accessToken: string;
  refreshToken?: string;
  idToken?: string;
  /** Seconds until the access token expires. */
  expiresIn: number;
};

export class OidcError extends Error {
  /**
   * True when the failure was the *provider being unreachable or broken*, rather than a definitive
   * rejection of the credential. The two must not be treated alike: `invalid_grant` means the
   * refresh token is genuinely dead and the session is over, while a timeout or a 502 means try
   * again — destroying the session on the latter logs everyone out during a brief IdP blip.
   */
  readonly retryable: boolean;

  constructor(message: string, options: { retryable?: boolean } = {}) {
    super(message);
    this.retryable = options.retryable ?? false;
  }
}

const DISCOVERY_TIMEOUT_MS = 5000;
/** Mirrors the backend's PyJWKClient(lifespan=300) so both sides age provider metadata alike. */
const DISCOVERY_TTL_MS = 5 * 60 * 1000;

// --- encoding helpers -----------------------------------------------------------------------------

function base64UrlEncode(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  // Unpadded base64url: '+' -> '-', '/' -> '_', and no '=' — RFC 7636 requires this exact alphabet,
  // and a stray '=' or '+' makes the IdP reject the challenge with an unhelpful error.
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function base64UrlDecodeToString(input: string): string {
  const normalized = input.replace(/-/g, "+").replace(/_/g, "/");
  const padding = normalized.length % 4 === 0 ? "" : "=".repeat(4 - (normalized.length % 4));
  const binary = atob(normalized + padding);
  const bytes = Uint8Array.from(binary, (char) => char.charCodeAt(0));
  return new TextDecoder().decode(bytes); // JWT payloads are UTF-8; atob alone would mangle non-ASCII
}

// --- PKCE -----------------------------------------------------------------------------------------

/** 32 bytes of CSPRNG as unpadded base64url: 43 characters, inside RFC 7636's 43-128 range. */
export function createCodeVerifier(): string {
  return base64UrlEncode(crypto.getRandomValues(new Uint8Array(32)));
}

/** S256 only. `plain` is never offered and never accepted — see `fetchOpenIdConfiguration`. */
export async function codeChallengeFor(verifier: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
  return base64UrlEncode(new Uint8Array(digest));
}

/** Opaque random values for `state` (CSRF binding) and `nonce` (ID-token replay binding). */
export function randomToken(): string {
  return base64UrlEncode(crypto.getRandomValues(new Uint8Array(32)));
}

// --- tenant discovery (our own API) ---------------------------------------------------------------

/**
 * Ask Django which realm and client this tenant uses. `null` for an unknown or inactive tenant —
 * the endpoint deliberately cannot distinguish those, and neither can the caller.
 */
export async function discoverTenant(slug: string): Promise<TenantOidcConfig | null> {
  const url = new URL("/api/tenants/discovery", apiBaseUrl());
  url.searchParams.set("slug", slug);
  const response = await fetch(url, {
    cache: "no-store",
    signal: AbortSignal.timeout(DISCOVERY_TIMEOUT_MS),
  });
  if (response.status === 404) return null;
  if (!response.ok) throw new OidcError(`tenant discovery failed (${response.status})`);
  const body: unknown = await response.json();
  const { issuer, client_id: clientId } = body as { issuer?: string; client_id?: string };
  if (!issuer || !clientId) throw new OidcError("tenant discovery returned an incomplete record");
  return { issuer, clientId };
}

// --- provider discovery ---------------------------------------------------------------------------

const documentCache = new Map<string, { value: OpenIdConfiguration; expiresAt: number }>();

/** Test seam: forget cached provider metadata between tests. */
export function resetDiscoveryCacheForTest(): void {
  documentCache.clear();
}

/**
 * An endpoint URL must be absolute and https — with `http` allowed only for loopback, and only when
 * the app itself is running without TLS (i.e. local development).
 *
 * Note what is deliberately *not* enforced: same-origin with the issuer. Real providers split them
 * (Google's issuer is `accounts.google.com` while its token endpoint is `oauth2.googleapis.com`), so
 * an origin equality check would break any provider but Keycloak while adding nothing — the document
 * is already pinned to the issuer by the equality check below.
 */
function assertUsableEndpoint(raw: string | undefined, name: string): string {
  if (!raw) throw new OidcError(`provider metadata is missing ${name}`);
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    throw new OidcError(`provider metadata has a non-absolute ${name}`);
  }
  const loopback = ["localhost", "127.0.0.1", "[::1]", "::1"].includes(url.hostname);
  // `!cookieSecure()` stays the OUTER condition, and the opt-in cannot reach it: once the app is
  // served over TLS an http issuer is refused however the flag is set. Otherwise a single stray
  // environment variable would downgrade a production login to cleartext (#79).
  const httpAllowed =
    url.protocol === "http:" && (loopback || allowInsecureIssuer()) && !cookieSecure();
  if (url.protocol !== "https:" && !httpAllowed) {
    throw new OidcError(`${name} must be https (got ${url.protocol}//${url.host})`);
  }
  return url.toString();
}

export async function fetchOpenIdConfiguration(issuer: string): Promise<OpenIdConfiguration> {
  const cached = documentCache.get(issuer);
  if (cached && cached.expiresAt > Date.now()) return cached.value;

  const url = new URL(`${issuer.replace(/\/$/, "")}/.well-known/openid-configuration`);
  const response = await fetch(url, {
    cache: "no-store",
    // `redirect: "error"` matters: following a redirect here would let a compromised or misconfigured
    // issuer host bounce metadata resolution somewhere else entirely.
    redirect: "error",
    signal: AbortSignal.timeout(DISCOVERY_TIMEOUT_MS),
  });
  if (!response.ok) throw new OidcError(`provider metadata fetch failed (${response.status})`);
  const doc = (await response.json()) as Record<string, unknown>;

  // OIDC Discovery §4.3: the document must claim the issuer we asked for. This is what stops a
  // hijacked well-known path from redescribing itself as somebody else, and it is also what catches
  // the common compose mistake where Keycloak derives its issuer from the request Host and returns
  // a value that does not match the one stored on the Tenant row.
  const declared = String(doc.issuer ?? "").replace(/\/$/, "");
  if (declared !== issuer.replace(/\/$/, "")) {
    throw new OidcError(`provider metadata issuer mismatch (declared ${declared || "nothing"})`);
  }

  const methods = doc.code_challenge_methods_supported;
  if (!Array.isArray(methods) || !methods.includes("S256")) {
    // Refusing here rather than silently downgrading: `plain` PKCE offers no protection at all
    // against an intercepted authorization code.
    throw new OidcError("provider does not support PKCE S256");
  }

  const value: OpenIdConfiguration = {
    issuer: declared,
    authorizationEndpoint: assertUsableEndpoint(
      doc.authorization_endpoint as string | undefined,
      "authorization_endpoint",
    ),
    tokenEndpoint: assertUsableEndpoint(doc.token_endpoint as string | undefined, "token_endpoint"),
    endSessionEndpoint: doc.end_session_endpoint
      ? assertUsableEndpoint(doc.end_session_endpoint as string, "end_session_endpoint")
      : undefined,
  };
  documentCache.set(issuer, { value, expiresAt: Date.now() + DISCOVERY_TTL_MS });
  return value;
}

// --- token endpoint -------------------------------------------------------------------------------

async function postToTokenEndpoint(
  tokenEndpoint: string,
  form: URLSearchParams,
): Promise<TokenResponse> {
  let response: Response;
  try {
    response = await fetch(tokenEndpoint, {
      method: "POST",
      headers: { "content-type": "application/x-www-form-urlencoded" },
      body: form,
      cache: "no-store",
      signal: AbortSignal.timeout(DISCOVERY_TIMEOUT_MS),
    });
  } catch (cause) {
    // The provider is unreachable or timed out — that says nothing about the credential.
    throw new OidcError(`token endpoint unreachable: ${String(cause)}`, { retryable: true });
  }
  const body = (await response.json().catch(() => ({}))) as Record<string, unknown>;
  if (!response.ok) {
    const error = String(body.error ?? response.status);
    if (response.status >= 500) {
      throw new OidcError(`token endpoint returned ${response.status}`, { retryable: true });
    }
    if (error === "invalid_client") {
      // The single most likely misconfiguration, so it gets a message that names the fix rather than
      // an opaque failure: TenantIQ has nowhere to put a client secret (see docs/auth-keycloak.md).
      throw new OidcError(
        "token endpoint rejected the client (invalid_client): the Keycloak client must have " +
          "Client authentication OFF — TenantIQ authenticates as a public client with PKCE.",
      );
    }
    throw new OidcError(`token endpoint returned ${error}`);
  }
  const accessToken = body.access_token;
  if (typeof accessToken !== "string") throw new OidcError("token response has no access_token");
  return {
    accessToken,
    refreshToken: typeof body.refresh_token === "string" ? body.refresh_token : undefined,
    idToken: typeof body.id_token === "string" ? body.id_token : undefined,
    expiresIn: typeof body.expires_in === "number" ? body.expires_in : 300,
  };
}

export function exchangeCode(params: {
  tokenEndpoint: string;
  clientId: string;
  code: string;
  redirectUri: string;
  codeVerifier: string;
}): Promise<TokenResponse> {
  return postToTokenEndpoint(
    params.tokenEndpoint,
    new URLSearchParams({
      grant_type: "authorization_code",
      client_id: params.clientId,
      code: params.code,
      redirect_uri: params.redirectUri,
      code_verifier: params.codeVerifier,
    }),
  );
}

export function refreshAccessToken(params: {
  tokenEndpoint: string;
  clientId: string;
  refreshToken: string;
}): Promise<TokenResponse> {
  return postToTokenEndpoint(
    params.tokenEndpoint,
    new URLSearchParams({
      grant_type: "refresh_token",
      client_id: params.clientId,
      refresh_token: params.refreshToken,
    }),
  );
}

// --- ID token -------------------------------------------------------------------------------------

/**
 * Decode the ID token payload and check it is *consistent* with the transaction we started.
 *
 * **This is not a security boundary, and it is important not to mistake it for one.** There is no
 * signature verification here. It does not need one: the token arrived over TLS directly from a
 * token endpoint the server derived from its own database, in response to a code bound by PKCE. The
 * real authorization boundary is Django, which verifies signature, issuer, audience and expiry
 * properly on every API call (`app/auth/verifier.py`). What this catches is a mismatched `nonce` or
 * `iss` — i.e. a response that does not belong to this login attempt.
 */
export function assertIdTokenMatches(
  idToken: string | undefined,
  expected: { nonce: string; issuer: string },
): void {
  if (!idToken) return; // a provider that returns no ID token is not an error for our purposes
  const [, payload] = idToken.split(".");
  if (!payload) throw new OidcError("id_token is malformed");
  let claims: { nonce?: string; iss?: string };
  try {
    claims = JSON.parse(base64UrlDecodeToString(payload)) as { nonce?: string; iss?: string };
  } catch {
    throw new OidcError("id_token payload is not JSON");
  }
  if (claims.nonce !== expected.nonce) throw new OidcError("id_token nonce does not match");
  if (claims.iss?.replace(/\/$/, "") !== expected.issuer.replace(/\/$/, "")) {
    throw new OidcError("id_token issuer does not match");
  }
}
