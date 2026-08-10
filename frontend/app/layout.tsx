import type { ReactNode } from "react";

export const metadata = {
  title: "TenantIQ",
  description: "Multi-tenant document intelligence with grounded, cited answers.",
};

/**
 * The root layout wraps *everything*, including `/login`, so it holds only the document shell.
 * Signed-in chrome (the header, the sign-out control) lives in the `(app)` route group's layout, so
 * the login page does not offer to sign out a user who is not signed in.
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
