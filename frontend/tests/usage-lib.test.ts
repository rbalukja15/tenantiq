import { describe, expect, it } from "vitest";

import { decimalsFor, labelledIndexes, niceScale } from "@/lib/chart";
import { formatCompact, formatCount, formatDay, formatUsd, formatUsdTick } from "@/lib/format";
import { addDays, presets, usageHref, utcToday } from "@/lib/usage";

/**
 * The usage dashboard's pure parts (#27): its ranges, its axes and how it prints money.
 *
 * These carry the decisions. The components are thin over them, and jsdom does no layout, so a
 * claim like "the labels never touch" can only be pinned where it is computed.
 */

describe("ranges", () => {
  it("counts today in UTC, whatever the clock's own timezone", () => {
    // 23:30 on the 22nd in New York is already the 23rd in UTC, which is the day the API counts in.
    expect(utcToday(new Date("2026-10-22T23:30:00-04:00"))).toBe("2026-10-23");
  });

  it("moves across a month end and a leap day", () => {
    expect(addDays("2026-10-01", -1)).toBe("2026-09-30");
    expect(addDays("2024-02-28", 1)).toBe("2024-02-29");
    expect(addDays("2026-12-31", 1)).toBe("2027-01-01");
  });

  it("offers the ranges a reader reaches for, every one ending today", () => {
    expect(presets("2026-10-22")).toEqual([
      { label: "Last 7 days", start: "2026-10-16", end: "2026-10-22" },
      { label: "Last 30 days", start: "2026-09-23", end: "2026-10-22" },
      { label: "Last 90 days", start: "2026-07-25", end: "2026-10-22" },
      { label: "This month", start: "2026-10-01", end: "2026-10-22" },
      { label: "This year", start: "2026-01-01", end: "2026-10-22" },
    ]);
  });

  it("keeps the longest preset inside the year the API will report", () => {
    // The API refuses more than 366 days. "This year" on New Year's Eve of a leap year is exactly
    // that, and the arithmetic below is the API's: both ends included.
    const longest = presets("2024-12-31").find((preset) => preset.label === "This year")!;
    const days =
      (Date.parse(`${longest.end}T00:00:00Z`) - Date.parse(`${longest.start}T00:00:00Z`)) /
        86_400_000 +
      1;
    expect(days).toBe(366);
  });

  it("links a range as the query string the form itself would submit", () => {
    expect(usageHref({ start: "2026-09-23", end: "2026-10-22" })).toBe(
      "/usage?start=2026-09-23&end=2026-10-22",
    );
  });
});

describe("niceScale", () => {
  it.each([
    // max, whole, ticks
    [7, true, [0, 2, 4, 6, 8]],
    [3, true, [0, 1, 2, 3]],
    [1, true, [0, 1]], // never a step of half a query
    [42_000, true, [0, 10_000, 20_000, 30_000, 40_000, 50_000]], // to 50K, not 60K
    [0.0315, false, [0, 0.01, 0.02, 0.03, 0.04]],
    [0.3, false, [0, 0.1, 0.2, 0.3]], // already round: no empty band on top
    [0.000126, false, [0, 0.00005, 0.0001, 0.00015]],
  ])("puts %d on round ticks", (max, whole, ticks) => {
    expect(niceScale(max, { whole }).ticks).toEqual(ticks);
  });

  it("always reaches the largest value, in at most five intervals", () => {
    for (const max of [1, 2, 9, 11, 50, 99, 101, 0.07, 0.5, 1234.5, 40_517]) {
      const { top, ticks } = niceScale(max);
      expect(top).toBeGreaterThanOrEqual(max);
      expect(ticks.length - 1).toBeLessThanOrEqual(5);
    }
  });

  it("gives an empty series an axis rather than a division by zero", () => {
    expect(niceScale(0)).toEqual({ top: 1, step: 1, ticks: [0, 1] });
  });

  it("says how many decimals a step needs", () => {
    expect([decimalsFor(500), decimalsFor(1), decimalsFor(0.1), decimalsFor(0.05)]).toEqual([
      0, 0, 1, 2,
    ]);
    expect(decimalsFor(0.001)).toBe(3); // Math.log10(0.001) is not exactly -3
  });
});

describe("labelledIndexes", () => {
  it("labels every day when they fit", () => {
    expect(labelledIndexes(7, 8.9)).toEqual([0, 1, 2, 3, 4, 5, 6]);
  });

  it("labels every so many days, and always the last one", () => {
    expect(labelledIndexes(30, 7)).toEqual([0, 5, 10, 15, 20, 29]);
  });

  it("never sets a label closer to the last than the step it keeps elsewhere", () => {
    // The last label hangs left from its day, so a label just before it would be drawn into it.
    for (const [count, room] of [
      [30, 7],
      [31, 4],
      [90, 9],
      [366, 12],
      [366, 3],
    ]) {
      const picked = labelledIndexes(count, room);
      const steps = picked.slice(1).map((index, n) => index - picked[n]);
      const usual = steps[0];
      expect(steps[steps.length - 1]).toBeGreaterThanOrEqual(usual);
      expect(picked.length).toBeLessThanOrEqual(Math.max(2, Math.floor(room)));
      expect(picked[0]).toBe(0);
      expect(picked[picked.length - 1]).toBe(count - 1);
    }
  });

  it("labels a single day, and nothing at all", () => {
    expect(labelledIndexes(1, 9)).toEqual([0]);
    expect(labelledIndexes(0, 9)).toEqual([]);
  });
});

describe("formatUsd", () => {
  it.each([
    ["0.000000", "$0.00"],
    ["0.010500", "$0.0105"], // exact: a cent and a half of a tenth is the whole point of the page
    ["0.000126", "$0.000126"],
    ["0.500000", "$0.50"],
    ["12.345600", "$12.35"],
    ["1234.500000", "$1,234.50"],
  ])("prints %s as %s", (value, expected) => {
    expect(formatUsd(value)).toBe(expected);
  });

  it("shows the server's own value back rather than a guess", () => {
    expect(formatUsd("NaN")).toBe("NaN");
    expect(formatUsd("-1.00")).toBe("-1.00");
  });
});

describe("formatUsdTick", () => {
  it.each([
    [0, 0.01, "$0"],
    [0.01, 0.01, "$0.01"],
    [0.005, 0.005, "$0.005"],
    [0.5, 0.5, "$0.50"],
    [10, 5, "$10"],
    [2000, 1000, "$2,000"],
  ])("prints %d on a step of %d as %s", (value, step, expected) => {
    expect(formatUsdTick(value, step)).toBe(expected);
  });
});

describe("counts and days", () => {
  it("groups a count, and compacts it for an axis", () => {
    expect(formatCount(12_345)).toBe("12,345");
    expect([
      formatCompact(0),
      formatCompact(500),
      formatCompact(1500),
      formatCompact(20_000),
    ]).toEqual(["0", "500", "1.5K", "20K"]);
  });

  it("prints a day as the UTC calendar day it is, not the day before in the west", () => {
    // Parsed naively, "2026-10-22" is midnight UTC, which a reader in California would see as the
    // 21st. These are formatted in UTC because they are UTC days.
    expect(formatDay("2026-10-22")).toBe("Oct 22");
    expect(formatDay("2026-10-22", { long: true })).toBe("Thu, Oct 22, 2026");
  });

  it("shows a day it cannot read as it was given", () => {
    expect(formatDay("someday")).toBe("someday");
  });
});
