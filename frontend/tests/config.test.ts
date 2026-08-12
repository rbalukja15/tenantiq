import { describe, expect, it } from "vitest";

import { API_ORIGIN, APP_ORIGIN, withEnv } from "./env";

describe("lib/config", () => {
  it("reads the API and app origins from the environment", async () => {
    const { apiBaseUrl, appBaseUrl } = await import("@/lib/config");

    expect(apiBaseUrl()).toBe(API_ORIGIN);
    expect(appBaseUrl()).toBe(APP_ORIGIN);
  });

  it("throws when called, not when imported, if a variable is missing", async () => {
    // Import-time validation would throw while `next build` compiles route modules — CI builds with
    // no environment set — turning a missing variable into an opaque build failure. So importing the
    // module must succeed and only the *call* may fail.
    await withEnv({ API_BASE_URL: "" }, async () => {
      const config = await import("@/lib/config");

      expect(() => config.apiBaseUrl()).toThrow(/API_BASE_URL/);
    });
  });

  it("marks cookies Secure and __Host- prefixed only on an https deployment", async () => {
    await withEnv({ APP_BASE_URL: "https://app.example" }, async () => {
      const { cookieSecure, cookieNames } = await import("@/lib/config");

      expect(cookieSecure()).toBe(true);
      expect(cookieNames().session).toBe("__Host-tiq_session");
      expect(cookieNames().csrf).toBe("__Host-tiq_csrf");
    });
  });

  it("drops the __Host- prefix on plain http, because the prefix mandates Secure", async () => {
    // Safari refuses a Secure cookie on http://localhost outright, so keeping the prefix in local
    // development would break sign-in on that browser only — the worst kind of bug to discover late.
    const { cookieSecure, cookieNames } = await import("@/lib/config");

    expect(cookieSecure()).toBe(false);
    expect(cookieNames().session).toBe("tiq_session");
  });
});
