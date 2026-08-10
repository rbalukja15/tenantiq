/**
 * Route gating for the app shell (#18).
 *
 * Not to be confused with the *API* proxy at `app/api/[...path]/route.ts`, which forwards calls to
 * Django. This file is Next's request interceptor — the file formerly called `middleware.ts`, which
 * is deprecated in Next 16 (`PROXY_FILENAME = 'proxy'`) and makes `next build` warn on every run.
 * It also runs the **Node** runtime, so future hardening can reach for `node:crypto` without
 * discovering at deploy time that Edge has no such global. Do not add a `runtime` key to `config` —
 * that is a hard build error.
 *
 * **It checks cookie *presence* only, never the store.** That looks unsound — a cookie can outlive
 * the session it names — but three properties make it safe: the API proxy and the Server Component
 * both validate for real, both delete the session and clear the cookie on any failure, and the proxy
 * refreshes a live session before it lapses. So a stale cookie costs exactly one request, after
 * which it is gone and this gate redirects. Reading the session store here would couple route gating
 * to store internals and buy nothing.
 */

import { NextResponse, type NextRequest } from "next/server";

import { cookieNames } from "@/lib/config";

export function proxy(request: NextRequest): NextResponse {
  if (request.cookies.has(cookieNames().session)) return NextResponse.next();
  return NextResponse.redirect(new URL("/login", request.url));
}

export const config = {
  // `/api` is excluded because the API proxy answers with 401 JSON rather than a redirect — an XHR
  // needs a status it can act on, not an HTML login page. `/login` is excluded so the redirect
  // target is not itself gated, which would be an infinite loop.
  matcher: ["/((?!api|login|_next/static|_next/image|favicon.ico).*)"],
};
