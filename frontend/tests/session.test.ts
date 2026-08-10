import { NextResponse } from "next/server";
import { afterEach, describe, expect, it } from "vitest";

import { cookieNames } from "@/lib/config";
import {
  SESSION_TTL_MS,
  createSession,
  csrfCookieOptions,
  deleteSession,
  getSession,
  needsRefresh,
  resetSessionsForTest,
  sessionCookieOptions,
  updateSession,
  type SessionRecord,
} from "@/lib/session";

import { withEnv } from "./env";

afterEach(() => resetSessionsForTest());

function record(overrides: Partial<SessionRecord> = {}): SessionRecord {
  return {
    accessToken: "access-token",
    refreshToken: "refresh-token",
    expiresAt: Math.floor(Date.now() / 1000) + 3600,
    tokenEndpoint: "https://idp.test/token",
    issuer: "https://idp.test/realms/acme",
    clientId: "tenantiq-acme",
    tenantSlug: "acme",
    createdAt: Date.now(),
    ...overrides,
  };
}

/** Read the literal `Set-Cookie` line, which is the only thing the browser actually sees. */
function setCookieFor(name: string, response: NextResponse): string {
  const line = response.headers.getSetCookie().find((entry) => entry.startsWith(`${name}=`));
  expect(line, `no Set-Cookie for ${name}`).toBeDefined();
  return line as string;
}

describe("lib/session — the store", () => {
  it("round-trips a record under an opaque id", () => {
    const id = createSession(record());

    expect(getSession(id)?.accessToken).toBe("access-token");
    // The id must carry no information about the session: it is handed to the browser.
    expect(id).toMatch(/^[0-9a-f-]{36}$/);
    expect(id).not.toContain("acme");
  });

  it("keeps two sessions completely separate", () => {
    // The isolation property this store must never lose: one browser's session id can only ever
    // resolve to its own tenant's token.
    const acme = createSession(record({ accessToken: "token-A", tenantSlug: "acme" }));
    const globex = createSession(record({ accessToken: "token-B", tenantSlug: "globex" }));

    expect(getSession(acme)?.accessToken).toBe("token-A");
    expect(getSession(globex)?.accessToken).toBe("token-B");
  });

  it("forgets a session once it passes the absolute lifetime", () => {
    const id = createSession(record({ createdAt: Date.now() - SESSION_TTL_MS - 1 }));

    expect(getSession(id)).toBeUndefined();
  });

  it("returns nothing for an unknown or absent id", () => {
    expect(getSession("not-a-session")).toBeUndefined();
    expect(getSession(undefined)).toBeUndefined();
  });

  it("deletes a session outright — logout is revocation, not just a cleared cookie", () => {
    const id = createSession(record());

    deleteSession(id);

    expect(getSession(id)).toBeUndefined();
  });

  it("patches a record in place when a token is refreshed", () => {
    const id = createSession(record());

    updateSession(id, { accessToken: "rotated" });

    expect(getSession(id)?.accessToken).toBe("rotated");
    expect(getSession(id)?.tenantSlug).toBe("acme"); // untouched fields survive
  });
});

describe("lib/session — refresh timing", () => {
  it("refreshes slightly before expiry rather than exactly at it", () => {
    const now = Date.now();
    const nowSeconds = Math.floor(now / 1000);

    expect(needsRefresh(record({ expiresAt: nowSeconds + 3600 }), now)).toBe(false);
    expect(needsRefresh(record({ expiresAt: nowSeconds + 10 }), now)).toBe(true); // inside the skew
    expect(needsRefresh(record({ expiresAt: nowSeconds - 1 }), now)).toBe(true);
  });
});

describe("lib/session — cookie flags", () => {
  it("writes HttpOnly, SameSite=Lax and Path=/ on the session cookie", () => {
    // Assert on the raw header, not on `response.cookies.get(...)`: the latter reports presence and
    // would pass just as happily for a cookie with no flags at all.
    const response = NextResponse.json({});
    response.cookies.set(cookieNames().session, "sid", sessionCookieOptions());

    const line = setCookieFor("tiq_session", response);

    expect(line).toMatch(/HttpOnly/);
    expect(line).toMatch(/SameSite=Lax/i); // serialized lowercase by Next — the /i is required
    expect(line).toMatch(/Path=\/(;|$)/);
  });

  it("leaves the CSRF cookie readable by script, since the client must echo it", () => {
    const response = NextResponse.json({});
    response.cookies.set(cookieNames().csrf, "token", csrfCookieOptions());

    const line = setCookieFor("tiq_csrf", response);

    expect(line).not.toMatch(/HttpOnly/);
    expect(line).toMatch(/SameSite=Lax/i);
  });

  it("omits Secure on http, and sets it with no Domain on https", async () => {
    const insecure = NextResponse.json({});
    insecure.cookies.set(cookieNames().session, "sid", sessionCookieOptions());
    expect(setCookieFor("tiq_session", insecure)).not.toMatch(/Secure/);

    await withEnv({ APP_BASE_URL: "https://app.example" }, async () => {
      const config = await import("@/lib/config");
      const session = await import("@/lib/session");
      const response = NextResponse.json({});
      response.cookies.set(config.cookieNames().session, "sid", session.sessionCookieOptions());

      const line = response.headers
        .getSetCookie()
        .find((entry) => entry.startsWith("__Host-tiq_session=")) as string;

      expect(line).toMatch(/Secure/);
      // __Host- forbids Domain, and a Domain attribute is the only way a sibling subdomain could
      // shadow a cookie this server minted.
      expect(line).not.toMatch(/Domain=/i);
    });
  });
});
