import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import LoginPage from "@/app/login/page";

/**
 * The login page is an async Server Component, so it is awaited and then rendered — the same way
 * `TenantHome` is tested. No mocking is involved: it reads only its own `searchParams`.
 */
async function renderLogin(error?: string) {
  render(await LoginPage({ searchParams: Promise.resolve(error ? { error } : {}) }));
}

describe("login page", () => {
  it("asks for the workspace, because that is what resolves to a realm", async () => {
    await renderLogin();

    expect(screen.getByLabelText("Workspace")).toHaveAttribute("name", "tenant");
    expect(screen.getByRole("button", { name: "Continue" })).toHaveAttribute("type", "submit");
  });

  it("posts to the login route rather than linking to it", async () => {
    // A GET login endpoint would let any site push a visitor into an attacker-chosen tenant, which
    // SameSite=Lax permits for top-level navigations (T9). The form method is part of that defence.
    const { container } = render(await LoginPage({ searchParams: Promise.resolve({}) }));
    const form = container.querySelector("form");

    expect(form).toHaveAttribute("method", "post");
    expect(form).toHaveAttribute("action", "/api/auth/login");
  });

  it("ties the failure message to the field it is about", async () => {
    // The error arrives as a fresh document load, so `role="alert"` announces nothing on its own —
    // a live region only reports mutations made after it is registered. The association is what
    // makes the failure reachable: the field is invalid, and it points at the reason.
    await renderLogin("unknown_tenant");

    const field = screen.getByLabelText("Workspace");
    expect(field).toHaveAttribute("aria-invalid", "true");
    expect(field).toHaveAccessibleDescription(/could not find that workspace/i);
  });

  it("announces the failure before the hint", async () => {
    await renderLogin("unknown_tenant");

    const description = screen.getByLabelText("Workspace").getAttribute("aria-describedby") ?? "";
    expect(description.split(" ")[0]).toBe("login-error");
  });

  it("marks nothing invalid when there is no error", async () => {
    await renderLogin();

    expect(screen.getByLabelText("Workspace")).not.toHaveAttribute("aria-invalid");
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it.each([
    ["unknown_tenant", /could not find that workspace/i],
    ["session_expired", /session has ended/i],
    ["unavailable", /temporarily unavailable/i],
    ["retry", /could not be completed/i],
  ])("explains %s in words a user can act on", async (code, expected) => {
    await renderLogin(code);

    expect(screen.getByRole("alert")).toHaveTextContent(expected);
  });

  it("ignores an unrecognised error code rather than rendering an empty alert", async () => {
    // The code comes from the query string, so anyone can put anything there.
    await renderLogin("../../etc/passwd");

    expect(screen.queryByRole("alert")).toBeNull();
  });
});
