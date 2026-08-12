import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, describe, expect, it, vi } from "vitest";

import { LogoutButton } from "@/app/components/LogoutButton";

import { APP_ORIGIN } from "./env";
import { server } from "./msw";

/**
 * The client half of the double-submit CSRF scheme (#18).
 *
 * It had no test at all, which meant the browser-side end of the only CSRF control was unverified:
 * renaming the header or dropping the cookie read left the whole suite green while every logout in
 * production would have been rejected with a 403.
 */

const routerPush = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: routerPush }) }));

const assign = vi.fn();

afterEach(() => {
  routerPush.mockReset();
  assign.mockReset();
  document.cookie = "tiq_csrf=; Max-Age=0; path=/";
});

function stubNavigation() {
  Object.defineProperty(window, "location", {
    configurable: true,
    value: { ...window.location, assign },
  });
}

/** Capture what the browser actually sent to the logout route. */
function mockLogout(body: object = { location: "https://idp.test/logout" }) {
  const seen: { csrf: string | null }[] = [];
  server.use(
    http.post(`${APP_ORIGIN}/api/auth/logout`, ({ request }) => {
      seen.push({ csrf: request.headers.get("x-csrf-token") });
      return HttpResponse.json(body);
    }),
  );
  return seen;
}

describe("LogoutButton", () => {
  it("echoes the CSRF cookie in the X-CSRF-Token header", async () => {
    // The double-submit pair: the server compares this header against the cookie, so the exact
    // header name and the exact cookie value both matter.
    document.cookie = "tiq_csrf=the-token; path=/";
    stubNavigation();
    const seen = mockLogout();

    render(<LogoutButton />);
    await userEvent.click(screen.getByRole("button", { name: /sign out/i }));

    expect(seen).toHaveLength(1);
    expect(seen[0].csrf).toBe("the-token");
  });

  it("navigates to the IdP end-session URL the server returns", async () => {
    // A real navigation, not a client-side route change: the target is usually another origin, and
    // letting fetch follow it would leave the address bar put and the IdP session alive.
    document.cookie = "tiq_csrf=t; path=/";
    stubNavigation();
    mockLogout({ location: "https://idp.test/realms/acme/logout?id_token_hint=x" });

    render(<LogoutButton />);
    await userEvent.click(screen.getByRole("button", { name: /sign out/i }));

    expect(assign).toHaveBeenCalledWith("https://idp.test/realms/acme/logout?id_token_hint=x");
  });

  it("still gets the user to the login page when the server returns no location", async () => {
    document.cookie = "tiq_csrf=t; path=/";
    stubNavigation();
    mockLogout({});

    render(<LogoutButton />);
    await userEvent.click(screen.getByRole("button", { name: /sign out/i }));

    expect(routerPush).toHaveBeenCalledWith("/login");
    expect(assign).not.toHaveBeenCalled();
  });

  it("does not strand the user when the request fails outright", async () => {
    document.cookie = "tiq_csrf=t; path=/";
    stubNavigation();
    server.use(http.post(`${APP_ORIGIN}/api/auth/logout`, () => HttpResponse.error()));

    render(<LogoutButton />);
    await userEvent.click(screen.getByRole("button", { name: /sign out/i }));

    expect(routerPush).toHaveBeenCalledWith("/login");
  });
});
