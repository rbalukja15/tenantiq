/**
 * End the session: `POST /api/auth/logout` (#18, ADR-0013 §1).
 *
 * **Returns JSON, not a redirect**, and the client navigates. Three separate things go wrong with
 * the obvious `NextResponse.redirect(endSessionEndpoint)`:
 *
 * - a 307 from a POST re-POSTs to the target, so `/login` (a page with no POST handler) answers 405;
 * - `fetch` *follows* a cross-origin redirect itself, so the IdP's logout page would arrive as a
 *   response body the caller discards — the address bar never moves and the IdP session stays live;
 * - Keycloak 18+ renders a "are you sure" confirmation page unless `id_token_hint` is supplied.
 *
 * The endpoints come from the stored session rather than a fresh discovery call: one less network
 * hop, and logout still works when the IdP is briefly unreachable.
 */

import { NextResponse, type NextRequest } from "next/server";

import { appBaseUrl, cookieNames } from "@/lib/config";
import { requireCsrf } from "@/lib/csrf";
import { deleteSession, getSession, readSessionId } from "@/lib/session";

export async function POST(request: NextRequest): Promise<NextResponse> {
  if (!requireCsrf(request)) {
    return NextResponse.json({ error: "forbidden" }, { status: 403 });
  }

  const id = readSessionId(request);
  const session = getSession(id);
  // Real revocation: the tokens live server-side, so dropping the record actually ends the session
  // rather than merely clearing a cookie whose token may already have escaped.
  deleteSession(id);

  let location = `${appBaseUrl()}/login`;
  if (session?.endSessionEndpoint) {
    const url = new URL(session.endSessionEndpoint);
    if (session.idToken) url.searchParams.set("id_token_hint", session.idToken);
    url.searchParams.set("post_logout_redirect_uri", `${appBaseUrl()}/login`);
    location = url.toString();
  }

  const response = NextResponse.json({ location });
  response.cookies.delete(cookieNames().session);
  response.cookies.delete(cookieNames().csrf);
  return response;
}
