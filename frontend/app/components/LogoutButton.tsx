"use client";

import { useRouter } from "next/navigation";

import { Button } from "@/app/components/ui/Button";
import { readCsrfToken } from "@/lib/csrf-client";
import { useState } from "react";

/**
 * Signs the user out (#18).
 *
 * Reads the CSRF cookie and echoes it in a header — the readable half of the double-submit pair.
 * The request is a `fetch`, not a form POST, because the response is JSON: the server hands back the
 * IdP's end-session URL and *this* code navigates to it. Letting `fetch` follow that redirect itself
 * would fetch the IdP's logout page into a response body nobody renders, leaving the address bar
 * where it was and the IdP session alive.
 */
export function LogoutButton() {
  const router = useRouter();
  const [busy, setBusy] = useState(false);

  async function signOut() {
    setBusy(true);
    try {
      // Via the shared reader (#19), not an inline suffix match. `endsWith("tiq_csrf")` also accepts
      // `evil_tiq_csrf` — a name any sibling subdomain can write — and sending that token makes the
      // proxy answer 403, at which point this handler's fallback pushes the user to /login while the
      // session cookie and its server-side record both survive. That is threat-model T13 exactly: a
      // sign-out that does not sign out, on a shared machine.
      const csrf = readCsrfToken();
      const response = await fetch("/api/auth/logout", {
        method: "POST",
        headers: csrf !== null ? { "x-csrf-token": csrf } : {},
      });
      const body = (await response.json()) as { location?: string };
      if (body.location) {
        // Always absolute, and usually the IdP's end-session endpoint on another origin — so this
        // has to be a real navigation, not a client-side route change.
        window.location.assign(body.location);
        return;
      }
      router.push("/login");
    } catch {
      // Even if the call fails, get the user to the login page rather than stranding them here.
      router.push("/login");
    }
  }

  return (
    <Button variant="quiet" onClick={signOut} disabled={busy}>
      {busy ? "Signing out…" : "Sign out"}
    </Button>
  );
}
