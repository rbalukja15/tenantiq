import Link from "next/link";
import { redirect } from "next/navigation";
import type { ReactNode } from "react";

import { type ChartSeries, UsageChart } from "@/app/components/UsageChart";
import { Button } from "@/app/components/ui/Button";
import { Callout } from "@/app/components/ui/Callout";
import { DataTable } from "@/app/components/ui/DataTable";
import { EmptyState } from "@/app/components/ui/EmptyState";
import { TextField } from "@/app/components/ui/TextField";
import { apiBaseUrl } from "@/lib/config";
import { formatCount, formatDay, formatUsd } from "@/lib/format";
import {
  type DailyUsage,
  type DateRange,
  type UsageDay,
  presets,
  usageHref,
  utcToday,
} from "@/lib/usage";

import styles from "./UsageDashboard.module.css";

type Props = {
  accessToken: string;
  /** The range asked for in the URL, passed through as given; the API is what validates it. */
  start?: string;
  end?: string;
  /** Injectable so a test can pin "today"; in the app it is today in UTC. */
  today?: string;
};

/**
 * The usage dashboard: what this workspace's answers have cost, day by day (#27, ADR-0025).
 *
 * A Server Component that **calls Django directly with the token passed in**, for the reasons
 * `TenantHome` gives: a server-side fetch of the app's own `/api/*` proxy would carry no cookie and
 * answer 401, and `cache: "no-store"` keeps one tenant's numbers out of any shared cache entry. The
 * token never reaches the browser; the charts are Client Components handed only the numbers.
 *
 * The date range lives in the URL (`/usage?start=…&end=…`), so the filter is a plain GET form and a
 * row of links. Every view of a range is a link that can be shared or reloaded, the back button
 * works, and the page needs no client-side data fetching at all. The numbers are the tenant's own,
 * through the tenant-scoped API, so there is nothing here to gate beyond being signed in.
 */
export async function UsageDashboard({ accessToken, start, end, today = utcToday() }: Props) {
  const url = new URL("/api/usage/daily", apiBaseUrl());
  if (start) url.searchParams.set("start", start);
  if (end) url.searchParams.set("end", end);

  const response = await fetch(url, {
    headers: { authorization: `Bearer ${accessToken}` },
    cache: "no-store",
  });

  if (response.status === 401) redirect("/login?error=session_expired");

  if (!response.ok) {
    return (
      <Screen today={today} asked={{ start, end }}>
        {response.status === 400 ? (
          // The API's own wording names query parameters; this says what to change in the form.
          <Callout tone="warning">
            Those dates can&apos;t be shown. Pick a start no later than the end or today, and no
            more than a year before the end.
          </Callout>
        ) : (
          // Generic on purpose: the reason an API call failed stays on the server.
          <Callout tone="error">Could not load usage.</Callout>
        )}
      </Screen>
    );
  }

  const usage = (await response.json()) as DailyUsage;
  const { totals, days } = usage;
  const range = rangeOf(usage);

  return (
    <Screen today={today} asked={{ start, end }} shown={usage}>
      <dl className={styles.tiles} aria-label={`Totals, ${range}`}>
        <div className={styles.tile}>
          <dt>Answers</dt>
          <dd>{formatCount(totals.requests)}</dd>
        </div>
        <div className={styles.tile}>
          <dt>Input tokens</dt>
          <dd>{formatCount(totals.input_tokens)}</dd>
        </div>
        <div className={styles.tile}>
          <dt>Output tokens</dt>
          <dd>{formatCount(totals.output_tokens)}</dd>
        </div>
        <div className={styles.tile}>
          <dt>Estimated cost</dt>
          <dd>{formatUsd(totals.estimated_cost_usd)}</dd>
        </div>
      </dl>

      {totals.requests === 0 ? (
        <EmptyState
          title="No answers in this range"
          action={
            <Link className={styles.action} href="/">
              Ask a question
            </Link>
          }
        >
          Usage appears here once someone asks a question your documents can answer. A refusal never
          reaches the model, so it costs nothing and is not counted.
        </EmptyState>
      ) : (
        <>
          <div className={styles.charts}>
            <UsageChart
              title="Answers per day"
              summary={answersSummary(usage)}
              dates={days.map((day) => day.date)}
              series={[countSeries("Answers", days, (day) => day.requests)]}
              unit="count"
            />
            <UsageChart
              title="Tokens per day"
              summary={tokensSummary(usage)}
              dates={days.map((day) => day.date)}
              series={[
                countSeries("Input", days, (day) => day.input_tokens),
                countSeries("Output", days, (day) => day.output_tokens),
              ]}
              unit="count"
            />
            <UsageChart
              title="Estimated cost per day"
              summary={costSummary(usage)}
              dates={days.map((day) => day.date)}
              series={[
                {
                  label: "Estimated cost",
                  // Geometry only. Every printed amount comes from the exact string instead.
                  values: days.map((day) => Number(day.estimated_cost_usd)),
                  display: days.map((day) => formatUsd(day.estimated_cost_usd)),
                },
              ]}
              unit="usd"
            />
          </div>

          <details className={styles.table}>
            <summary>Every day as a table</summary>
            <DataTable
              caption={`Usage per day, ${range}`}
              head={
                <tr>
                  <th scope="col">Day (UTC)</th>
                  <th scope="col" data-numeric>
                    Answers
                  </th>
                  <th scope="col" data-numeric>
                    Input tokens
                  </th>
                  <th scope="col" data-numeric>
                    Output tokens
                  </th>
                  <th scope="col" data-numeric>
                    Estimated cost
                  </th>
                </tr>
              }
            >
              {/* Most recent first: the day a reader is most likely looking for is today. */}
              {[...days].reverse().map((day) => (
                <tr key={day.date}>
                  <th scope="row">
                    <time dateTime={day.date}>{longDay(day.date)}</time>
                  </th>
                  <td data-numeric>{formatCount(day.requests)}</td>
                  <td data-numeric>{formatCount(day.input_tokens)}</td>
                  <td data-numeric>{formatCount(day.output_tokens)}</td>
                  <td data-numeric>{formatUsd(day.estimated_cost_usd)}</td>
                </tr>
              ))}
            </DataTable>
          </details>
        </>
      )}
    </Screen>
  );
}

/**
 * The frame every outcome shares: the heading, then the range filter in one row above everything it
 * scopes, so a rejected range can be corrected where it was entered.
 */
function Screen({
  today,
  asked,
  shown,
  children,
}: {
  today: string;
  asked: Partial<DateRange>;
  /** The range the API reported, when it reported one. It wins over what was asked for, because
   *  the API may have moved a future end back to today. */
  shown?: DateRange;
  children: ReactNode;
}) {
  const form = shown ?? asked;
  return (
    <section className={styles.screen}>
      <header className={styles.head}>
        <h1>Usage</h1>
        <p className={styles.lede}>
          What this workspace&apos;s answers have cost, day by day. Tokens are estimated from the
          text of each prompt and answer, and each answer is priced at its model&apos;s rate when it
          was given, so these are estimates, not an invoice. Days are UTC.
        </p>
      </header>

      <div className={styles.filters}>
        <nav aria-label="Date range" className={styles.presets}>
          {presets(today).map((preset) => {
            const current = shown?.start === preset.start && shown?.end === preset.end;
            return (
              <Link
                key={preset.label}
                href={usageHref(preset)}
                className={current ? `${styles.preset} ${styles.current}` : styles.preset}
                aria-current={current ? "true" : undefined}
              >
                {preset.label}
              </Link>
            );
          })}
        </nav>
        <form className={styles.custom} method="get" action="/usage">
          <TextField label="From" type="date" name="start" defaultValue={form.start} max={today} />
          <TextField label="To" type="date" name="end" defaultValue={form.end} max={today} />
          <Button type="submit" variant="quiet">
            Show
          </Button>
        </form>
      </div>

      {children}
    </section>
  );
}

/** A series of whole numbers, printed in the tooltip as they are. */
function countSeries(
  label: string,
  days: UsageDay[],
  pick: (day: UsageDay) => number,
): ChartSeries {
  const values = days.map(pick);
  return { label, values, display: values.map(formatCount) };
}

function answersSummary(usage: DailyUsage): string {
  const busiest = usage.days.reduce((best, day) => (day.requests > best.requests ? day : best));
  return (
    `Answers per day, ${rangeOf(usage)}: ${formatCount(usage.totals.requests)} in total, ` +
    `the most on ${longDay(busiest.date)} with ${formatCount(busiest.requests)}.`
  );
}

function tokensSummary(usage: DailyUsage): string {
  const { input_tokens, output_tokens } = usage.totals;
  return (
    `Input and output tokens per day, ${rangeOf(usage)}: ${formatCount(input_tokens)} input ` +
    `and ${formatCount(output_tokens)} output in total.`
  );
}

function costSummary(usage: DailyUsage): string {
  // Comparing as numbers only chooses which day to name; the amounts printed are the exact strings.
  const dearest = usage.days.reduce((best, day) =>
    Number(day.estimated_cost_usd) > Number(best.estimated_cost_usd) ? day : best,
  );
  return (
    `Estimated cost per day, ${rangeOf(usage)}: ${formatUsd(usage.totals.estimated_cost_usd)} ` +
    `in total, the most on ${longDay(dearest.date)} at ${formatUsd(dearest.estimated_cost_usd)}.`
  );
}

/** The range the API reported, as the page names it: "Fri, Oct 16, 2026 to Thu, Oct 22, 2026". */
function rangeOf(range: DateRange): string {
  return `${longDay(range.start)} to ${longDay(range.end)}`;
}

/** A day as the page names it in a sentence or a table row: "Thu, Oct 22, 2026". */
function longDay(day: string): string {
  return formatDay(day, { long: true });
}
