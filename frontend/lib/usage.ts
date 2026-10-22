/**
 * The usage dashboard's data and its date ranges (#27, ADR-0025).
 *
 * Every date here is a UTC calendar day written `YYYY-MM-DD`, because that is what the API counts
 * in (`GET /api/usage/daily`) and what an `<input type="date">` submits. Days are compared as
 * strings, which is sound for this one fixed-width format and keeps timezones out of it entirely:
 * the reader's timezone is never consulted, so the server and the browser can never disagree about
 * what "today" or "the last 7 days" means.
 */

/** One day's or one range's numbers, as `GET /api/usage/daily` returns them. */
export type UsageNumbers = {
  /** Answers that reached the model. A refusal never does, so it is not here. */
  requests: number;
  input_tokens: number;
  output_tokens: number;
  /** A decimal string, never a JSON number, so the money stays exact (ADR-0012). */
  estimated_cost_usd: string;
};

export type UsageDay = UsageNumbers & { date: string };

export type DailyUsage = {
  /** The range the API reported, which can end earlier than asked: never after today. */
  start: string;
  end: string;
  timezone: "UTC";
  currency: "USD";
  totals: UsageNumbers;
  /** Every day from `start` to `end`, zeros included. */
  days: UsageDay[];
};

export type DateRange = { start: string; end: string };

export type Preset = DateRange & { label: string };

/** Today in UTC, as the API counts days. */
export function utcToday(now: Date = new Date()): string {
  return now.toISOString().slice(0, 10);
}

/** `day` moved by `days`, which may be negative. */
export function addDays(day: string, days: number): string {
  const moved = new Date(`${day}T00:00:00Z`);
  moved.setUTCDate(moved.getUTCDate() + days);
  return moved.toISOString().slice(0, 10);
}

/**
 * The ranges a reader reaches for first, ending today. "This month" and "This year" start on the
 * 1st, so on the 1st itself they are one day long, which is what they are.
 */
export function presets(today: string): Preset[] {
  return [
    { label: "Last 7 days", start: addDays(today, -6), end: today },
    { label: "Last 30 days", start: addDays(today, -29), end: today },
    { label: "Last 90 days", start: addDays(today, -89), end: today },
    { label: "This month", start: `${today.slice(0, 7)}-01`, end: today },
    { label: "This year", start: `${today.slice(0, 4)}-01-01`, end: today },
  ];
}

/** The URL of the dashboard showing `range`. */
export function usageHref(range: DateRange): string {
  return `/usage?${new URLSearchParams({ start: range.start, end: range.end })}`;
}
