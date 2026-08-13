"use client";

import styles from "./SourceCard.module.css";

export type Source = {
  n: number;
  documentTitle: string;
  /** The chunk's text, exactly as stored — never paraphrased. */
  quote: string;
  chunkId: number;
  startOffset: number;
  endOffset: number;
  similarity: number;
};

type Props = {
  source: Source;
  active?: boolean;
  onSelect?: (n: number) => void;
};

/**
 * One retrieved chunk, shown beside the answer it supports (#74; consumed by #19).
 *
 * This component is the project's central claim made visible: the quote is the stored chunk text and
 * the offsets are its real span, so a reader can go and check. It renders what it is given and
 * neither truncates nor reformats the quote — a "tidied up" citation would defeat the point.
 *
 * **The card is an `<article>`, and only the numbered badge is a control.** Two designs were
 * rejected on the way here:
 *
 *   - *The whole card as a `<button>`.* `role=button` is children-presentational in ARIA, so every
 *     descendant is stripped from the accessibility tree and the quote, chunk id and offsets survive
 *     only as the button's accessible name — one unpunctuated run, because the visual separation
 *     comes from flex `gap`, which contributes no text. The evidence would be technically present
 *     and practically unreadable, in the component whose whole job is making evidence readable.
 *   - *A transparent control stretched over the card.* Keeps the large click target, but it sits on
 *     top of the text and blocks selection — and copying a quoted passage is a primary thing to do
 *     with a citation.
 *
 * The badge mirrors the `CitationChip` in the answer, which is exactly the relationship being
 * expressed: press either end to light up the other. It renders as a button only when `onSelect` is
 * given, so a display-only card has no focusable no-op in the tab order.
 */
export function SourceCard({ source, active = false, onSelect }: Props) {
  return (
    <article className={styles.card} data-active={active || undefined}>
      <p className={styles.head}>
        {onSelect ? (
          <button
            type="button"
            className={styles.badgeButton}
            aria-pressed={active}
            aria-label={`Show source ${source.n} in the answer`}
            onClick={() => onSelect(source.n)}
          >
            {source.n}
          </button>
        ) : (
          <span className={styles.badge}>{source.n}</span>
        )}
        <span className={styles.doc}>{source.documentTitle}</span>
      </p>
      <p className={styles.quote}>{source.quote}</p>
      <p className={styles.meta}>
        <span>chunk {source.chunkId}</span>
        <span>
          {source.startOffset}–{source.endOffset}
        </span>
        <span>sim {source.similarity.toFixed(2)}</span>
      </p>
    </article>
  );
}
