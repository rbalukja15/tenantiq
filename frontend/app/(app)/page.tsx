import { redirect } from "next/navigation";

import { TenantHome } from "@/app/components/TenantHome";
import { ensureFreshSession } from "@/lib/session-refresh";
import { getSessionFromCookieStore } from "@/lib/session-server";

/**
 * `force-dynamic` guards against a real build failure, not a hypothetical one: in Next 16 a Server
 * Component whose only async work is a `fetch` is prerender-eligible, so `next build` would *execute*
 * that fetch — and CI runs the frontend build with no Django running, turning this page into
 * `ECONNREFUSED` and failing the job. Reading cookies marks the route dynamic anyway; the explicit
 * export makes the reason visible instead of load-bearing-by-accident.
 */
export const dynamic = "force-dynamic";

export default async function Home() {
  const session = await getSessionFromCookieStore();
  // `proxy.ts` already redirects when the cookie is absent; this catches the other case — a cookie
  // that outlived its session record — which route gating deliberately does not check.
  if (!session) redirect("/login?error=session_expired");

  // Refresh here too, not only in the API proxy. Keycloak's default access-token lifespan is five
  // minutes, and this page is the one thing a user actually loads: without this, reloading a tab a
  // few minutes after signing in sends an expired bearer to Django, gets a 401, and bounces the user
  // back to the login form — while the BFF is holding a refresh token that would have renewed it.
  const outcome = await ensureFreshSession(session.id, session.record);
  if (outcome.status === "expired") redirect("/login?error=session_expired");

  // On `unavailable` (the IdP is briefly unreachable) we deliberately proceed with the token we
  // already hold rather than logging the user out: it may still be valid, and if it is not, Django
  // answers 401 and `TenantHome` redirects. Degrading to one failed render beats ending the session.
  return <TenantHome accessToken={outcome.record.accessToken} />;
}
