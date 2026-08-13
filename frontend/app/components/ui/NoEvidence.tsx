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
    /*
     * A named region, and deliberately **not** a live region of any kind.
     *
     * `role="status"` on the section would carry an implicit `aria-atomic="true"`, flattening the
     * heading, the paragraph and both list items into one structureless utterance — and it would
     * override the section's implicit `region` role. The narrower fix this originally shipped with,
     * `aria-live` on the heading alone, turned out to announce nothing at all: a live region only
     * reports mutations made *after* it is registered, and this whole subtree is inserted into the
     * DOM already containing its text, so there is no mutation to report (#19).
     *
     * Announcing a refusal is therefore the caller's job, through a region that is already mounted
     * when the refusal happens — see `AskScreen`.
     */
    <section className={styles.wrap} aria-labelledby="no-evidence-heading">
      <h2 id="no-evidence-heading" className={styles.heading}>
        No supporting passage found
      </h2>
      <p className={styles.body}>
        {/* The `{" "}` is load-bearing: JSX strips the whitespace around a newline before an
            expression, so `to\n{question}` renders as "toWhat are the payment terms?". */}
        Nothing in this workspace&rsquo;s documents was close enough to{" "}
        {question ? <q className={styles.q}>{question}</q> : "that question"} to answer from. Rather
        than guess, TenantIQ has answered nothing.
      </p>
      <ul className={styles.next}>
        <li>Try naming the document or the exact term you expect to appear.</li>
        <li>Check the passage is in a document that finished processing.</li>
      </ul>
    </section>
  );
}
