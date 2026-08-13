import Link from "next/link";

import styles from "./message.module.css";

/**
 * Next ships an unstyled fallback for 404s, hardcoded to white-on-black and outside the token system
 * entirely — so without this file the one page most likely to be hit by a stale link is the one page
 * that ignores the design (#74).
 */
export default function NotFound() {
  return (
    <main className={styles.wrap}>
      <h1>Page not found</h1>
      <p className={styles.body}>
        That address does not match anything in TenantIQ. It may have been a stale link.
      </p>
      <Link className={styles.link} href="/">
        Back to your workspace
      </Link>
    </main>
  );
}
