import { describe, expect, it } from "vitest";

import { formatBytes, formatTimestamp } from "@/lib/format";

describe("formatBytes", () => {
  it.each([
    [0, "0 bytes"],
    [1, "1 byte"],
    [999, "999 bytes"],
    [1000, "1.0 kB"],
    [184_320, "184.3 kB"],
    [26_214_400, "26.2 MB"],
  ])("formats %i as %s", (bytes, expected) => {
    expect(formatBytes(bytes)).toBe(expected);
  });

  it("steps up rather than rendering a thousand of the smaller unit", () => {
    // A naive loop compares before rounding and renders "1000.0 kB" here.
    expect(formatBytes(999_950)).toBe("1.0 MB");
  });

  it("says nothing rather than something wrong for a size it cannot format", () => {
    expect(formatBytes(Number.NaN)).toBe("—");
    expect(formatBytes(-1)).toBe("—");
  });
});

describe("formatTimestamp", () => {
  it("renders an instant the reader can read", () => {
    const formatted = formatTimestamp("2026-08-14T09:12:00Z");

    // Deliberately not asserted against a literal: the output is locale- and timezone-dependent, so
    // pinning it here would pin the suite to whatever CI happens to run under. What must hold is
    // that it produced *something* other than the raw ISO string it was given.
    expect(formatted).not.toBe("2026-08-14T09:12:00Z");
    expect(formatted).toMatch(/2026/);
  });

  it("shows the server's own value back rather than 'Invalid Date'", () => {
    expect(formatTimestamp("not a date")).toBe("not a date");
  });
});
