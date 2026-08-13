"use client";

import { Button } from "@/app/components/ui/Button";

import styles from "./message.module.css";

/**
 * The route-level error boundary (#74).
 *
 * Must be a Client Component — that is Next's contract for `error.tsx`, since it receives a `reset`
 * callback. It deliberately renders **nothing from `error`**: a thrown error's message can carry
 * internal detail (a hostname, a query, a path), and the backend already takes care to sanitise what
 * reaches a tenant (#47). The digest is shown because it is an opaque id a user can quote in a
 * support request, and it is the only way to correlate this screen with a server log.
 */
export default function Error({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <main className={styles.wrap}>
      <h1>Something went wrong</h1>
      <p className={styles.body}>
        This page could not be loaded. Trying again often works; if it does not, the reference below
        identifies what failed.
      </p>
      {error.digest ? (
        <p className={styles.body}>
          Reference <code>{error.digest}</code>
        </p>
      ) : null}
      <Button onClick={reset}>Try again</Button>
    </main>
  );
}
