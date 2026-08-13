import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

/**
 * The design system's only genuinely automatable guarantee (#74, ADR-0014).
 *
 * Most "CSS tests" are theatre: jsdom does not apply a CSS Module, so asserting that a component got
 * a class name proves nothing about how it looks. These tests instead read the stylesheet as data and
 * check the two properties that *are* objective — that both themes define the same tokens, and that
 * text meets WCAG AA against the surface it sits on.
 *
 * That catches the regression this system is most likely to suffer: someone nudges a colour, it looks
 * fine on their monitor in their theme, and body text drops below legible in the other one.
 */

// Resolved from the vitest root (the `frontend` directory), because `import.meta.url` is not a
// file: URL under the jsdom environment.
const CSS = readFileSync(resolve(process.cwd(), "app/global.css"), "utf8");

/** WCAG 2.1 relative luminance (https://www.w3.org/TR/WCAG21/#dfn-relative-luminance). */
function luminance(hex: string): number {
  const value = hex.replace("#", "");
  const full =
    value.length === 3
      ? value
          .split("")
          .map((c) => c + c)
          .join("")
      : value;
  const channels = [0, 2, 4].map((i) => parseInt(full.slice(i, i + 2), 16) / 255);
  const [r, g, b] = channels.map((c) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4));
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function contrast(a: string, b: string): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

/**
 * Pull one palette out of the stylesheet.
 *
 * `light` is the `:root {...}` block; `dark` is the `:root {...}` nested in the
 * prefers-color-scheme media query. Deliberately a dumb parser over the real file rather than a
 * duplicated table of colours in the test — a copy would drift and then verify nothing.
 */
function palette(theme: "light" | "dark"): Record<string, string> {
  const darkAt = CSS.indexOf("@media (prefers-color-scheme: dark)");
  expect(darkAt, "no dark palette found in global.css").toBeGreaterThan(-1);

  const region = theme === "light" ? CSS.slice(0, darkAt) : CSS.slice(darkAt);
  const start = region.indexOf(":root");
  const block = region.slice(start, region.indexOf("}", start));

  const tokens: Record<string, string> = {};
  for (const [, name, value] of block.matchAll(/(--[a-z0-9-]+)\s*:\s*([^;]+);/g)) {
    tokens[name] = value.trim();
  }
  return tokens;
}

const light = palette("light");
const dark = palette("dark");

const isColour = (value: string) => /^#[0-9a-f]{3,8}$/i.test(value);
const colourNames = (p: Record<string, string>) =>
  Object.entries(p)
    .filter(([, v]) => isColour(v))
    .map(([k]) => k)
    .sort();

describe("token parity", () => {
  it("defines the same colour tokens in both themes", () => {
    // A token present in light and missing in dark resolves to nothing, and the element renders
    // transparent — invisible to anyone developing in light mode.
    expect(colourNames(dark)).toEqual(colourNames(light));
  });

  it("actually found a palette to check", () => {
    // Guards the parser itself: if the file were restructured so the regex matched nothing, every
    // contrast assertion below would vacuously pass over an empty set.
    expect(colourNames(light).length).toBeGreaterThanOrEqual(14);
  });
});

/** Text pairs that must clear AA for normal text (4.5:1). */
const TEXT_PAIRS: [string, string][] = [
  ["--ink", "--paper"],
  ["--ink", "--surface"],
  ["--ink", "--sunken"],
  ["--ink-muted", "--paper"],
  ["--ink-muted", "--surface"],
  ["--ink-muted", "--sunken"],
  ["--accent-ink", "--accent-wash"], // citation chip, resting
  ["--on-accent", "--accent"], // filled button, pressed chip
  ["--ok", "--ok-wash"], // status pills
  ["--warn", "--warn-wash"],
  ["--crit", "--crit-wash"],
];

/**
 * Non-text pairs that must clear 3:1 (WCAG 1.4.11, non-text contrast).
 *
 * `--rule` and `--rule-soft` are deliberately absent: the rule applies to boundaries needed to
 * *identify* a component, not to decorative card edges and row dividers, and holding a hairline to
 * 3:1 would turn the whole interface into a grid of hard lines. Control boundaries use
 * `--rule-strong`, which is checked here — that split is why this list exists at all.
 */
const UI_PAIRS: [string, string][] = [
  ["--accent", "--paper"], // focus ring, active nav
  ["--accent", "--surface"],
  ["--rule-strong", "--surface"], // a text input's edge, on a card
  ["--rule-strong", "--paper"], // and on the page ground
];

describe.each(["light", "dark"] as const)("%s theme contrast", (theme) => {
  const tokens = theme === "light" ? light : dark;

  it.each(TEXT_PAIRS)("%s on %s meets AA for text (4.5:1)", (fg, bg) => {
    const ratio = contrast(tokens[fg], tokens[bg]);
    expect(
      ratio,
      `${fg} (${tokens[fg]}) on ${bg} (${tokens[bg]}) = ${ratio.toFixed(2)}:1`,
    ).toBeGreaterThanOrEqual(4.5);
  });

  it.each(UI_PAIRS)("%s on %s meets AA for UI (3:1)", (fg, bg) => {
    const ratio = contrast(tokens[fg], tokens[bg]);
    expect(
      ratio,
      `${fg} (${tokens[fg]}) on ${bg} (${tokens[bg]}) = ${ratio.toFixed(2)}:1`,
    ).toBeGreaterThanOrEqual(3);
  });
});

describe("the contrast maths itself", () => {
  it("matches known reference ratios", () => {
    // Without this, a bug in luminance() could make every assertion above pass on nonsense.
    expect(contrast("#000000", "#ffffff")).toBeCloseTo(21, 1);
    expect(contrast("#ffffff", "#ffffff")).toBeCloseTo(1, 5);
    expect(contrast("#777777", "#ffffff")).toBeCloseTo(4.48, 1);
  });
});
