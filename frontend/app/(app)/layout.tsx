import type { ReactNode } from "react";

import { LogoutButton } from "@/app/components/LogoutButton";

/**
 * Chrome for the signed-in area (#18). `(app)` is a route group, so it adds no URL segment — `/`
 * still resolves to `(app)/page.tsx` — but it scopes this header to authenticated routes and keeps
 * it off `/login`.
 */
export default function AppLayout({ children }: { children: ReactNode }) {
  return (
    <>
      <header>
        <strong>TenantIQ</strong>
        <LogoutButton />
      </header>
      <main>{children}</main>
    </>
  );
}
