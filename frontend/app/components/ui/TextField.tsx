import type { InputHTMLAttributes } from "react";
import { useId } from "react";

import styles from "./TextField.module.css";

type Props = InputHTMLAttributes<HTMLInputElement> & {
  label: string;
  /** Shown under the field; announced with the input rather than floating free. */
  hint?: string;
};

/**
 * A labelled text input (#74).
 *
 * The label is always rendered and always associated — no placeholder-as-label, which disappears the
 * moment someone types and leaves screen-reader users with an unnamed field. `useId` keeps the
 * association correct even with several fields on a page.
 */
export function TextField({ label, hint, id, ...rest }: Props) {
  const generated = useId();
  const inputId = id ?? generated;
  const hintId = hint ? `${inputId}-hint` : undefined;

  return (
    <div className={styles.field}>
      <label className={styles.label} htmlFor={inputId}>
        {label}
      </label>
      <input id={inputId} className={styles.input} aria-describedby={hintId} {...rest} />
      {hint ? (
        <p id={hintId} className={styles.hint}>
          {hint}
        </p>
      ) : null}
    </div>
  );
}
