/**
 * The arithmetic behind the usage charts (#27): where the gridlines go and which days get a label.
 *
 * Pure, and tested directly. jsdom does no layout, so a test of the drawn SVG could only assert
 * that some numbers were passed around; the decisions worth pinning are made here.
 */

export type Scale = {
  /** The value at the top of the axis: a round number at or above the largest value. */
  top: number;
  step: number;
  /** From zero to `top`, `step` apart. */
  ticks: number[];
};

/** The most intervals an axis is divided into: six labels down the side of a 200px chart. */
const MAX_INTERVALS = 5;

/**
 * A y axis from zero to the lowest round number at or above `max`: the smallest step of 1, 2 or 5
 * times a power of ten that covers `max` in at most five intervals. Smallest, so the line uses the
 * height it is given; a 41K peak gets an axis to 50K, not to 60K. `whole` keeps the step a whole
 * number, for counts: there is no half a query.
 *
 * An empty series still gets an axis, from 0 to 1, so a chart of nothing draws a flat line at zero
 * rather than dividing by it.
 */
export function niceScale(max: number, { whole = false }: { whole?: boolean } = {}): Scale {
  if (!(max > 0)) return { top: 1, step: 1, ticks: [0, 1] };
  const magnitude = 10 ** Math.floor(Math.log10(max / MAX_INTERVALS));
  // The epsilon absorbs float error, so a max that is already a whole number of steps (0.3 in steps
  // of 0.1 is 3.0000000000000004 of them) does not grow an empty band above it.
  const intervals = (step: number) => Math.ceil(max / step - 1e-9);
  // The last candidate always qualifies: the magnitude is the power of ten at or below a fifth of
  // `max`, so ten times it is above that fifth, and `max` fits in fewer than five of it.
  const step = [1, 2, 5, 10]
    .map((multiple) => multiple * magnitude)
    .map((candidate) => (whole ? Math.max(1, Math.round(candidate)) : candidate))
    .find((candidate) => intervals(candidate) <= MAX_INTERVALS)!;
  const ticks = Array.from({ length: intervals(step) + 1 }, (_, i) => roundTo(i * step, step));
  return { top: ticks[ticks.length - 1], step, ticks };
}

/** How many decimals a value on an axis of this step needs: 2 for 0.05, 0 for 500. */
export function decimalsFor(step: number): number {
  return Math.max(0, -Math.floor(Math.log10(step) + 1e-9));
}

function roundTo(value: number, step: number): number {
  return Number(value.toFixed(decimalsFor(step)));
}

/**
 * Which of `count` evenly spaced days get an x-axis label, when there is room for about `room`.
 *
 * Every day when they fit. Otherwise every so many, starting from the first, and always the last,
 * it being the one a reader looks for. The last label hangs left from its day, so the label before
 * it needs at least the usual step clear, or the two are drawn into each other; when it has less,
 * it is dropped, and the final gap is the longer one.
 */
export function labelledIndexes(count: number, room: number): number[] {
  if (count <= 0) return [];
  const slots = Math.max(2, Math.floor(room));
  if (count <= slots) return Array.from({ length: count }, (_, i) => i);
  const step = Math.ceil((count - 1) / (slots - 1));
  const picked: number[] = [];
  for (let i = 0; i < count - 1; i += step) picked.push(i);
  if (count - 1 - picked[picked.length - 1] < step) picked.pop();
  picked.push(count - 1);
  return picked;
}
