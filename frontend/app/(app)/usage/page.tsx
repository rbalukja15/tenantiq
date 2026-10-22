import { redirect } from "next/navigation";

import { UsageDashboard } from "@/app/components/UsageDashboard";
import { ensureFreshSession } from "@/lib/session-refresh";
import { getSessionFromCookieStore } from "@/lib/session-server";

/**
 * `force-dynamic` for the same reason as the ask screen: reading cookies already marks this route
 * dynamic, and stating it keeps the reason visible rather than load-bearing-by-accident.
 */
export const dynamic = "force-dynamic";

export const metadata = { title: "Usage — TenantIQ" };

type SearchParams = { start?: string | string[]; end?: string | string[] };

/** A repeated parameter (`?start=a&start=b`) is read as its first value. */
function first(value: string | string[] | undefined): string | undefined {
  return Array.isArray(value) ? value[0] : value;
}

/**
 * The usage dashboard (#27).
 *
 * The session is checked and refreshed here exactly as on the ask screen: this page fetches on the
 * server with the session's token, so an expired one would otherwise cost a render that ends in a
 * redirect anyway. The range is read from the URL and handed down untouched; `UsageDashboard` lets
 * the API decide whether it is a range it will report.
 */
export default async function UsagePage({ searchParams }: { searchParams: Promise<SearchParams> }) {
  const session = await getSessionFromCookieStore();
  if (!session) redirect("/login?error=session_expired");

  const outcome = await ensureFreshSession(session.id, session.record);
  if (outcome.status === "expired") redirect("/login?error=session_expired");

  const { start, end } = await searchParams;
  return (
    <UsageDashboard
      accessToken={outcome.record.accessToken}
      start={first(start)}
      end={first(end)}
    />
  );
}
