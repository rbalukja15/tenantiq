import type { ReactNode } from "react";

import styles from "./Callout.module.css";

type Props = {
  /** `error` announces assertively; `info` and `warning` are polite status messages. */
  tone?: "error" | "warning" | "info";
  /** Set when a form control needs to point at this message via `aria-describedby`. */
  id?: string;
  children: ReactNode;
};

/**
 * A short message about the state of things — a failed sign-in, an expired session (#74).
 *
 * The ARIA role follows the tone rather than being hardcoded: an error is an `alert` (announced
 * immediately), while informational messages are `status` (announced when the user is idle).
 * Marking everything `alert` trains people to ignore alerts.
 */
export function Callout({ tone = "info", id, children }: Props) {
  return (
    <p
      id={id}
      className={`${styles.callout} ${styles[tone]}`}
      role={tone === "error" ? "alert" : "status"}
    >
      {children}
    </p>
  );
}
