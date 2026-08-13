import styles from "./StatusPill.module.css";

/** Mirrors `Document.Status` in the backend (app/models.py). */
export type DocumentStatus = "pending" | "processing" | "ready" | "failed";

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
 */
export function StatusPill({ status }: { status: DocumentStatus }) {
  return (
    <span className={`${styles.pill} ${styles[status]}`}>
      <span className={styles.dot} aria-hidden="true" />
      {LABELS[status]}
    </span>
  );
}
