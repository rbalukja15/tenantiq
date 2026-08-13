import styles from "./NoEvidence.module.css";

/**
 * What the reader sees when retrieval found nothing above the similarity floor (#74; used by #19).
 *
 * This is the most important screen in a product whose whole claim is grounded answers, and it is a
 * primitive rather than a branch inside the chat UI precisely so it cannot drift into looking like
 * an answer. Three things it deliberately does NOT do:
 *
 *   - it is not set in the serif used for answers, so it never reads as prose the model produced;
 *   - it carries no citation chips and no source list, because there is nothing to cite;
 *   - it does not apologise or hedge — it states plainly that the corpus had nothing, and says what
 *     the reader can do about it.
 *
 * A grey "No results" box would fail the first test: it looks like an empty list, not like a system
 * declining to guess.
 */
export function NoEvidence({ question }: { question?: string }) {
  return (
    <section className={styles.wrap} role="status">
      <h2 className={styles.heading}>No supporting passage found</h2>
      <p className={styles.body}>
        Nothing in this workspace&rsquo;s documents was close enough to
        {question ? <q className={styles.q}>{question}</q> : " that question"} to answer from.
        Rather than guess, TenantIQ has answered nothing.
      </p>
      <ul className={styles.next}>
        <li>Try naming the document or the exact term you expect to appear.</li>
        <li>Check the passage is in a document that finished processing.</li>
      </ul>
    </section>
  );
}
