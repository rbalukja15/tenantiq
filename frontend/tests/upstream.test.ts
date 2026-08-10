import { describe, expect, it } from "vitest";

import { buildUpstreamHeaders, filterResponseHeaders } from "@/lib/upstream";

/** Everything a hostile or merely noisy browser might send at the proxy. */
function browserHeaders(): Headers {
  return new Headers({
    host: "evil.example",
    cookie: "tiq_session=stolen; other=1",
    authorization: "Bearer attacker-token",
    "x-forwarded-for": "1.2.3.4",
    "x-real-ip": "1.2.3.4",
    connection: "close",
    "accept-encoding": "gzip",
    referer: "https://evil.example/",
    "content-type": "application/json",
    accept: "application/json",
    "content-length": "13",
  });
}

describe("buildUpstreamHeaders", () => {
  it("sends exactly the allowlist plus our own bearer — nothing else", () => {
    // Exact equality, not a set of `.not.toBe` spot-checks: a header added to the forwarding path
    // later must break this test rather than slip through because nobody thought to assert on it.
    const headers = buildUpstreamHeaders(browserHeaders(), "real-token");

    expect([...headers].map(([name]) => name).sort()).toEqual([
      "accept",
      "authorization",
      "content-length",
      "content-type",
    ]);
  });

  it("replaces any client-supplied Authorization with the session's own token", () => {
    const headers = buildUpstreamHeaders(browserHeaders(), "real-token");

    // One value, not "Bearer attacker-token, Bearer real-token" — an appended header would make
    // Django's `get_authorization_header(...).split()` 401 every single call.
    expect(headers.get("authorization")).toBe("Bearer real-token");
    expect([...headers].filter(([name]) => name === "authorization")).toHaveLength(1);
  });

  it("never forwards the browser's cookies to the API", () => {
    // Our session cookie means nothing to Django, and forwarding it would hand the API a credential
    // it has no business seeing.
    expect(buildUpstreamHeaders(browserHeaders(), "t").get("cookie")).toBeNull();
  });

  it("never forwards Host or the forwarding headers", () => {
    // A forwarded Host passes in dev (DJANGO_ALLOWED_HOSTS contains localhost) and 400s every
    // request on the first real deployment. X-Forwarded-For would be trusted by DRF's throttling.
    const headers = buildUpstreamHeaders(browserHeaders(), "t");

    expect(headers.get("host")).toBeNull();
    expect(headers.get("x-forwarded-for")).toBeNull();
    expect(headers.get("x-real-ip")).toBeNull();
    expect(headers.get("connection")).toBeNull();
  });

  it("preserves the content type, so a multipart boundary survives for #20", () => {
    const incoming = new Headers({
      "content-type": 'multipart/form-data; boundary="----abc123"',
    });

    expect(buildUpstreamHeaders(incoming, "t").get("content-type")).toBe(
      'multipart/form-data; boundary="----abc123"',
    );
  });

  it("copies content-length, because Django sizes the body from it", () => {
    // With a streamed body undici would emit Transfer-Encoding: chunked, and Django's WSGI handler
    // reads CONTENT_LENGTH (defaulting to 0) — so dropping this makes an upload arrive *empty*
    // rather than failing loudly.
    expect(buildUpstreamHeaders(new Headers({ "content-length": "42" }), "t").get("content-length"))
      .toBe("42");
  });

  it("omits an allowlisted header that was not sent, rather than sending it empty", () => {
    expect(buildUpstreamHeaders(new Headers(), "t").get("accept")).toBeNull();
  });
});

describe("filterResponseHeaders", () => {
  function upstreamResponseHeaders(): Headers {
    return new Headers({
      "content-type": "application/json",
      "cache-control": "no-cache",
      "retry-after": "5",
      "x-accel-buffering": "no",
      "set-cookie": "evil=1; Path=/",
      "content-length": "9999",
      "content-encoding": "gzip",
      "www-authenticate": 'Bearer realm="api"',
      "x-internal-detail": "leaky",
    });
  }

  it("returns exactly the allowlist", () => {
    const headers = filterResponseHeaders(upstreamResponseHeaders());

    expect([...headers].map(([name]) => name).sort()).toEqual([
      "cache-control",
      "content-type",
      "retry-after",
      "x-accel-buffering",
    ]);
  });

  it("refuses to relay a Set-Cookie from the API onto our own origin", () => {
    // Otherwise the proxy becomes a cookie-write channel, and the double-submit CSRF scheme's
    // assumption that only the BFF writes cookies on this origin stops holding.
    expect(filterResponseHeaders(upstreamResponseHeaders()).getSetCookie()).toEqual([]);
  });

  it("drops Content-Length and Content-Encoding, which undici has already applied", () => {
    const headers = filterResponseHeaders(upstreamResponseHeaders());

    expect(headers.get("content-length")).toBeNull();
    expect(headers.get("content-encoding")).toBeNull();
  });

  it("does not advertise the API's bearer scheme to the browser", () => {
    // A WWW-Authenticate makes a 401 read as "send a token", when what it means here is "your BFF
    // session ended" — and it tells the browser about an authentication scheme it cannot use.
    expect(filterResponseHeaders(upstreamResponseHeaders()).get("www-authenticate")).toBeNull();
  });

  it("keeps the SSE anti-buffering header that #19 depends on", () => {
    expect(filterResponseHeaders(upstreamResponseHeaders()).get("x-accel-buffering")).toBe("no");
  });
});
