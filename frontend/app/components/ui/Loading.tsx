import styles from "./Loading.module.css";

/**
 * A busy indicator (#74).
 *
 * `role="status"` with a real text label, not a bare spinner: a screen reader must be told that
 * something is in progress, and the animation is decorative. The dots stop moving entirely under
 * `prefers-reduced-motion` (handled globally in global.css).
 */
export function Loading({ label = "Loading…" }: { label?: string }) {
  return (
    <p className={styles.wrap} role="status">
      <span className={styles.dots} aria-hidden="true">
        <span />
        <span />
        <span />
      </span>
      {label}
    </p>
  );
}
