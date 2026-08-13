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
 */
export function SourceCard({ source, active = false, onSelect }: Props) {
  return (
    <button
      type="button"
      className={styles.card}
      data-active={active || undefined}
      aria-pressed={active}
      onClick={() => onSelect?.(source.n)}
    >
      <span className={styles.head}>
        <span className={styles.n}>{source.n}</span>
        <span className={styles.doc}>{source.documentTitle}</span>
      </span>
      <span className={styles.quote}>{source.quote}</span>
      <span className={styles.meta}>
        <span>chunk {source.chunkId}</span>
        <span>
          {source.startOffset}–{source.endOffset}
        </span>
        <span>sim {source.similarity.toFixed(2)}</span>
      </span>
    </button>
  );
}
