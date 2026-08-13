import { afterEach, describe, expect, it } from "vitest";

import { readCsrfToken } from "@/lib/csrf-client";

/**
 * Reading the browser's half of the double-submit pair (#19).
 *
 * The CSRF cookie is deliberately not `httpOnly` (lib/session.ts) precisely so script can read it
 * and echo it in `x-csrf-token`. Read at call time rather than captured at render: a login in
 * another tab rotates the cookie, and a stale value turns the next question into a 403.
 */

function setCookies(...pairs: string[]) {
  for (const pair of pairs) document.cookie = pair;
}

afterEach(() => {
  for (const cookie of document.cookie.split(";")) {
    document.cookie = `${cookie.split("=")[0].trim()}=; max-age=0`;
  }
});

describe("readCsrfToken", () => {
  it("reads the plain cookie name used over http", () => {
    setCookies("tiq_csrf=abc123");

    expect(readCsrfToken()).toBe("abc123");
  });

  it("reads the __Host- prefixed name used over https", () => {
    // Passed as a string rather than set on `document`: a browser refuses a `__Host-` cookie without
    // `Secure` (RFC 6265bis), so jsdom on http cannot hold the very name production uses. Testing it
    // through the parameter is the only way this case is covered at all.
    expect(readCsrfToken("__Host-tiq_csrf=secure456")).toBe("secure456");
  });

  it("prefers the __Host- cookie when a plain one is also present", () => {
    // Both names can exist at once. Only this origin over https can write a `__Host-` cookie; the
    // unprefixed name can be planted by any sibling subdomain. Taking whichever appears first in the
    // header would let that plant win — and because the proxy compares against the real cookie, every
    // question would 403 for as long as it sat there. A denial of service anyone on the domain can
    // mount, fixed by never letting header order decide.
    expect(readCsrfToken("tiq_csrf=planted; __Host-tiq_csrf=real")).toBe("real");
    expect(readCsrfToken("__Host-tiq_csrf=real; tiq_csrf=planted")).toBe("real");
  });

  it("does not accept a prefixed impostor either", () => {
    expect(readCsrfToken("x__Host-tiq_csrf=planted")).toBeNull();
  });

  it("finds it among other cookies", () => {
    setCookies("other=1", "tiq_csrf=abc123", "another=2");

    expect(readCsrfToken()).toBe("abc123");
  });

  it("returns null when there is no CSRF cookie", () => {
    setCookies("other=1");

    expect(readCsrfToken()).toBeNull();
  });

  it("does not accept a cookie whose name merely ends with the real one", () => {
    // A suffix match — the obvious way to handle the optional `__Host-` prefix — would accept
    // `evil_tiq_csrf`, letting a cookie planted on a sibling subdomain supply the token.
    setCookies("evil_tiq_csrf=planted");

    expect(readCsrfToken()).toBeNull();
  });
});
