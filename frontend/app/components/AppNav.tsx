"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import styles from "./AppNav.module.css";

/**
 * The signed-in area's two surfaces (#20).
 *
 * `aria-current="page"` is the load-bearing part, not the underline. The active tab is otherwise
 * distinguished only by colour and weight, which WCAG 1.4.1 does not accept as the sole carrier of
 * meaning and which tells a screen-reader user nothing at all about where they are.
 *
 * A Client Component because `usePathname` is one, and because the alternative — threading the
 * current path down from every page — is worse. It holds no data and no token.
 */
const LINKS = [
  { href: "/", label: "Ask" },
  { href: "/documents", label: "Documents" },
] as const;

export function AppNav() {
  const pathname = usePathname();

  return (
    <nav className={styles.nav} aria-label="Sections">
      {LINKS.map((link) => {
        // Exact match, not `startsWith`: "/" is a prefix of every path, so a prefix test would mark
        // Ask as the current page while the user is standing on Documents.
        const current = pathname === link.href;
        return (
          <Link
            key={link.href}
            href={link.href}
            className={current ? `${styles.link} ${styles.current}` : styles.link}
            aria-current={current ? "page" : undefined}
          >
            {link.label}
          </Link>
        );
      })}
    </nav>
  );
}
