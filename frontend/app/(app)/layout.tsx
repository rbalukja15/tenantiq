import type { ReactNode } from "react";

import { AppNav } from "@/app/components/AppNav";
import { LogoutButton } from "@/app/components/LogoutButton";

import styles from "./layout.module.css";

/**
 * Chrome for the signed-in area (#18, styled in #74, navigation in #20). `(app)` is a route group,
 * so it adds no URL segment — `/` still resolves to `(app)/page.tsx` — but it scopes this header to
 * authenticated routes and keeps it off `/login`.
 */
export default function AppLayout({ children }: { children: ReactNode }) {
  return (
    <div className={styles.shell}>
      <header className={styles.header}>
        <span className={styles.brand}>
          Tenant<em>IQ</em>
        </span>
        <AppNav />
        <LogoutButton />
      </header>
      <main className={styles.main}>{children}</main>
    </div>
  );
}
