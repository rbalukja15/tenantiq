import type { ReactNode } from "react";

export const metadata = {
  title: "TenantIQ",
  description: "Multi-tenant document intelligence with grounded, cited answers.",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
