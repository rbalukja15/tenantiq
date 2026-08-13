import type { InputHTMLAttributes } from "react";
import { useId } from "react";

import styles from "./TextField.module.css";

type Props = InputHTMLAttributes<HTMLInputElement> & {
  label: string;
  /** Shown under the field; announced with the input rather than floating free. */
  hint?: string;
  /**
   * Id of a message elsewhere on the page saying why this value was rejected. Setting it also marks
   * the field invalid, so the two can never disagree.
   */
  errorId?: string;
  /** Render the value in the evidence face. For identifiers (a slug, an id), not for prose. */
  mono?: boolean;
};

/**
 * A labelled text input (#74).
 *
 * The label is always rendered and always associated — no placeholder-as-label, which disappears the
 * moment someone types and leaves screen-reader users with an unnamed field. `useId` keeps the
 * association correct even with several fields on a page.
 */
export function TextField({ label, hint, id, errorId, mono = false, ...rest }: Props) {
  const generated = useId();
  const inputId = id ?? generated;
  const hintId = hint ? `${inputId}-hint` : undefined;
  // Error first: when a value has just been rejected, that is the thing to hear before the hint.
  const describedBy = [errorId, hintId].filter(Boolean).join(" ") || undefined;

  return (
    <div className={styles.field}>
      <label className={styles.label} htmlFor={inputId}>
        {label}
      </label>
      <input
        id={inputId}
        className={mono ? `${styles.input} ${styles.mono}` : styles.input}
        aria-describedby={describedBy}
        aria-invalid={errorId ? true : undefined}
        {...rest}
      />
      {hint ? (
        <p id={hintId} className={styles.hint}>
          {hint}
        </p>
      ) : null}
    </div>
  );
}
