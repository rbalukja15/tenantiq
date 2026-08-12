import { NextRequest } from "next/server";
import { afterEach, describe, expect, it } from "vitest";

import { POST } from "@/app/api/auth/logout/route";
import { createSession, getSession, type SessionRecord } from "@/lib/session";
import { resetSessionsForTest } from "@/lib/session";

import { APP_ORIGIN, isCleared, withEnv } from "./env";

const ISSUER = "https://idp.test/realms/acme";

afterEach(() => resetSessionsForTest());

function seedSession(overrides: Partial<SessionRecord> = {}): string {
  return createSession({
    accessToken: "access-token",
    idToken: "id-token",
    expiresAt: Math.floor(Date.now() / 1000) + 3600,
    tokenEndpoint: `${ISSUER}/protocol/openid-connect/token`,
    endSessionEndpoint: `${ISSUER}/protocol/openid-connect/logout`,
    issuer: ISSUER,
    clientId: "tenantiq-acme",
    tenantSlug: "acme",
    createdAt: Date.now(),
    ...overrides,
  });
}

function logoutRequest(options: { id?: string; token?: string; origin?: string | null } = {}) {
  const token = options.token ?? "csrf-token";
  const headers = new Headers();
  if (options.origin !== null) headers.set("origin", options.origin ?? APP_ORIGIN);
  headers.set("x-csrf-token", token);
  const cookies = [`tiq_csrf=${token}`];
  if (options.id) cookies.unshift(`tiq_session=${options.id}`);
  headers.set("cookie", cookies.join("; "));
  return new NextRequest(`${APP_ORIGIN}/api/auth/logout`, { method: "POST", headers });
}

describe("POST /api/auth/logout", () => {
  it("destroys the server-side session, not just the cookie", async () => {
    // The tokens live server-side, so this is real revocation: the id becomes worthless immediately,
    // rather than merely being forgotten by one browser.
    const id = seedSession();

    await POST(logoutRequest({ id }));

    expect(getSession(id)).toBeUndefined();
  });

  it("returns the IdP's end-session URL as JSON for the client to navigate to", async () => {
    // Not a redirect. `fetch` follows a cross-origin redirect itself, so the IdP's logout page would
    // land in a response body nobody renders — address bar unchanged, IdP session still alive. And a
    // 307 from a POST would re-POST to the target.
    const id = seedSession();

    const response = await POST(logoutRequest({ id }));

    expect(response.status).toBe(200);
    const body = (await response.json()) as { location: string };
    const location = new URL(body.location);
    expect(location.origin + location.pathname).toBe(`${ISSUER}/protocol/openid-connect/logout`);
    // Without id_token_hint, Keycloak 18+ shows a "did you mean to sign out?" confirmation page.
    expect(location.searchParams.get("id_token_hint")).toBe("id-token");
    expect(location.searchParams.get("post_logout_redirect_uri")).toBe(`${APP_ORIGIN}/login`);
  });

  it("clears both cookies", async () => {
    const response = await POST(logoutRequest({ id: seedSession() }));

    expect(isCleared(response, "tiq_session")).toBe(true);
    expect(isCleared(response, "tiq_csrf")).toBe(true);
  });

  it("falls back to the login page when the provider publishes no end-session endpoint", async () => {
    const id = seedSession({ endSessionEndpoint: undefined });

    const body = (await (await POST(logoutRequest({ id }))).json()) as { location: string };

    expect(body.location).toBe(`${APP_ORIGIN}/login`);
  });

  it("refuses a cross-site logout and leaves the session alive", async () => {
    // Forced logout is a genuine nuisance attack, and this is a state-changing route like any other.
    const id = seedSession();

    const response = await POST(logoutRequest({ id, origin: "https://evil.example" }));

    expect(response.status).toBe(403);
    expect(getSession(id)).toBeDefined();
  });

  it("refuses when the CSRF header does not match the cookie", async () => {
    const id = seedSession();
    const headers = new Headers({
      origin: APP_ORIGIN,
      "x-csrf-token": "header-token",
      cookie: `tiq_session=${id}; tiq_csrf=a-different-token`,
    });

    const response = await POST(
      new NextRequest(`${APP_ORIGIN}/api/auth/logout`, { method: "POST", headers }),
    );

    expect(response.status).toBe(403);
    expect(getSession(id)).toBeDefined();
  });
});

describe("POST /api/auth/logout — https", () => {
  it("emits clearing cookies the browser will actually accept", async () => {
    // The bug this pins: `response.cookies.delete(name)` emits no `Secure`, and RFC 6265bis makes a
    // browser IGNORE a `__Host-`-prefixed Set-Cookie without it. So on https — the only deployment
    // where the prefix is applied — signing out left the session cookie in the jar for its full
    // eight hours, while every local test passed because tests run on plain http.
    await withEnv({ APP_BASE_URL: "https://app.example" }, async () => {
      const { POST: securedPost } = await import("@/app/api/auth/logout/route");
      const { createSession } = await import("@/lib/session");
      const id = createSession({
        accessToken: "a",
        expiresAt: Math.floor(Date.now() / 1000) + 3600,
        tokenEndpoint: `${ISSUER}/protocol/openid-connect/token`,
        issuer: ISSUER,
        clientId: "tenantiq-acme",
        tenantSlug: "acme",
        createdAt: Date.now(),
      });
      const headers = new Headers({
        origin: "https://app.example",
        "x-csrf-token": "tok",
        cookie: `__Host-tiq_session=${id}; __Host-tiq_csrf=tok`,
      });

      const response = await securedPost(
        new NextRequest("https://app.example/api/auth/logout", { method: "POST", headers }),
      );

      for (const name of ["__Host-tiq_session", "__Host-tiq_csrf"]) {
        const line = response.headers.getSetCookie().find((c) => c.startsWith(`${name}=`));
        expect(line, `no clearing Set-Cookie for ${name}`).toBeDefined();
        expect(line).toMatch(/Secure/);
        expect(line).toMatch(/Max-Age=0|Expires=Thu, 01 Jan 1970/);
      }
    });
  });
});
