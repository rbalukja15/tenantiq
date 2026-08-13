import { NextRequest } from "next/server";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { GET } from "@/app/api/auth/callback/route";
import { getSession, resetSessionsForTest } from "@/lib/session";
import { resetDiscoveryCacheForTest } from "@/lib/oidc";

import { API_ORIGIN, APP_ORIGIN, isCleared, setCookie, withEnv } from "./env";
import { server } from "./msw";

const ISSUER = "https://idp.test/realms/acme";
const TOKEN_ENDPOINT = `${ISSUER}/protocol/openid-connect/token`;

beforeEach(() => resetDiscoveryCacheForTest());
afterEach(() => resetSessionsForTest());

function base64Url(value: object): string {
  return btoa(JSON.stringify(value)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function idTokenWith(claims: object): string {
  return `header.${base64Url(claims)}.signature`;
}

function mockDiscovery() {
  server.use(
    http.get(`${API_ORIGIN}/api/tenants/discovery`, () =>
      HttpResponse.json({ issuer: ISSUER, client_id: "tenantiq-acme" }),
    ),
    http.get(`${ISSUER}/.well-known/openid-configuration`, () =>
      HttpResponse.json({
        issuer: ISSUER,
        authorization_endpoint: `${ISSUER}/protocol/openid-connect/auth`,
        token_endpoint: TOKEN_ENDPOINT,
        end_session_endpoint: `${ISSUER}/protocol/openid-connect/logout`,
        code_challenge_methods_supported: ["S256"],
      }),
    ),
  );
}

/** Records every token-endpoint call so a test can assert *where* the exchange went. */
function mockTokenEndpoint(options: { nonce?: string; status?: number } = {}) {
  const seen: { url: string; body: Record<string, string> }[] = [];
  server.use(
    http.post(TOKEN_ENDPOINT, async ({ request }) => {
      const body = Object.fromEntries(new URLSearchParams(await request.text()));
      seen.push({ url: request.url, body });
      if (options.status && options.status >= 400) {
        return HttpResponse.json({ error: "invalid_grant" }, { status: options.status });
      }
      return HttpResponse.json({
        access_token: "access-token",
        refresh_token: "refresh-token",
        id_token: idTokenWith({ nonce: options.nonce ?? "n-1", iss: ISSUER }),
        expires_in: 300,
      });
    }),
  );
  return seen;
}

function callback(options: { tx?: object | string | null; state?: string; code?: string } = {}) {
  const headers = new Headers();
  if (options.tx !== null) {
    const value =
      typeof options.tx === "string"
        ? options.tx
        : JSON.stringify(
            options.tx ?? { state: "s-1", nonce: "n-1", verifier: "v-1", slug: "acme" },
          );
    headers.set("cookie", `tiq_tx=${encodeURIComponent(value)}`);
  }
  const url = new URL(`${APP_ORIGIN}/api/auth/callback`);
  url.searchParams.set("code", options.code ?? "auth-code");
  url.searchParams.set("state", options.state ?? "s-1");
  return new NextRequest(url, { headers });
}

function sessionIdFrom(response: Response): string {
  const line = response.headers.getSetCookie().find((c) => c.startsWith("tiq_session=")) as string;
  return line.split(";")[0].split("=")[1];
}

describe("GET /api/auth/callback", () => {
  it("exchanges the code and establishes a session", async () => {
    mockDiscovery();
    const exchanges = mockTokenEndpoint();

    const response = await GET(callback());

    expect(response.status).toBe(303);
    expect(response.headers.get("location")).toBe(`${APP_ORIGIN}/`);
    expect(exchanges[0].body).toMatchObject({
      grant_type: "authorization_code",
      code: "auth-code",
      code_verifier: "v-1", // the PKCE verifier from the transaction cookie, never from the URL
      client_id: "tenantiq-acme",
    });
    const record = getSession(sessionIdFrom(response));
    expect(record?.accessToken).toBe("access-token");
    expect(record?.tenantSlug).toBe("acme");
  });

  it("puts an opaque id in the cookie, never the token itself", async () => {
    // A browser silently drops a Set-Cookie over ~4 KB, so a realm with many role claims would
    // produce a login that "succeeds" server-side and then loops invisibly. An opaque id is a fixed
    // ~36 bytes whatever the IdP emits.
    mockDiscovery();
    mockTokenEndpoint();

    const response = await GET(callback());

    const line = response.headers
      .getSetCookie()
      .find((c) => c.startsWith("tiq_session=")) as string;
    expect(line).not.toContain("access-token");
    expect(sessionIdFrom(response)).toMatch(/^[0-9a-f-]{36}$/);
    expect(line).toMatch(/HttpOnly/);
  });

  it("issues the readable CSRF cookie alongside the session", async () => {
    mockDiscovery();
    mockTokenEndpoint();

    const response = await GET(callback());

    const csrf = response.headers.getSetCookie().find((c) => c.startsWith("tiq_csrf=")) as string;
    expect(csrf).toBeDefined();
    expect(csrf).not.toMatch(/HttpOnly/);
  });

  it("ignores an issuer planted in the transaction cookie and asks the database instead", async () => {
    // The attack this closes: anyone can *write* a cookie, so a transaction cookie naming an
    // attacker's IdP must not steer the token exchange. No handler is registered for evil.example,
    // so if the exchange went there the unhandled-request tripwire fails this test.
    mockDiscovery();
    const exchanges = mockTokenEndpoint();

    const response = await GET(
      callback({
        tx: {
          state: "s-1",
          nonce: "n-1",
          verifier: "v-1",
          slug: "acme",
          issuer: "https://evil.example/realms/acme",
          clientId: "attacker-client",
        },
      }),
    );

    expect(response.status).toBe(303);
    expect(exchanges).toHaveLength(1);
    expect(exchanges[0].url).toBe(TOKEN_ENDPOINT);
    expect(exchanges[0].body.client_id).toBe("tenantiq-acme");
  });

  it.each([
    ["the transaction cookie is missing", { tx: null as null }, true],
    ["the transaction cookie is malformed", { tx: "not-json" }, true],
    // A mismatched state is the one case that must NOT clear: the cookie belongs to a different,
    // possibly live attempt (see below).
    ["the state does not match", { state: "someone-elses-state" }, false],
  ])(
    "bounces back to the login form when %s, creating no session",
    async (_label, opts, clearsTx) => {
      // No MSW handlers: each of these must fail before any network call happens.
      const response = await GET(callback(opts));

      expect(response.status).toBe(303);
      expect(response.headers.get("location")).toContain("/login?error=retry");
      expect(isCleared(response, "tiq_tx")).toBe(clearsTx);
      expect(response.headers.getSetCookie().some((c) => c.startsWith("tiq_session="))).toBe(false);
    },
  );

  it("leaves a concurrent tab's transaction alone when a stale callback arrives", async () => {
    // Tabs share one cookie name, so opening a second login overwrites the first tab's transaction.
    // When the first tab's callback then comes back with a stale state, clearing the cookie would
    // destroy the *live* second attempt too — and both tabs would fail. The stale attempt simply
    // bounces to the login form; the live one is untouched and expires on its own within 10 minutes.
    const live = { state: "tab-two", nonce: "n-2", verifier: "v-2", slug: "acme" };

    const response = await GET(callback({ tx: live, state: "tab-one-stale" }));

    expect(response.headers.get("location")).toContain("/login?error=retry");
    expect(setCookie(response, "tiq_tx")).toBeUndefined(); // not touched at all
  });

  it("clears the transaction and creates no session when the exchange fails", async () => {
    mockDiscovery();
    mockTokenEndpoint({ status: 400 });

    const response = await GET(callback());

    expect(response.headers.get("location")).toContain("/login?error=retry");
    expect(isCleared(response, "tiq_tx")).toBe(true);
    expect(response.headers.getSetCookie().some((c) => c.startsWith("tiq_session="))).toBe(false);
  });

  it("refuses an ID token minted for a different login attempt", async () => {
    mockDiscovery();
    mockTokenEndpoint({ nonce: "a-replayed-nonce" });

    const response = await GET(callback());

    expect(response.headers.get("location")).toContain("/login?error=retry");
    expect(response.headers.getSetCookie().some((c) => c.startsWith("tiq_session="))).toBe(false);
  });
});

describe("GET /api/auth/callback — cookie flags", () => {
  it("mints the session cookie HttpOnly, SameSite=Lax, Path=/", async () => {
    // Asserting the flags the *route* applies, not just those `sessionCookieOptions()` returns:
    // the whole suite previously passed with the callback minting a flagless cookie, because
    // nothing checked what this handler actually emitted.
    mockDiscovery();
    mockTokenEndpoint();

    const line = setCookie(await GET(callback()), "tiq_session") as string;

    expect(line).toMatch(/HttpOnly/);
    expect(line).toMatch(/SameSite=Lax/i);
    expect(line).toMatch(/Path=\/(;|$)/);
  });

  it("marks both cookies Secure and __Host- prefixed on an https deployment", async () => {
    await withEnv({ APP_BASE_URL: "https://app.example" }, async () => {
      const { GET: securedGet } = await import("@/app/api/auth/callback/route");
      mockDiscovery();
      mockTokenEndpoint();
      const headers = new Headers({
        cookie: `__Host-tiq_tx=${encodeURIComponent(
          JSON.stringify({ state: "s-1", nonce: "n-1", verifier: "v-1", slug: "acme" }),
        )}`,
      });
      const url = new URL("https://app.example/api/auth/callback");
      url.searchParams.set("code", "auth-code");
      url.searchParams.set("state", "s-1");

      const response = await securedGet(new NextRequest(url, { headers }));

      for (const name of ["__Host-tiq_session", "__Host-tiq_csrf"]) {
        const line = response.headers
          .getSetCookie()
          .find((c) => c.startsWith(`${name}=`)) as string;
        expect(line, `missing ${name}`).toBeDefined();
        expect(line).toMatch(/Secure/);
        // __Host- forbids Domain; that prohibition is the whole reason for the prefix.
        expect(line).not.toMatch(/Domain=/i);
      }
    });
  });

  it("clears the transaction cookie with Secure on https, so the browser accepts the deletion", async () => {
    // RFC 6265bis makes a browser ignore a `__Host-` Set-Cookie without Secure — which is exactly
    // what `response.cookies.delete()` emits. That made every deletion a silent no-op in production.
    await withEnv({ APP_BASE_URL: "https://app.example" }, async () => {
      const { GET: securedGet } = await import("@/app/api/auth/callback/route");
      const url = new URL("https://app.example/api/auth/callback");
      url.searchParams.set("code", "c");
      url.searchParams.set("state", "s-1");

      const response = await securedGet(
        new NextRequest(url, { headers: new Headers({ cookie: "__Host-tiq_tx=not-json" }) }),
      );

      const line = response.headers
        .getSetCookie()
        .find((c) => c.startsWith("__Host-tiq_tx=")) as string;
      expect(line).toMatch(/Secure/);
      expect(line).toMatch(/Max-Age=0|Expires=Thu, 01 Jan 1970/);
    });
  });
});

describe("redirect origin when the server is bound elsewhere (#81)", () => {
  /**
   * Under `docker compose` Next runs as `next dev -H 0.0.0.0`, and it derives `request.url`'s host
   * from the bound address rather than the `Host` header. Every redirect built on `request.url` then
   * points at `http://0.0.0.0:3000` — a *different origin* from the one the browser is using, so the
   * session cookie just set is not sent with the follow-up request, `proxy.ts` finds no session, and
   * the user is bounced to the login form having authenticated successfully a moment earlier.
   *
   * The whole suite hid this because it builds requests at APP_ORIGIN, where the two agree.
   */
  const BOUND = "http://0.0.0.0:3000";

  /** `callback()` builds its request at APP_ORIGIN; this rebuilds the same one at the bound host. */
  function boundCallback(): NextRequest {
    const original = callback();
    const url = new URL(original.url);
    url.protocol = "http:";
    url.host = "0.0.0.0:3000";
    return new NextRequest(url, { headers: original.headers });
  }

  it("sends a successful sign-in to the app's own origin", async () => {
    mockDiscovery();
    mockTokenEndpoint();

    const response = await GET(boundCallback());

    expect(new URL(response.headers.get("location")!).origin).toBe(APP_ORIGIN);
  });

  it("sends a failed sign-in back to the login form on the app's own origin", async () => {
    const response = await GET(new NextRequest(`${BOUND}/api/auth/callback?code=abc&state=nope`));

    const location = new URL(response.headers.get("location")!);
    expect(location.origin).toBe(APP_ORIGIN);
    expect(location.pathname).toBe("/login");
  });
});
