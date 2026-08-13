import type { ButtonHTMLAttributes } from "react";

import styles from "./Button.module.css";

type Props = ButtonHTMLAttributes<HTMLButtonElement> & {
  /** `primary` fills with the accent; `quiet` is a bordered control for secondary actions. */
  variant?: "primary" | "quiet";
  full?: boolean;
};

/**
 * The app's button (#74).
 *
 * `type` defaults to `"button"` on purpose: an unspecified `<button>` inside a form defaults to
 * `submit` in HTML, which is a classic accidental-submit bug. A button that submits says so.
 */
export function Button({ variant = "primary", full = false, type = "button", ...rest }: Props) {
  const className = [styles.button, styles[variant], full ? styles.full : ""]
    .filter(Boolean)
    .join(" ");
  return <button type={type} className={className} {...rest} />;
}
