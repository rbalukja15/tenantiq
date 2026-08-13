import type { ReactNode } from "react";

import "./global.css";

export const metadata = {
  title: "TenantIQ",
  description: "Multi-tenant document intelligence with grounded, cited answers.",
};

/**
 * The root layout wraps *everything*, including `/login`, so it holds only the document shell and
 * the one global stylesheet. Signed-in chrome lives in the `(app)` route group's layout, so the
 * login page does not offer to sign out a user who is not signed in.
 *
 * `global.css` is imported here and nowhere else. Next orders CSS by import order, so a single entry
 * point is what keeps token definitions ahead of every module that reads them (ADR-0014).
 *
 * Deliberately not `force-dynamic`: that would de-optimise `/login`, which has nothing per-request
 * in it. The signed-in page opts into dynamic rendering for itself.
 */
export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
