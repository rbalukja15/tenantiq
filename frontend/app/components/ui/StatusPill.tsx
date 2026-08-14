import type { DocumentStatus } from "@/lib/documents";

import styles from "./StatusPill.module.css";

const LABELS: Record<DocumentStatus, string> = {
  pending: "Pending",
  processing: "Processing",
  ready: "Ready",
  failed: "Failed",
};

/**
 * Ingestion state, encoded in form as well as colour (#74).
 *
 * The dot is not decoration: colour alone must never be the only carrier of meaning (WCAG 1.4.1),
 * so each pill also states its status in words. That is why there is no icon-only variant.
 *
 * The status union lives in `lib/documents.ts` — it mirrors `Document.Status` in the backend, which
 * is a fact about the domain rather than about this component. Keeping a second copy here is how the
 * two quietly stop agreeing.
 */
export function StatusPill({ status }: { status: DocumentStatus }) {
  // The type says this cannot miss; the network says otherwise. A status the backend grew after this
  // build shipped would otherwise index `LABELS` and `styles` with `undefined` and render an empty,
  // unstyled pill — a blank cell where a state should be. Showing the unknown value under neutral
  // styling is worse-looking and more truthful.
  const known = status in LABELS;
  return (
    <span className={`${styles.pill} ${known ? styles[status] : styles.pending}`}>
      <span className={styles.dot} aria-hidden="true" />
      {known ? LABELS[status] : status}
    </span>
  );
}
