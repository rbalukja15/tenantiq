import { NextRequest } from "next/server";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it } from "vitest";

import { POST } from "@/app/api/auth/login/route";
import { resetDiscoveryCacheForTest } from "@/lib/oidc";

import { API_ORIGIN, APP_ORIGIN } from "./env";
import { server } from "./msw";

const ISSUER = "https://idp.test/realms/acme";

beforeEach(() => resetDiscoveryCacheForTest());

function mockTenantAndProvider() {
  server.use(
    http.get(`${API_ORIGIN}/api/tenants/discovery`, () =>
      HttpResponse.json({ issuer: ISSUER, client_id: "tenantiq-acme" }),
    ),
    http.get(`${ISSUER}/.well-known/openid-configuration`, () =>
      HttpResponse.json({
        issuer: ISSUER,
        authorization_endpoint: `${ISSUER}/protocol/openid-connect/auth`,
        token_endpoint: `${ISSUER}/protocol/openid-connect/token`,
        code_challenge_methods_supported: ["S256"],
      }),
    ),
  );
}

function loginRequest(options: { origin?: string | null; tenant?: string } = {}): NextRequest {
  const body = new FormData();
  body.set("tenant", options.tenant ?? "acme");
  const headers = new Headers();
  if (options.origin !== null) headers.set("origin", options.origin ?? APP_ORIGIN);
  return new NextRequest(`${APP_ORIGIN}/api/auth/login`, { method: "POST", headers, body });
}

describe("POST /api/auth/login", () => {
  it("redirects to the tenant's IdP with a PKCE-protected authorization request", async () => {
    mockTenantAndProvider();

    const response = await POST(loginRequest());

    // 303, not the NextResponse.redirect default of 307: 307 preserves the method, so the browser
    // would POST to an authorization endpoint that only answers GET.
    expect(response.status).toBe(303);
    const location = new URL(response.headers.get("location") as string);
    expect(location.origin + location.pathname).toBe(`${ISSUER}/protocol/openid-connect/auth`);
    expect(location.searchParams.get("response_type")).toBe("code");
    expect(location.searchParams.get("client_id")).toBe("tenantiq-acme");
    expect(location.searchParams.get("redirect_uri")).toBe(`${APP_ORIGIN}/api/auth/callback`);
    expect(location.searchParams.get("code_challenge_method")).toBe("S256");
    expect(location.searchParams.get("code_challenge")).toMatch(/^[A-Za-z0-9\-_]{43}$/);
    expect(location.searchParams.get("state")).toBeTruthy();
    expect(location.searchParams.get("nonce")).toBeTruthy();
  });

  it("stashes the PKCE verifier in an httpOnly transaction cookie", async () => {
    mockTenantAndProvider();

    const response = await POST(loginRequest());

    const line = response.headers.getSetCookie().find((c) => c.startsWith("tiq_tx=")) as string;
    expect(line).toMatch(/HttpOnly/);
    expect(line).toMatch(/Max-Age=600/);
    const payload = JSON.parse(
      decodeURIComponent(line.split(";")[0].split("=").slice(1).join("=")),
    );
    expect(payload).toHaveProperty("verifier");
    expect(payload).toHaveProperty("state");
    expect(payload.slug).toBe("acme");
  });

  it("does not record which IdP to trust in the cookie", async () => {
    // httpOnly stops script *reading* a cookie; nothing stops anyone *writing* one, and nothing
    // proves the cookie read back is the cookie written. Which realm a slug belongs to is a question
    // the server can answer from its own database, so the callback asks the database again.
    mockTenantAndProvider();

    const response = await POST(loginRequest());

    const line = response.headers.getSetCookie().find((c) => c.startsWith("tiq_tx=")) as string;
    expect(line).not.toContain("idp.test");
    expect(line).not.toContain("client");
  });

  it("refuses a cross-site login attempt, sets no cookie, and contacts nothing", async () => {
    // Without this, SameSite=Lax's deliberate allowance of top-level GET navigations would let any
    // site push a visitor into an attacker-chosen tenant: the victim authenticates against the
    // attacker's realm and their next upload lands in the attacker's workspace. Tenant isolation
    // holds perfectly at every layer and the data still crosses.
    // No MSW handlers are registered here on purpose — the unhandled-request tripwire in setup.ts
    // turns any outbound call into a test failure, which is what proves "contacts nothing".
    const response = await POST(loginRequest({ origin: "https://evil.example" }));

    expect(response.status).toBe(403);
    expect(response.headers.getSetCookie()).toEqual([]);
  });

  it("refuses a request with no Origin header", async () => {
    const response = await POST(loginRequest({ origin: null }));

    expect(response.status).toBe(403);
    expect(response.headers.getSetCookie()).toEqual([]);
  });

  it("sends an unknown workspace back to the form without contacting an IdP", async () => {
    server.use(
      http.get(`${API_ORIGIN}/api/tenants/discovery`, () =>
        HttpResponse.json({ detail: "Not found." }, { status: 404 }),
      ),
    );

    const response = await POST(loginRequest({ tenant: "nope" }));

    expect(response.status).toBe(303);
    expect(response.headers.get("location")).toContain("/login?error=unknown_tenant");
  });

  it("rejects a malformed slug before it ever reaches the API", async () => {
    // No handlers registered: a slug that cannot be a Django SlugField must not become a request.
    const response = await POST(loginRequest({ tenant: "../../etc/passwd" }));

    expect(response.status).toBe(303);
    expect(response.headers.get("location")).toContain("unknown_tenant");
  });
});
