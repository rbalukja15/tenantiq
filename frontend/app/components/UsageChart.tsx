"use client";

import { type KeyboardEvent, type PointerEvent, useEffect, useId, useRef, useState } from "react";

import { labelledIndexes, niceScale } from "@/lib/chart";
import { formatCompact, formatDay, formatUsdTick } from "@/lib/format";

import styles from "./UsageChart.module.css";

export type ChartSeries = {
  label: string;
  /** One per day. Drives the geometry only. */
  values: number[];
  /** What the tooltip prints for each day, formatted on the server from the exact value. */
  display: string[];
};

type Props = {
  title: string;
  /** A sentence a screen reader hears in place of the drawing: the range and what it adds up to. */
  summary: string;
  /** Every day of the range, `YYYY-MM-DD`, in order. */
  dates: string[];
  /** One series, or two; each takes the next chart colour in the order given. */
  series: ChartSeries[];
  /** How the y axis is labelled: compact counts, or dollars. */
  unit: "count" | "usd";
};

const HEIGHT = 200;
const MARGIN = { top: 12, bottom: 28, left: 56 };
/** The width drawn until the container is measured, and the one the server renders. */
const FALLBACK_WIDTH = 640;
/** Roughly how far apart day labels need to be to never touch: "Sep 30" at 12px, and some air. */
const LABEL_SPACING = 72;
/** Markers on every day only while there are few enough of them to read as points, not a fringe. */
const MAX_MARKED_DAYS = 14;

/**
 * A line chart of one measure per day (#27, ADR-0025): a 2px line per series over hairline
 * gridlines, the area under a lone series washed in its colour, and a legend once there are two.
 *
 * A Client Component for one reason, the hover layer. Pointing anywhere over the plot snaps a
 * crosshair to the nearest day and lists every series' value for it; the arrow keys do the same
 * once the chart has focus, so the readout is not mouse-only. It is an enhancement, never the only
 * way to a number: the dashboard prints every day in a table, and the drawing carries a
 * one-sentence summary for a screen reader.
 *
 * Handed numbers and preformatted strings, never a token. The amounts the tooltip prints are
 * strings the server formatted from the API's exact decimals; the `values` only place the line.
 */
export function UsageChart({ title, summary, dates, series, unit }: Props) {
  const plotRef = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(FALLBACK_WIDTH);
  const [active, setActive] = useState<number | null>(null);
  const hintId = useId();

  useEffect(() => {
    const node = plotRef.current;
    // jsdom has no ResizeObserver; the fallback width is a complete chart, just not a fitted one.
    if (!node || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(([entry]) => {
      setWidth(Math.max(280, Math.round(entry.contentRect.width)));
    });
    observer.observe(node);
    return () => observer.disconnect();
  }, []);

  const count = dates.length;
  const scale = niceScale(Math.max(0, ...series.flatMap((s) => s.values)), {
    whole: unit === "count",
  });
  const right = 16;
  const plotWidth = width - MARGIN.left - right;
  const plotHeight = HEIGHT - MARGIN.top - MARGIN.bottom;
  const x = (i: number) =>
    MARGIN.left + (count > 1 ? (i * plotWidth) / (count - 1) : plotWidth / 2);
  const y = (value: number) => MARGIN.top + plotHeight - (value / scale.top) * plotHeight;
  const tick = (value: number) =>
    unit === "usd" ? formatUsdTick(value, scale.step) : formatCompact(value);
  const labelled = labelledIndexes(count, plotWidth / LABEL_SPACING);
  const marked = count <= MAX_MARKED_DAYS;

  const clamp = (i: number) => Math.min(count - 1, Math.max(0, i));

  function onPointerMove(event: PointerEvent<SVGSVGElement>) {
    const box = event.currentTarget.getBoundingClientRect();
    const at = ((event.clientX - box.left) / box.width) * width;
    setActive(count > 1 ? clamp(Math.round(((at - MARGIN.left) / plotWidth) * (count - 1))) : 0);
  }

  function onKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    const moves: Record<string, (i: number) => number> = {
      ArrowLeft: (i) => i - 1,
      ArrowRight: (i) => i + 1,
      Home: () => 0,
      End: () => count - 1,
    };
    if (event.key === "Escape") {
      setActive(null);
      return;
    }
    const move = moves[event.key];
    if (!move) return;
    event.preventDefault(); // the arrows would otherwise scroll the page
    setActive((current) => clamp(move(current ?? count - 1)));
  }

  const line = (values: number[]) =>
    values
      .map((value, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(value).toFixed(1)}`)
      .join("");
  const baseline = y(0);
  // The line, then down to the baseline and back along it to the first day.
  const area = (values: number[]) =>
    `${line(values)}L${x(count - 1).toFixed(1)},${baseline}L${x(0).toFixed(1)},${baseline}Z`;

  return (
    <figure className={styles.figure}>
      <figcaption className={styles.title}>{title}</figcaption>
      {series.length > 1 ? (
        <ul className={styles.legend}>
          {series.map((s, n) => (
            <li key={s.label} data-series={n + 1}>
              <span className={styles.key} aria-hidden="true" />
              {s.label}
            </li>
          ))}
        </ul>
      ) : null}

      <div
        ref={plotRef}
        className={styles.plot}
        tabIndex={0}
        role="group"
        aria-label={`${title}, day by day`}
        aria-describedby={hintId}
        onKeyDown={onKeyDown}
        onFocus={() => setActive((current) => current ?? count - 1)}
        onBlur={() => setActive(null)}
      >
        <svg
          className={styles.svg}
          viewBox={`0 0 ${width} ${HEIGHT}`}
          height={HEIGHT}
          role="img"
          aria-label={summary}
          onPointerMove={onPointerMove}
          onPointerLeave={() => setActive(null)}
        >
          {scale.ticks.map((value) => (
            <g key={value}>
              <line
                className={value === 0 ? styles.baseline : styles.grid}
                x1={MARGIN.left}
                x2={width - right}
                y1={y(value)}
                y2={y(value)}
              />
              <text
                className={styles.axis}
                x={MARGIN.left - 8}
                y={y(value)}
                textAnchor="end"
                dy="0.32em"
              >
                {tick(value)}
              </text>
            </g>
          ))}

          {labelled.map((i) => (
            <text
              key={dates[i]}
              className={styles.axis}
              x={x(i)}
              y={HEIGHT - 8}
              // The first and last labels hang inward, so neither runs off the edge of the plot.
              textAnchor={
                count > 1 && i === 0 ? "start" : count > 1 && i === count - 1 ? "end" : "middle"
              }
            >
              {formatDay(dates[i])}
            </text>
          ))}

          {series.map((s, n) => (
            <g key={s.label} data-series={n + 1}>
              {series.length === 1 ? <path className={styles.area} d={area(s.values)} /> : null}
              <path className={styles.line} d={line(s.values)} />
              {marked || count === 1
                ? s.values.map((value, i) => (
                    <circle key={dates[i]} className={styles.dot} cx={x(i)} cy={y(value)} r={4} />
                  ))
                : null}
            </g>
          ))}

          {active !== null ? (
            <g className={styles.hover} aria-hidden="true">
              <line
                className={styles.crosshair}
                x1={x(active)}
                x2={x(active)}
                y1={MARGIN.top}
                y2={baseline}
              />
              {series.map((s, n) => (
                <circle
                  key={s.label}
                  data-series={n + 1}
                  className={styles.dot}
                  cx={x(active)}
                  cy={y(s.values[active])}
                  r={5}
                />
              ))}
            </g>
          ) : null}
        </svg>

        {active !== null ? (
          <div
            className={styles.tip}
            // Beside the crosshair, on whichever side has room for it.
            data-side={x(active) > width / 2 ? "left" : "right"}
            style={{ left: x(active), top: MARGIN.top }}
            aria-hidden="true"
          >
            <p className={styles.tipDay}>{formatDay(dates[active], { long: true })}</p>
            {series.map((s, n) => (
              <p key={s.label} className={styles.tipRow} data-series={n + 1}>
                {series.length > 1 ? <span className={styles.key} /> : null}
                <span className={styles.tipValue}>{s.display[active]}</span>
                <span className={styles.tipLabel}>{s.label}</span>
              </p>
            ))}
          </div>
        ) : null}

        <p id={hintId} className={styles.srOnly}>
          Use the left and right arrow keys to read each day.
        </p>
        {/* What the tooltip shows, said once for a keyboard user who cannot see it. */}
        <p className={styles.srOnly} aria-live="polite">
          {active !== null
            ? `${formatDay(dates[active], { long: true })}: ${series
                .map((s) => `${s.label} ${s.display[active]}`)
                .join(", ")}`
            : ""}
        </p>
      </div>
    </figure>
  );
}
