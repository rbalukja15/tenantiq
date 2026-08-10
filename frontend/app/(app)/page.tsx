import { redirect } from "next/navigation";

import { TenantHome } from "@/app/components/TenantHome";
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

  return <TenantHome accessToken={session.accessToken} />;
}
