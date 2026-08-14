import styles from "./ProgressBar.module.css";

type Props = {
  /** Names what is progressing — "Uploading MSA.pdf", not "Progress". */
  label: string;
  /** `0`–`1`, or `null` when the browser cannot say how much there is to do. */
  fraction: number | null;
};

/**
 * A determinate-or-indeterminate progress bar (#74's family, added by #20).
 *
 * A native `<progress>`, not a div with `role="progressbar"`. It carries the role, the value and the
 * min/max for free, and — the part that matters here — a `<progress>` with **no** `value` is the
 * platform's own indeterminate state, which assistive technology already knows how to report. The
 * ARIA-on-a-div version has to reimplement all of that and gets the indeterminate case wrong by
 * omission: an `aria-valuenow` of 0 is announced as "0 percent", which is not what "we cannot say"
 * means. The cost is three vendor pseudo-elements in the stylesheet, which is a fair trade.
 *
 * The percentage is also rendered as text beside the bar, because a bar alone carries its meaning
 * only in colour and length.
 */
export function ProgressBar({ label, fraction }: Props) {
  const percent = fraction === null ? null : Math.round(fraction * 100);
  return (
    <span className={styles.wrap}>
      {/* `value` omitted entirely when indeterminate — `value={undefined}` is what makes the element
          indeterminate, and `value={0}` would claim, wrongly, that nothing has happened yet. */}
      <progress
        className={styles.bar}
        aria-label={label}
        max={100}
        {...(percent === null ? {} : { value: percent })}
      />
      <span className={styles.value}>{percent === null ? "…" : `${percent}%`}</span>
    </span>
  );
}
