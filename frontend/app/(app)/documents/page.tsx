import { redirect } from "next/navigation";

import { DocumentsScreen } from "@/app/components/DocumentsScreen";
import { ensureFreshSession } from "@/lib/session-refresh";
import { getSessionFromCookieStore } from "@/lib/session-server";

/**
 * `force-dynamic` for the same reason as the ask screen: reading cookies already marks this route
 * dynamic, and stating it keeps the reason visible rather than load-bearing-by-accident.
 */
export const dynamic = "force-dynamic";

export const metadata = { title: "Documents — TenantIQ" };

/**
 * The document management screen (#20).
 *
 * The session is checked here, exactly as on the ask screen, even though `DocumentsScreen` fetches
 * through the API proxy which validates for real. Route gating (`proxy.ts`) only checks that the
 * cookie *exists*, so without this a cookie that outlived its session record would render the whole
 * page and then replace the list with "your session has ended" — a redirect to the login form is
 * both faster and the truth.
 *
 * `DocumentsScreen` is a Client Component rendered as a sibling and is never handed the access
 * token: props crossing a `"use client"` boundary are serialised into the RSC payload and shipped to
 * the browser, which would put a tenant bearer token in the page source and undo the BFF entirely
 * (ADR-0013 §1). It reaches the API through the same-origin proxy.
 */
export default async function DocumentsPage() {
  const session = await getSessionFromCookieStore();
  if (!session) redirect("/login?error=session_expired");

  const outcome = await ensureFreshSession(session.id, session.record);
  if (outcome.status === "expired") redirect("/login?error=session_expired");

  return <DocumentsScreen />;
}
