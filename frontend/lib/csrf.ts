/**
 * CSRF defence for the BFF (#18, ADR-0013 §1).
 *
 * Choosing cookies over a bearer token means owning CSRF: a bearer token has to be *added* by
 * script, so a cross-site request can never carry one, whereas a cookie is attached by the browser
 * automatically. Two controls, in this order:
 *
 * 1. **`Origin` must equal our own origin.** This is the load-bearing check. It fails closed on a
 *    missing or `null` `Origin`, and it runs before anything else.
 * 2. **A double-submit token** (cookie + matching header), as defence in depth.
 *
 * The order is not arbitrary, and the second check cannot replace the first. A double-submit token
 * only proves the caller could *read* the cookie — but cookie write scope is same-**site**, not
 * same-**origin**. A sibling subdomain can set a cookie on the parent domain, and in local
 * development cookies ignore the port entirely, so any other service on `localhost` can write both
 * halves of the pair and pass. `Origin` includes scheme, host **and port**, and no cross-origin page
 * can forge it. That is why an attacker who can plant cookies still gets a 403.
 */

import type { NextRequest } from "next/server";

import { appBaseUrl } from "@/lib/config";
import { cookieNames } from "@/lib/config";

/** Methods the browser treats as safe; they never mutate, so they are not CSRF-gated. */
const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);

export function isSafeMethod(method: string): boolean {
  return SAFE_METHODS.has(method.toUpperCase());
}

/**
 * Strict origin equality. A missing header is a **failure**, not a pass: some legacy clients omit
 * `Origin`, but treating absence as permission would hand every attacker a one-header bypass.
 */
export function requireSameOrigin(request: NextRequest): boolean {
  const origin = request.headers.get("origin");
  return origin !== null && origin === appBaseUrl();
}

/** 32 bytes of CSPRNG, hex-encoded — opaque, and safe to place in a header and a cookie. */
export function mintCsrfToken(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(32));
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

/**
 * Length-independent comparison. The token is not a secret an attacker can grind down over the
 * network, so this is closer to hygiene than a critical control — but a timing-variable compare in
 * an auth path is the kind of thing that gets copied somewhere it does matter.
 */
export function timingSafeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i += 1) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

/** The full gate for a state-changing request: same origin **and** a matching double-submit pair. */
export function requireCsrf(request: NextRequest): boolean {
  if (!requireSameOrigin(request)) return false;
  const cookie = request.cookies.get(cookieNames().csrf)?.value;
  const header = request.headers.get("x-csrf-token");
  if (!cookie || !header) return false;
  return timingSafeEqual(cookie, header);
}
