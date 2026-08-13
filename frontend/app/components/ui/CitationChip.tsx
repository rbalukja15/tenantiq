"use client";

import styles from "./CitationChip.module.css";

type Props = {
  /** 1-based index shown to the reader, matching the source list. */
  n: number;
  active?: boolean;
  onSelect?: (n: number) => void;
};

/**
 * An inline citation marker inside an answer (#74; consumed by #19).
 *
 * A real `<button>`, not a styled `<span>`: selecting a citation changes what is shown, so it must
 * be reachable and operable from the keyboard. `aria-pressed` communicates the selected state, and
 * the accessible name says "Source 2" rather than leaving a screen reader to announce a bare digit.
 */
export function CitationChip({ n, active = false, onSelect }: Props) {
  return (
    <button
      type="button"
      className={styles.chip}
      aria-pressed={active}
      aria-label={`Source ${n}`}
      onClick={() => onSelect?.(n)}
    >
      {n}
    </button>
  );
}
