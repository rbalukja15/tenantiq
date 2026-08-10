import { NextRequest } from "next/server";
import { http, HttpResponse } from "msw";
import { afterEach, describe, expect, it } from "vitest";

import { GET, POST } from "@/app/api/[...path]/route";
import { createSession, getSession, resetSessionsForTest, type SessionRecord } from "@/lib/session";

import { API_ORIGIN, APP_ORIGIN } from "./env";
import { server } from "./msw";

const ISSUER = "https://idp.test/realms/acme";
const TOKEN_ENDPOINT = `${ISSUER}/protocol/openid-connect/token`;

afterEach(() => resetSessionsForTest());

function seedSession(overrides: Partial<SessionRecord> = {}): string {
  return createSession({
    accessToken: "access-token",
    refreshToken: "refresh-token",
    expiresAt: Math.floor(Date.now() / 1000) + 3600,
    tokenEndpoint: TOKEN_ENDPOINT,
    issuer: ISSUER,
    clientId: "tenantiq-acme",
    tenantSlug: "acme",
    createdAt: Date.now(),
    ...overrides,
  });
}

/** Build a request plus the `context.params` shape Next hands a catch-all route in v16. */
function proxyCall(
  segments: string[],
  options: {
    id?: string;
    method?: string;
    origin?: string | null;
    csrf?: string;
    headers?: Record<string, string>;
    body?: string;
    search?: string;
  } = {},
) {
  const headers = new Headers(options.headers ?? {});
  const cookies: string[] = [];
  if (options.id) cookies.push(`tiq_session=${options.id}`);
  if (options.csrf !== undefined) {
    cookies.push(`tiq_csrf=${options.csrf}`);
    headers.set("x-csrf-token", options.csrf);
  }
  if (cookies.length) headers.set("cookie", cookies.join("; "));
  if (options.origin !== null) headers.set("origin", options.origin ?? APP_ORIGIN);

  const url = `${APP_ORIGIN}/api/${segments.join("/")}${options.search ?? ""}`;
  const request = new NextRequest(url, {
    method: options.method ?? "GET",
    headers,
    ...(options.body ? { body: options.body } : {}),
  });
  return [request, { params: Promise.resolve({ path: segments }) }] as const;
}

/** Capture what Django actually received. */
function mockUpstream(
  handler?: (info: { request: Request }) => Response | Promise<Response>,
): { headers: Headers; url: string; body: string }[] {
  const seen: { headers: Headers; url: string; body: string }[] = [];
  server.use(
    http.all(`${API_ORIGIN}/api/*`, async ({ request }) => {
      const body = request.body ? await request.clone().text() : "";
      seen.push({ headers: new Headers(request.headers), url: request.url, body });
      return handler ? handler({ request }) : HttpResponse.json({ ok: true });
    }),
  );
  return seen;
}

describe("proxy — path hygiene", () => {
  // Next *decodes* %2F when it populates params, so a single segment can arrive already holding
  // `//evil.example/c` or `https://evil.example/c`. Fed to `new URL(relative, base)` that resolves
  // to a different origin — and this handler would hand a valid tenant bearer token to that host.
  // A GET needs no CSRF token and is reachable by plain top-level navigation, which SameSite=Lax
  // permits. No MSW handler is registered in these cases, so any outbound call fails the test.
  it.each([
    ["a decoded scheme-relative URL", ["//evil.example/c"]],
    ["a decoded absolute URL", ["https://evil.example/c"]],
    ["traversal out of /api", ["x", "..", "..", "media", "secret.pdf"]],
    ["a smuggled query string", ["me?x=1"]],
    ["a smuggled fragment", ["me#frag"]],
    ["a CRLF injection attempt", ["me\r\nX-Evil: 1"]],
    ["an empty path", []],
  ])("rejects %s without contacting anything", async (_label, segments) => {
    const [request, context] = proxyCall(segments, { id: seedSession() });

    const response = await GET(request, context);

    expect(response.status).toBe(404);
  });

  it("passes through the API's real routes untouched", async () => {
    const seen = mockUpstream();
    const id = seedSession();

    for (const segments of [["me"], ["documents"], ["documents", "12", "retry"], ["usage"]]) {
      const [request, context] = proxyCall(segments, { id });
      expect((await GET(request, context)).status).toBe(200);
    }

    expect(seen.map((s) => new URL(s.url).pathname)).toEqual([
      "/api/me",
      "/api/documents",
      "/api/documents/12/retry",
      "/api/usage",
    ]);
  });

  it("takes the query string from the query, not from the path", async () => {
    const seen = mockUpstream();
    const [request, context] = proxyCall(["usage"], {
      id: seedSession(),
      search: "?start=2026-01-01",
    });

    await GET(request, context);

    expect(new URL(seen[0].url).searchParams.get("start")).toBe("2026-01-01");
  });
});

describe("proxy — session handling", () => {
  it("answers 401 and clears the cookie when there is no session, without calling the API", async () => {
    const [request, context] = proxyCall(["me"]);

    const response = await GET(request, context);

    expect(response.status).toBe(401);
    expect(await response.json()).toEqual({ error: "session_expired" });
    expect(response.headers.getSetCookie().some((c) => /tiq_session=.*Max-Age=0|1970/.test(c))).toBe(
      true,
    );
  });

  it("ends the session when Django rejects the token, so the next navigation redirects", async () => {
    // Passing a bare 401 through would leave a cookie naming a dead session and a page that can
    // never recover: route gating sees the cookie, allows the render, the fetch 401s, forever.
    mockUpstream(() => new HttpResponse(null, { status: 401 }));
    const id = seedSession();
    const [request, context] = proxyCall(["me"], { id });

    const response = await GET(request, context);

    expect(response.status).toBe(401);
    expect(getSession(id)).toBeUndefined();
    expect(response.headers.getSetCookie().some((c) => /tiq_session=.*Max-Age=0|1970/.test(c))).toBe(
      true,
    );
  });
});

describe("proxy — cross-tenant isolation", () => {
  it("never serves one tenant's session with another tenant's token or data", async () => {
    // The isolation proof for this data path (CLAUDE.md). Two sessions, one process, one after the
    // other: each upstream call must carry its *own* bearer and return its *own* tenant's body. A
    // cached response or a hoisted session would show up here as Globex seeing Acme's data.
    const seen = mockUpstream(({ request }) => {
      const auth = request.headers.get("authorization");
      return HttpResponse.json({ tenant: auth === "Bearer token-A" ? "Acme" : "Globex" });
    });
    const acme = seedSession({ accessToken: "token-A", tenantSlug: "acme" });
    const globex = seedSession({ accessToken: "token-B", tenantSlug: "globex" });

    const [reqA, ctxA] = proxyCall(["me"], { id: acme });
    const first = await (await GET(reqA, ctxA)).json();
    const [reqB, ctxB] = proxyCall(["me"], { id: globex });
    const second = await (await GET(reqB, ctxB)).json();

    expect(first).toEqual({ tenant: "Acme" });
    expect(second).toEqual({ tenant: "Globex" });
    expect(seen).toHaveLength(2); // two real upstream calls, not one cached answer
    expect(seen[0].headers.get("authorization")).toBe("Bearer token-A");
    expect(seen[1].headers.get("authorization")).toBe("Bearer token-B");
  });
});

describe("proxy — request headers", () => {
  it("sends only the allowlisted headers plus our own bearer", async () => {
    const seen = mockUpstream();
    const [request, context] = proxyCall(["me"], {
      id: seedSession(),
      headers: {
        host: "evil.example",
        cookie: "tiq_session=stolen; other=1",
        authorization: "Bearer attacker-token",
        "x-forwarded-for": "1.2.3.4",
        "x-real-ip": "1.2.3.4",
        connection: "close",
        accept: "application/json",
      },
    });

    await GET(request, context);

    const upstream = seen[0].headers;
    // Our token, and only ours — `set` not `append`, or Django would see "Bearer a, Bearer b" and
    // 401 every call.
    expect(upstream.get("authorization")).toBe(`Bearer access-token`);
    expect(upstream.get("accept")).toBe("application/json");
    // A forwarded Host passes in dev (ALLOWED_HOSTS has localhost) and 400s on the first real
    // deployment, whose tempting fix is ALLOWED_HOSTS=*.
    expect(upstream.get("host")).not.toBe("evil.example");
    expect(upstream.get("cookie")).toBeNull();
    expect(upstream.get("x-forwarded-for")).toBeNull();
    expect(upstream.get("x-real-ip")).toBeNull();
  });

  it("preserves the content type so a multipart boundary survives", async () => {
    const seen = mockUpstream();
    const [request, context] = proxyCall(["documents"], {
      id: seedSession(),
      method: "POST",
      csrf: "t",
      body: '{"title":"x"}',
      headers: { "content-type": "application/json" },
    });

    await POST(request, context);

    expect(seen[0].headers.get("content-type")).toBe("application/json");
  });

  it("forwards the request body byte for byte", async () => {
    const seen = mockUpstream();
    const body = '{"question":"what are the payment terms?"}';
    const [request, context] = proxyCall(["query"], {
      id: seedSession(),
      method: "POST",
      csrf: "t",
      body,
      headers: { "content-type": "application/json" },
    });

    await POST(request, context);

    expect(seen[0].body).toBe(body);
  });
});

describe("proxy — response headers", () => {
  it("passes the allowlisted headers and drops the dangerous ones", async () => {
    mockUpstream(
      () =>
        new HttpResponse('{"ok":true}', {
          headers: {
            "content-type": "application/json",
            "cache-control": "no-cache",
            "retry-after": "5",
            "x-accel-buffering": "no",
            "set-cookie": "evil=1; Path=/",
            "content-length": "9999",
            "www-authenticate": 'Bearer realm="api"',
          },
        }),
    );
    const [request, context] = proxyCall(["me"], { id: seedSession() });

    const response = await GET(request, context);

    expect(response.headers.get("content-type")).toBe("application/json");
    expect(response.headers.get("retry-after")).toBe("5");
    expect(response.headers.get("x-accel-buffering")).toBe("no");
    // The proxy must not become a cookie-write channel onto our own origin — double-submit assumes
    // only the BFF writes cookies here.
    expect(response.headers.getSetCookie()).toEqual([]);
    // undici already decoded the body, so an upstream Content-Length would truncate it.
    expect(response.headers.get("content-length")).toBeNull();
    // Advertising the internal bearer scheme makes a 401 read as "send a token" rather than
    // "your session ended".
    expect(response.headers.get("www-authenticate")).toBeNull();
  });
});

describe("proxy — CSRF", () => {
  it("allows a same-origin mutation carrying a matching token", async () => {
    mockUpstream();
    const [request, context] = proxyCall(["documents"], {
      id: seedSession(),
      method: "POST",
      csrf: "matching-token",
      body: "{}",
    });

    expect((await POST(request, context)).status).toBe(200);
  });

  it.each([
    ["a cross-origin request with a matching token pair", { origin: "https://evil.tenantiq.app" }],
    ["a request with no Origin header", { origin: null }],
  ])("rejects %s", async (_label, options) => {
    const [request, context] = proxyCall(["documents"], {
      id: seedSession(),
      method: "POST",
      csrf: "matching-token",
      body: "{}",
      ...options,
    });

    expect((await POST(request, context)).status).toBe(403);
  });

  it("rejects a mutation with no CSRF token", async () => {
    const [request, context] = proxyCall(["documents"], {
      id: seedSession(),
      method: "POST",
      body: "{}",
    });

    expect((await POST(request, context)).status).toBe(403);
  });

  it("does not require a token for a read", async () => {
    mockUpstream();
    const [request, context] = proxyCall(["me"], { id: seedSession() });

    expect((await GET(request, context)).status).toBe(200);
  });
});

describe("proxy — token refresh", () => {
  it("refreshes an expiring token and forwards the new one", async () => {
    server.use(
      http.post(TOKEN_ENDPOINT, () =>
        HttpResponse.json({ access_token: "rotated-token", expires_in: 300 }),
      ),
    );
    const seen = mockUpstream();
    const id = seedSession({ expiresAt: Math.floor(Date.now() / 1000) - 1 });
    const [request, context] = proxyCall(["me"], { id });

    await GET(request, context);

    expect(seen[0].headers.get("authorization")).toBe("Bearer rotated-token");
    expect(getSession(id)?.accessToken).toBe("rotated-token");
  });

  it("ends the session when the refresh is refused", async () => {
    server.use(
      http.post(TOKEN_ENDPOINT, () =>
        HttpResponse.json({ error: "invalid_grant" }, { status: 400 }),
      ),
    );
    const id = seedSession({ expiresAt: Math.floor(Date.now() / 1000) - 1 });
    const [request, context] = proxyCall(["me"], { id });

    const response = await GET(request, context);

    expect(response.status).toBe(401);
    expect(getSession(id)).toBeUndefined();
  });
});
