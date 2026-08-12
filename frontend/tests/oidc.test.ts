import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  OidcError,
  assertIdTokenMatches,
  codeChallengeFor,
  createCodeVerifier,
  discoverTenant,
  fetchOpenIdConfiguration,
  resetDiscoveryCacheForTest,
} from "@/lib/oidc";

import { API_ORIGIN, withEnv } from "./env";
import { server } from "./msw";

const ISSUER = "https://idp.test/realms/acme";

beforeEach(() => resetDiscoveryCacheForTest());
afterEach(() => resetDiscoveryCacheForTest());

/** A minimal, valid provider document; individual tests override the parts they are probing. */
function providerDocument(overrides: Record<string, unknown> = {}) {
  return {
    issuer: ISSUER,
    authorization_endpoint: `${ISSUER}/protocol/openid-connect/auth`,
    token_endpoint: `${ISSUER}/protocol/openid-connect/token`,
    end_session_endpoint: `${ISSUER}/protocol/openid-connect/logout`,
    code_challenge_methods_supported: ["S256"],
    ...overrides,
  };
}

function mockProvider(overrides: Record<string, unknown> = {}) {
  server.use(
    http.get(`${ISSUER}/.well-known/openid-configuration`, () =>
      HttpResponse.json(providerDocument(overrides)),
    ),
  );
}

function base64Url(value: object): string {
  return btoa(JSON.stringify(value)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

describe("PKCE", () => {
  it("produces a verifier in RFC 7636's charset and length range", () => {
    const verifier = createCodeVerifier();

    expect(verifier).toMatch(/^[A-Za-z0-9\-._~]+$/);
    expect(verifier.length).toBeGreaterThanOrEqual(43);
    expect(verifier.length).toBeLessThanOrEqual(128);
    expect(createCodeVerifier()).not.toBe(verifier);
  });

  it("derives the S256 challenge exactly as the RFC 7636 appendix B vector does", async () => {
    // The published vector. Getting the base64url alphabet or the padding wrong still produces a
    // plausible-looking string, and the IdP then rejects the exchange with an opaque error — so this
    // is pinned to a known answer rather than to a round-trip.
    const verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk";

    expect(await codeChallengeFor(verifier)).toBe("E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM");
  });

  it("emits no base64 padding or non-url-safe characters", async () => {
    const challenge = await codeChallengeFor(createCodeVerifier());

    expect(challenge).not.toMatch(/[+/=]/);
  });
});

describe("discoverTenant", () => {
  it("maps a slug to its issuer and client id", async () => {
    server.use(
      http.get(`${API_ORIGIN}/api/tenants/discovery`, ({ request }) => {
        expect(new URL(request.url).searchParams.get("slug")).toBe("acme");
        return HttpResponse.json({ issuer: ISSUER, client_id: "tenantiq-acme" });
      }),
    );

    expect(await discoverTenant("acme")).toEqual({ issuer: ISSUER, clientId: "tenantiq-acme" });
  });

  it("returns null for an unknown tenant rather than throwing", async () => {
    server.use(
      http.get(`${API_ORIGIN}/api/tenants/discovery`, () =>
        HttpResponse.json({ detail: "Not found." }, { status: 404 }),
      ),
    );

    expect(await discoverTenant("nope")).toBeNull();
  });
});

describe("fetchOpenIdConfiguration", () => {
  it("returns the endpoints it needs from a valid document", async () => {
    mockProvider();

    const provider = await fetchOpenIdConfiguration(ISSUER);

    expect(provider.tokenEndpoint).toBe(`${ISSUER}/protocol/openid-connect/token`);
    expect(provider.endSessionEndpoint).toBe(`${ISSUER}/protocol/openid-connect/logout`);
  });

  it("rejects a document that claims a different issuer", async () => {
    // OIDC Discovery §4.3. This is what stops a hijacked well-known path from redescribing itself as
    // somebody else — and it also catches the common compose mistake where Keycloak derives its
    // issuer from the request Host and returns something the Tenant row does not match.
    mockProvider({ issuer: "https://evil.example/realms/acme" });

    await expect(fetchOpenIdConfiguration(ISSUER)).rejects.toThrow(OidcError);
  });

  it("rejects a provider that cannot do PKCE S256 instead of silently downgrading", async () => {
    mockProvider({ code_challenge_methods_supported: ["plain"] });

    await expect(fetchOpenIdConfiguration(ISSUER)).rejects.toThrow(/S256/);
  });

  it("rejects a plaintext token endpoint on an https deployment", async () => {
    // The exfiltration shape: a document that keeps a legitimate issuer but points the token
    // endpoint at an attacker's collector.
    await withEnv({ APP_BASE_URL: "https://app.example" }, async () => {
      const oidc = await import("@/lib/oidc");
      server.use(
        http.get(`${ISSUER}/.well-known/openid-configuration`, () =>
          HttpResponse.json(providerDocument({ token_endpoint: "http://collector.evil.example/t" })),
        ),
      );

      await expect(oidc.fetchOpenIdConfiguration(ISSUER)).rejects.toThrow(/https/);
    });
  });

  it("accepts endpoints on a different host from the issuer", async () => {
    // Deliberately NOT same-origin-constrained: Google's issuer is accounts.google.com while its
    // token endpoint is oauth2.googleapis.com. An origin check would break every provider but
    // Keycloak while adding nothing — the issuer equality check already pins the document.
    const google = "https://accounts.google.com";
    server.use(
      http.get(`${google}/.well-known/openid-configuration`, () =>
        HttpResponse.json({
          issuer: google,
          authorization_endpoint: `${google}/o/oauth2/v2/auth`,
          token_endpoint: "https://oauth2.googleapis.com/token",
          code_challenge_methods_supported: ["S256"],
        }),
      ),
    );

    const provider = await fetchOpenIdConfiguration(google);

    expect(provider.tokenEndpoint).toBe("https://oauth2.googleapis.com/token");
  });

  it("caches the document so a login does not re-fetch it on every step", async () => {
    let calls = 0;
    server.use(
      http.get(`${ISSUER}/.well-known/openid-configuration`, () => {
        calls += 1;
        return HttpResponse.json(providerDocument());
      }),
    );

    await fetchOpenIdConfiguration(ISSUER);
    await fetchOpenIdConfiguration(ISSUER);

    expect(calls).toBe(1);
  });
});

describe("assertIdTokenMatches", () => {
  const idToken = (claims: object) => `header.${base64Url(claims)}.signature`;

  it("accepts a token whose nonce and issuer match the transaction", () => {
    expect(() =>
      assertIdTokenMatches(idToken({ nonce: "n-1", iss: ISSUER }), {
        nonce: "n-1",
        issuer: ISSUER,
      }),
    ).not.toThrow();
  });

  it("rejects a replayed token from a different login attempt", () => {
    expect(() =>
      assertIdTokenMatches(idToken({ nonce: "someone-elses", iss: ISSUER }), {
        nonce: "n-1",
        issuer: ISSUER,
      }),
    ).toThrow(/nonce/);
  });

  it("rejects a token issued by a different realm", () => {
    expect(() =>
      assertIdTokenMatches(idToken({ nonce: "n-1", iss: "https://idp.test/realms/globex" }), {
        nonce: "n-1",
        issuer: ISSUER,
      }),
    ).toThrow(/issuer/);
  });
});
