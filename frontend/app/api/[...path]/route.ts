/**
 * The BFF proxy: everything the browser sends to `/api/*` reaches Django through here (#18,
 * ADR-0013 §1).
 *
 * The browser holds no token, so this handler is the only place one is attached. That makes it the
 * single most security-sensitive file in the frontend, and the order of the checks below is part of
 * the design: **every gate fails closed before any outbound request is made.** A handler that
 * fetches first and validates afterwards has already leaked whatever the fetch revealed.
 *
 * Both header sets are **allowlists**, not denylists. A denylist is a list of the attacks someone
 * thought of; the headers this proxy needs are four in one direction and four in the other, so the
 * allowlist is both shorter and complete by construction.
 */

import { NextResponse, type NextRequest } from "next/server";

import { apiBaseUrl, cookieNames } from "@/lib/config";
import { isSafeMethod, requireCsrf } from "@/lib/csrf";
import {
  deleteSession,
  getSession,
  needsRefresh,
  readSessionId,
  updateSession,
  type SessionRecord,
} from "@/lib/session";
import { refreshAccessToken } from "@/lib/oidc";
import { buildUpstreamHeaders, filterResponseHeaders } from "@/lib/upstream";

/**
 * One path segment. Anything outside this set is rejected outright.
 *
 * This is the guard against a genuinely nasty escape: Next **decodes** `%2F` when it populates
 * `params`, so a single segment can arrive already containing `//evil.example/c` or
 * `https://evil.example/c`. Feed that to `new URL(relative, base)` and the result is a *different
 * origin* — at which point this handler would hand a valid tenant bearer token to an attacker's
 * server. And it needs no CSRF token to trigger, because a GET is reachable by plain top-level
 * navigation and `SameSite=Lax` sends the session cookie along.
 *
 * The charset kills traversal, encoded slashes, scheme injection, `?` and `#` in one predicate,
 * while still admitting every route the API has or is likely to grow: `me`, `documents`,
 * `documents/12/retry`, `query`, `usage`, `tenants/discovery`, and any uuid or slug.
 */
const SAFE_SEGMENT = /^[A-Za-z0-9._~-]+$/;

/**
 * `.` and `..` pass `SAFE_SEGMENT` — a dot has to be allowed for filenames, which makes a
 * dots-only segment the one traversal the charset cannot catch on its own. `x/../../media/x.pdf`
 * would otherwise resolve to `/media/x.pdf`, escaping the `/api` prefix entirely and handing a
 * tenant bearer token to a path that was never meant to be proxied. Checked separately, and
 * verified by a test that asserts nothing is fetched at all.
 */
function isTraversal(segment: string): boolean {
  return /^\.+$/.test(segment);
}

function sessionExpired(): NextResponse {
  const response = NextResponse.json({ error: "session_expired" }, { status: 401 });
  // Clearing the cookie is what stops a permanently stuck page: the next navigation hits `proxy.ts`,
  // finds no cookie, and redirects to the login form on its own.
  response.cookies.delete(cookieNames().session);
  response.cookies.delete(cookieNames().csrf);
  return response;
}

async function handle(
  request: NextRequest,
  context: { params: Promise<{ path: string[] }> },
): Promise<Response> {
  // --- 1. path hygiene, before anything else -----------------------------------------------------
  const { path } = await context.params;
  if (!path?.length || path.some((s) => !SAFE_SEGMENT.test(s) || isTraversal(s))) {
    return new NextResponse(null, { status: 404 });
  }
  // An absolute path against the base means the origin cannot move, whatever the segments contain.
  const upstream = new URL(`/api/${path.join("/")}`, apiBaseUrl());
  upstream.search = request.nextUrl.search; // the query comes from the query string, never the path

  // --- 2. session --------------------------------------------------------------------------------
  const id = readSessionId(request);
  let session: SessionRecord | undefined = getSession(id);
  if (!id || !session) return sessionExpired();

  // --- 3. CSRF on anything that mutates ----------------------------------------------------------
  if (!isSafeMethod(request.method) && !requireCsrf(request)) {
    return NextResponse.json({ error: "forbidden" }, { status: 403 });
  }

  // --- 4. token freshness ------------------------------------------------------------------------
  if (needsRefresh(session)) {
    if (!session.refreshToken) return sessionExpired();
    try {
      const refreshed = await refreshAccessToken({
        tokenEndpoint: session.tokenEndpoint,
        clientId: session.clientId,
        refreshToken: session.refreshToken,
      });
      const patch = {
        accessToken: refreshed.accessToken,
        refreshToken: refreshed.refreshToken ?? session.refreshToken,
        expiresAt: Math.floor(Date.now() / 1000) + refreshed.expiresIn,
      };
      updateSession(id, patch);
      session = { ...session, ...patch };
    } catch {
      deleteSession(id);
      return sessionExpired();
    }
  }

  // --- 5. request headers: allowlisted, built from scratch (see lib/upstream.ts) -----------------
  const headers = buildUpstreamHeaders(request.headers, session.accessToken);

  // --- 6. forward, streaming in both directions --------------------------------------------------
  let response: Response;
  try {
    response = await fetch(upstream, {
      method: request.method,
      headers,
      body: request.body,
      // `duplex: "half"` is mandatory when the body is a stream, and is missing from the DOM
      // RequestInit type — hence the local assertion rather than a global type augmentation.
      // Buffering via `await request.arrayBuffer()` instead would be a heap-exhaustion DoS.
      duplex: "half",
      redirect: "manual",
      signal: request.signal, // a client that disconnects cancels the upstream call too
      cache: "no-store",
    } as RequestInit);
  } catch {
    return NextResponse.json({ error: "upstream_unavailable" }, { status: 502 });
  }

  // --- 7. an upstream 401 ends the session -------------------------------------------------------
  if (response.status === 401) {
    deleteSession(id);
    return sessionExpired();
  }

  // --- 8. response headers: allowlisted (see lib/upstream.ts) ------------------------------------
  // The body is passed through as a stream, never buffered, so #19's SSE frames arrive unchanged.
  return new Response(response.body, {
    status: response.status,
    headers: filterResponseHeaders(response.headers),
  });
}

export const GET = handle;
export const POST = handle;
export const PUT = handle;
export const PATCH = handle;
export const DELETE = handle;
