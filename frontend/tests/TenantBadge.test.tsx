import { render, screen } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import { TenantBadge } from "@/app/components/TenantBadge";

import { server } from "./msw";

/**
 * Proves the #52 harness end to end: a real component renders in jsdom, its `fetch` is intercepted
 * at the network boundary by MSW, and Testing Library asserts on what a *user* would see. Every
 * #18–#20 criterion is UI behaviour, so this is the shape those tests will take.
 */
describe("TenantBadge", () => {
  it("renders the tenant once the session loads", async () => {
    server.use(
      http.get("/api/me", () =>
        HttpResponse.json({
          username: "alice",
          tenant: { id: "t-1", slug: "acme", name: "Acme" },
        }),
      ),
    );

    render(<TenantBadge />);

    // The loading state is what the user sees first...
    expect(screen.getByRole("status")).toHaveTextContent("Loading session…");
    // ...then the resolved session, proving the mocked response really flowed through fetch.
    expect(await screen.findByText(/Signed in as/)).toHaveTextContent("Signed in as alice — Acme");
  });

  it("shows a generic error when the session request fails", async () => {
    server.use(http.get("/api/me", () => new HttpResponse(null, { status: 500 })));

    render(<TenantBadge />);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Could not load your session.");
    // The API's failure detail is never rendered verbatim.
    expect(alert.textContent).not.toMatch(/500/);
  });
});
