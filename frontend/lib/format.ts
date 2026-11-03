/**
 * Rendering machine values as something a person reads (#20).
 *
 * Separate from `lib/documents.ts` on purpose: that module is data access, this one is presentation.
 * Both are pure and both are tested directly, which is what lets the components that use them stay
 * assertions about behaviour rather than about string formatting.
 */

import { decimalsFor } from "@/lib/chart";

const UNITS = ["kB", "MB", "GB"] as const;

/**
 * A file size a person can read.
 *
 * SI units (1 kB = 1000 bytes), matching macOS, disk vendors and the byte count the server actually
 * stored — not 1024, which would render the server's 25 MB limit as 23.8 MB and make a rejection
 * look like a bug.
 */
export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return "—";
  if (bytes < 1000) return bytes === 1 ? "1 byte" : `${bytes} bytes`;
  let value = bytes / 1000;
  let unit = 0;
  // The threshold is checked against the *rounded* value, so 999_950 bytes reads "1.0 MB" instead of
  // the "1000.0 kB" a naive loop produces.
  while (Number(value.toFixed(1)) >= 1000 && unit < UNITS.length - 1) {
    value /= 1000;
    unit += 1;
  }
  return `${value.toFixed(1)} ${UNITS[unit]}`;
}

/**
 * An ISO-8601 instant in the reader's own locale and timezone.
 *
 * Rendered only in a Client Component after mount, never during SSR: the server's timezone is not
 * the reader's, so formatting this on both sides would produce two different strings for the same
 * instant and React would report a hydration mismatch. The machine-readable value stays in the
 * `<time dateTime>` attribute, which is what the tests assert on — asserting on this output would
 * pin the suite to whatever locale CI happens to run under.
 *
 * An unparseable value returns the raw string rather than "Invalid Date": showing the server's own
 * bytes back is at least true.
 */
export function formatTimestamp(iso: string): string {
  const instant = new Date(iso);
  if (Number.isNaN(instant.getTime())) return iso;
  return instant.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

/*
 * Numbers and days on the usage dashboard (#27).
 *
 * Everything below formats in one fixed locale and, for days, in UTC. The dashboard is rendered on
 * the server and its charts hydrate in the browser, so formatting in the reader's locale would print
 * one string on each side and React would report a hydration mismatch, the problem
 * `formatTimestamp` solves by rendering only after mount. A day here is a UTC calendar day, the one
 * the API counted in, so there is no reader's timezone to honour.
 */

const COUNT = new Intl.NumberFormat("en-US");
const COMPACT = new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 });
const CENTS = new Intl.NumberFormat("en-US", {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});
const SHORT_DAY = new Intl.DateTimeFormat("en-US", {
  month: "short",
  day: "numeric",
  timeZone: "UTC",
});
const LONG_DAY = new Intl.DateTimeFormat("en-US", {
  weekday: "short",
  month: "short",
  day: "numeric",
  year: "numeric",
  timeZone: "UTC",
});

/** A count with its thousands grouped: 12,345. */
export function formatCount(value: number): string {
  return COUNT.format(value);
}

/** A count short enough for an axis tick: 500, 1.5K, 20K, 1.2M. */
export function formatCompact(value: number): string {
  return COMPACT.format(value);
}

/**
 * An estimated cost, from the API's decimal string.
 *
 * Below a dollar it is printed **exactly**, trailing zeros trimmed down to cents: one answer costs
 * a fraction of a cent, and rounding $0.0105 to "$0.01" would hide the very number the page exists
 * to show. From a dollar up, cents are enough. The string is never summed or compared here; totals
 * come from the API, which adds in decimal, so the money stays exact end to end (ADR-0012).
 */
export function formatUsd(value: string): string {
  const match = /^(\d+)\.?(\d*)$/.exec(value);
  if (!match) return value; // not a plain decimal: show the server's own bytes rather than a guess
  const [, whole, fraction] = match;
  if (Number(whole) >= 1) return `$${CENTS.format(Number(value))}`;
  return `$0.${fraction.replace(/0+$/, "").padEnd(2, "0")}`;
}

/**
 * A tick on a dollar axis. Cents at least while the step is under a dollar, more when the step
 * needs them ($0.005), and whole dollars from there up: $0, $0.01, $0.02 or $0, $5, $10.
 */
export function formatUsdTick(value: number, step: number): string {
  if (value === 0) return "$0";
  const decimals = step < 1 ? Math.max(2, decimalsFor(step)) : 0;
  const digits = { minimumFractionDigits: decimals, maximumFractionDigits: decimals };
  return `$${value.toLocaleString("en-US", digits)}`;
}

/** A UTC calendar day, `2026-10-22`, as "Oct 22", or "Thu, Oct 22, 2026" when `long`. */
export function formatDay(day: string, { long = false }: { long?: boolean } = {}): string {
  const instant = new Date(`${day}T00:00:00Z`);
  if (Number.isNaN(instant.getTime())) return day;
  return (long ? LONG_DAY : SHORT_DAY).format(instant);
}
