/**
 * Rendering machine values as something a person reads (#20).
 *
 * Separate from `lib/documents.ts` on purpose: that module is data access, this one is presentation.
 * Both are pure and both are tested directly, which is what lets the components that use them stay
 * assertions about behaviour rather than about string formatting.
 */

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
