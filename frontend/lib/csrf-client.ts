/**
 * The browser's half of the BFF's double-submit CSRF pair (#19, ADR-0013 §1).
 *
 * The CSRF cookie is written without `httpOnly` (lib/session.ts) for exactly this: script reads it
 * and echoes it back in `x-csrf-token`, which is what proves the caller could read a cookie on our
 * own origin. Read at call time, never captured at render — a login in another tab rotates the
 * cookie, and a captured value would turn the next question into a silent 403.
 */

/**
 * Both names the cookie can have. `lib/config.ts` adds the `__Host-` prefix only when the
 * deployment is secure, and that decision is server-side — the browser cannot tell which is live, so
 * it accepts either.
 *
 * Matched **exactly**, not by suffix. `name.endsWith("tiq_csrf")` is the tempting way to cover the
 * optional prefix and it also accepts `evil_tiq_csrf`, which is a cookie a sibling subdomain can
 * write (cookie scope is same-*site*, not same-origin — the very gap ADR-0013 notes when it makes
 * the `Origin` check the load-bearing one). Exact names close it.
 */
const CSRF_COOKIE_NAMES = ["__Host-tiq_csrf", "tiq_csrf"] as const;

/**
 * @param cookies defaults to `document.cookie`. Injectable because a browser will not *accept* a
 *   `__Host-` cookie over plain http (RFC 6265bis requires `Secure`), so the prefixed name — the one
 *   that ships in production — is unreachable through `document.cookie` in a jsdom test. Reading a
 *   string keeps the parsing itself provable rather than only the half that happens to be settable.
 */
export function readCsrfToken(cookies: string = document.cookie): string | null {
  const present = new Map<string, string>();
  for (const entry of cookies.split(";")) {
    const separator = entry.indexOf("=");
    if (separator === -1) continue;
    const name = entry.slice(0, separator).trim();
    // First occurrence wins *within* a name: the browser orders more specific paths first.
    if (!present.has(name)) present.set(name, entry.slice(separator + 1).trim());
  }
  // Ordered preference, never "whichever appears first in the header". Both names can be present at
  // once — a `__Host-` cookie can only be written by this origin over https, while the unprefixed
  // name can be planted by any sibling subdomain (cookie scope is same-*site*). Scanning in header
  // order would let that plant win, and since the proxy compares against the real cookie, every
  // question would 403 for as long as it sat there: a denial of service anyone on the domain can
  // mount. Preferring the name that cannot be forged removes the choice.
  for (const name of CSRF_COOKIE_NAMES) {
    const value = present.get(name);
    if (value !== undefined) return value;
  }
  return null;
}
