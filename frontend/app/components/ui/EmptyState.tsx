import type { ReactNode } from "react";

import styles from "./EmptyState.module.css";

type Props = {
  title: string;
  children?: ReactNode;
  /** A single next action, when there is an obvious one. */
  action?: ReactNode;
};

/** A collection with nothing in it yet — an empty document list, no usage in a period (#74). */
export function EmptyState({ title, children, action }: Props) {
  return (
    <div className={styles.wrap}>
      <h2 className={styles.title}>{title}</h2>
      {children ? <p className={styles.body}>{children}</p> : null}
      {action}
    </div>
  );
}
