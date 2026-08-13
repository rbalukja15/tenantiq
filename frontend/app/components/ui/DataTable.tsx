import type { ReactNode } from "react";

import styles from "./DataTable.module.css";

type Props = {
  /** Describes the table for screen readers; visually hidden unless `showCaption`. */
  caption: string;
  showCaption?: boolean;
  head: ReactNode;
  children: ReactNode;
};

/**
 * A table of records (#74; consumed by #20).
 *
 * Wrapped in its own horizontally scrollable container so a wide table scrolls *itself* rather than
 * the page — a body that scrolls sideways is the most common responsive regression in a data UI.
 * The wrapper is focusable (`tabindex=0`) because a scrollable region that cannot be reached by
 * keyboard is unusable without a mouse.
 *
 * **Numeric columns:** put `data-numeric` on the `<th>` *and* on its `<td>`s. The cells get the
 * evidence face with tabular figures and right alignment, and the header follows its digits — a
 * left-aligned heading over right-aligned numbers is the usual tell that only half the convention
 * was applied. `data-numeric={someBoolean}` is safe: React serialises `false` to the string
 * `"false"`, and the selector excludes it.
 *
 * **Row headers:** the identifying cell of a row is `<th scope="row">`, and it is styled as a cell —
 * not as a column header — so a case-significant filename is not uppercased.
 */
export function DataTable({ caption, showCaption = false, head, children }: Props) {
  return (
    <div className={styles.scroll} tabIndex={0} role="region" aria-label={caption}>
      <table className={styles.table}>
        <caption className={showCaption ? styles.caption : styles.srOnly}>{caption}</caption>
        <thead>{head}</thead>
        <tbody>{children}</tbody>
      </table>
    </div>
  );
}
