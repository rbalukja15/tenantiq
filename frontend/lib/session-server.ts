/**
 * The **only** file in the app permitted to import `next/headers` (#18).
 *
 * `cookies()` throws `cookies was called outside a request scope` under vitest, and the only way
 * around that is to mock the module — which ADR-0013 §5 rules out, because module patching is
 * exactly what stops a test from exercising real code. So the untestable surface is quarantined
 * here, in one function with no logic in it, and everything else (route handlers, the proxy, the
 * CSRF and session helpers) stays a plain function of a `NextRequest`.
 *
 * If this file ever grows a second responsibility, that responsibility has become untestable. Move
 * it out instead.
 */

import { cookies } from "next/headers";

import { cookieNames } from "@/lib/config";
import { getSession, type SessionRecord } from "@/lib/session";

/** Resolve the caller's session inside a Server Component. */
export async function getSessionFromCookieStore(): Promise<SessionRecord | undefined> {
  const store = await cookies();
  return getSession(store.get(cookieNames().session)?.value);
}
