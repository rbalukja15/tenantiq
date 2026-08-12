import { NextRequest } from "next/server";
import { describe, expect, it } from "vitest";

import { cookieNames } from "@/lib/config";
import { mintCsrfToken, requireCsrf, requireSameOrigin, timingSafeEqual } from "@/lib/csrf";

import { APP_ORIGIN } from "./env";

function request(options: {
  origin?: string | null;
  cookieToken?: string;
  headerToken?: string;
}): NextRequest {
  const headers = new Headers();
  if (options.origin !== null && options.origin !== undefined) headers.set("origin", options.origin);
  if (options.cookieToken) headers.set("cookie", `${cookieNames().csrf}=${options.cookieToken}`);
  if (options.headerToken) headers.set("x-csrf-token", options.headerToken);
  return new NextRequest(`${APP_ORIGIN}/api/documents`, { method: "POST", headers });
}

describe("lib/csrf", () => {
  it("accepts a same-origin request carrying a matching cookie and header", () => {
    const token = mintCsrfToken();

    expect(requireCsrf(request({ origin: APP_ORIGIN, cookieToken: token, headerToken: token }))).toBe(
      true,
    );
  });

  it("rejects a cross-origin request even when both halves of the token match", () => {
    // The cookie-tossing case, and the reason Origin is checked *first*. A double-submit token only
    // proves the caller could read the cookie — but cookie write scope is same-*site*, not
    // same-*origin*: a sibling subdomain (or, locally, any other port on localhost, since cookies
    // ignore ports) can plant both halves and pass the token check. Only Origin separates them.
    const token = mintCsrfToken();

    const allowed = requireCsrf(
      request({ origin: "https://evil.tenantiq.app", cookieToken: token, headerToken: token }),
    );

    expect(allowed).toBe(false);
  });

  it("rejects a request with no Origin header at all", () => {
    // Absence must fail closed. Treating a missing Origin as permission would hand every attacker a
    // one-header bypass of the only control that actually distinguishes origins.
    const token = mintCsrfToken();

    expect(requireCsrf(request({ origin: null, cookieToken: token, headerToken: token }))).toBe(
      false,
    );
  });

  it("rejects a same-origin request whose header does not match the cookie", () => {
    expect(
      requireCsrf(
        request({ origin: APP_ORIGIN, cookieToken: mintCsrfToken(), headerToken: mintCsrfToken() }),
      ),
    ).toBe(false);
  });

  it("rejects a same-origin request that carries no token at all", () => {
    expect(requireCsrf(request({ origin: APP_ORIGIN }))).toBe(false);
  });

  it("checks the origin exactly, including scheme and port", () => {
    expect(requireSameOrigin(request({ origin: APP_ORIGIN }))).toBe(true);
    expect(requireSameOrigin(request({ origin: "https://localhost:3000" }))).toBe(false);
    expect(requireSameOrigin(request({ origin: "http://localhost:3001" }))).toBe(false);
    expect(requireSameOrigin(request({ origin: "null" }))).toBe(false);
  });

  it("mints a fresh 256-bit token each time", () => {
    const a = mintCsrfToken();
    const b = mintCsrfToken();

    expect(a).toMatch(/^[0-9a-f]{64}$/);
    expect(a).not.toBe(b);
  });

  it("compares tokens without leaking length or position through early exit", () => {
    expect(timingSafeEqual("abc", "abc")).toBe(true);
    expect(timingSafeEqual("abc", "abd")).toBe(false);
    expect(timingSafeEqual("abc", "abcd")).toBe(false);
  });
});
