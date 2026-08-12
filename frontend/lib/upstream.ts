/**
 * Header policy for the BFF proxy (#18).
 *
 * Split out of the route handler as **pure functions** on purpose. Asserting the policy through a
 * proxied request turned out not to discriminate: in the vitest + MSW harness, headers the route
 * demonstrably holds do not all survive the interception boundary, so an integration test passes
 * whether the proxy forwards the browser's `Cookie` or not. Testing the policy directly is the only
 * way to prove it, and the route is then a thin caller of something already proven.
 *
 * Both directions are **allowlists**. A denylist enumerates the attacks someone remembered; these
 * lists are short enough to be complete by construction.
 */

/** Forwarded **to** Django. Everything else is dropped by omission. */
export const REQUEST_HEADER_ALLOWLIST = ["content-type", "accept", "content-length"] as const;

/** Forwarded **back** to the browser. */
export const RESPONSE_HEADER_ALLOWLIST = [
  "content-type",
  "cache-control",
  "retry-after",
  "x-accel-buffering", // keeps #19's SSE stream unbuffered end to end
] as const;

/**
 * Build the upstream request headers from scratch.
 *
 * Never `new Headers(incoming)`. That forwards:
 * - the browser's `Cookie` — our session cookie is meaningless to Django and must not leak onward;
 * - any client-supplied `Authorization` — which would let a caller present its own token;
 * - `X-Forwarded-*` — which Django's throttling would then trust;
 * - `Host` — which passes in dev, because `DJANGO_ALLOWED_HOSTS` contains `localhost`, and then
 *   400s every request on the first non-localhost deployment, whose tempting "fix" is
 *   `ALLOWED_HOSTS=*`.
 *
 * `content-length` **is** copied: the same bytes are forwarded, and with a streamed body undici
 * would otherwise send `Transfer-Encoding: chunked`, which Django's WSGI handler sizes from
 * `CONTENT_LENGTH` (defaulting to 0) — so an upload would arrive empty rather than failing loudly.
 */
export function buildUpstreamHeaders(incoming: Headers, accessToken: string): Headers {
  const headers = new Headers();
  for (const name of REQUEST_HEADER_ALLOWLIST) {
    const value = incoming.get(name);
    if (value !== null) headers.set(name, value);
  }
  // `set`, never `append`: appending onto a copied header would yield "Bearer a, Bearer b", which
  // Django's `get_authorization_header(...).split()` turns into a 401 on every call.
  headers.set("authorization", `Bearer ${accessToken}`);
  return headers;
}

/**
 * Filter the upstream response headers before they reach the browser.
 *
 * Deliberately dropped:
 * - `Set-Cookie` — the proxy must not become a cookie-write channel onto the app's own origin, or
 *   the double-submit scheme's assumption that only the BFF writes cookies here stops holding;
 * - `Content-Length` / `Content-Encoding` — undici has already decoded the body, so passing the
 *   upstream values through yields truncated or garbled responses;
 * - `WWW-Authenticate` — advertises the API's internal bearer scheme and makes a 401 read as "send
 *   a token" when it actually means "your BFF session ended".
 */
export function filterResponseHeaders(incoming: Headers): Headers {
  const headers = new Headers();
  for (const name of RESPONSE_HEADER_ALLOWLIST) {
    const value = incoming.get(name);
    if (value !== null) headers.set(name, value);
  }
  // Every proxied response is per-session tenant data on a *same-origin, cookie-authenticated* URL.
  // Django sets no Cache-Control on most responses, so without this the response is heuristically
  // cacheable: a shared cache (a CDN, a corporate proxy, or Next's own Data Cache in a later change)
  // could store one tenant's `/api/me` under a URL that carries no tenant in it and serve it to
  // another. `Vary: Cookie` alone would not be enough — it makes correctness depend on a cache
  // honouring it. Refusing to store the response is the control; the project's whole premise is that
  // no path can return another tenant's data.
  headers.set("cache-control", "private, no-store");
  headers.set("vary", "Cookie");
  return headers;
}
