"use client";

import { useEffect, useState } from "react";

/** The subset of `GET /api/me` this component needs (see the backend's MeView). */
type Me = {
  username: string;
  tenant: { id: string; slug: string; name: string };
};

type State =
  { status: "loading" } | { status: "ready"; me: Me } | { status: "error"; message: string };

/**
 * Shows which tenant the current session belongs to — the frontend's first real data-fetching
 * component, and the smoke test for the harness set up in #52.
 *
 * It calls the API through the **same-origin** `/api/*` path rather than the backend's origin: per
 * ADR-0013 the browser never holds a token, so requests go through the Next proxy, which attaches
 * the session cookie's credentials server-side. That also means no CORS is involved.
 *
 * The real app shell (#18) replaces this with proper session handling; what matters here is that a
 * component which renders, fetches, and handles loading/error states is provably testable.
 */
export function TenantBadge() {
  const [state, setState] = useState<State>({ status: "loading" });

  useEffect(() => {
    let cancelled = false;
    fetch("/api/me")
      .then(async (response) => {
        if (!response.ok) throw new Error(`Request failed (${response.status})`);
        return (await response.json()) as Me;
      })
      .then((me) => {
        if (!cancelled) setState({ status: "ready", me });
      })
      .catch(() => {
        // Deliberately generic: a failure reason from the API is not something to render verbatim.
        if (!cancelled) setState({ status: "error", message: "Could not load your session." });
      });
    return () => {
      cancelled = true; // don't set state after unmount
    };
  }, []);

  if (state.status === "loading") return <p role="status">Loading session…</p>;
  if (state.status === "error") return <p role="alert">{state.message}</p>;

  return (
    <p>
      Signed in as <strong>{state.me.username}</strong> — {state.me.tenant.name}
    </p>
  );
}
