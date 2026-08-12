import { NextRequest } from "next/server";
import { describe, expect, it } from "vitest";

import { config, proxy } from "@/proxy";

import { APP_ORIGIN } from "./env";

function visit(path: string, options: { session?: string } = {}): NextRequest {
  const headers = new Headers();
  if (options.session) headers.set("cookie", `tiq_session=${options.session}`);
  return new NextRequest(`${APP_ORIGIN}${path}`, { headers });
}

/**
 * The matcher is a regex source string, so it can be evaluated directly. This is what actually
 * decides whether the gate runs at all — a pattern that quietly stopped matching `/` would leave the
 * app wide open while every behavioural test below still passed.
 */
function matches(path: string): boolean {
  return config.matcher.some((pattern) => new RegExp(`^${pattern}$`).test(path));
}

describe("route gating (proxy.ts)", () => {
  it("redirects an unauthenticated visitor to the login page", async () => {
    // Acceptance criterion #1, at the layer that enforces it.
    const response = proxy(visit("/"));

    expect(response.status).toBe(307);
    expect(response.headers.get("location")).toBe(`${APP_ORIGIN}/login`);
  });

  it("lets a request with a session cookie through", async () => {
    const response = proxy(visit("/", { session: "some-session-id" }));

    expect(response.headers.get("location")).toBeNull();
    // NextResponse.next() marks itself for the framework rather than redirecting.
    expect(response.headers.get("x-middleware-next")).toBe("1");
  });

  it("guards the app's pages", () => {
    expect(matches("/")).toBe(true);
    expect(matches("/documents")).toBe(true);
  });

  it("does not guard the login page, the API, or static assets", () => {
    // `/login` must be excluded or the redirect target is itself gated — an infinite loop. `/api` is
    // excluded because the API proxy answers 401 JSON: an XHR needs a status it can act on, not an
    // HTML login page.
    expect(matches("/login")).toBe(false);
    expect(matches("/api/me")).toBe(false);
    expect(matches("/api/auth/callback")).toBe(false);
    expect(matches("/_next/static/chunk.js")).toBe(false);
    expect(matches("/favicon.ico")).toBe(false);
  });
});
