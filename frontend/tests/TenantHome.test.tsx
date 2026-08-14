import { render, screen } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import { TenantHome } from "@/app/components/TenantHome";

import { API_ORIGIN } from "./env";
import { server } from "./msw";

/** Answer `/api/me` according to which tenant's bearer token arrived. */
function mockMe() {
  const seen: (string | null)[] = [];
  server.use(
    http.get(`${API_ORIGIN}/api/me`, ({ request }) => {
      const auth = request.headers.get("authorization");
      seen.push(auth);
      if (auth === "Bearer token-A") {
        return HttpResponse.json({
          // The realistic payload: `username` is the synthesized identity key, `display_name` is
          // what a person is called. A fixture where both read "alice" cannot fail on the bug.
          username: "abc-123.6fa97fcf2c06",
          display_name: "alice",
          email: "alice@acme.test",
          tenant: { id: "t-1", slug: "acme", name: "Acme" },
        });
      }
      return HttpResponse.json({
        username: "def-456.9c1b20e4aa71",
        display_name: "bob",
        email: "bob@globex.test",
        tenant: { id: "t-2", slug: "globex", name: "Globex" },
      });
    }),
  );
  return seen;
}

describe("TenantHome", () => {
  it("shows the signed-in user's tenant", async () => {
    // Acceptance criterion: an authenticated user sees their tenant.
    mockMe();

    render(await TenantHome({ accessToken: "token-A" }));

    expect(screen.getByRole("heading")).toHaveTextContent("Acme");
    expect(screen.getByText(/Signed in as/)).toHaveTextContent("alice");
    expect(screen.getByText("acme")).toBeInTheDocument();
  });

  it("greets the person by name, never by their identity key", async () => {
    // The bug this pins shipped in #18 and survived until someone finally looked at the running app
    // (#84): the shell rendered `username`, which is the synthesized `<sub>.<issuer-hash>` key, so a
    // signed-in Alice was greeted as "c76c642e-…-8e0ad55a57f4.6fa97fcf2c06". No test could catch it
    // while the fixture used "alice" for both fields.
    mockMe();

    render(await TenantHome({ accessToken: "token-A" }));

    expect(screen.getByText(/Signed in as/)).toHaveTextContent("alice");
    expect(screen.queryByText(/abc-123\.6fa97fcf2c06/)).toBeNull();
  });

  it("says only that you are signed in when the token carries no name", async () => {
    // A minimal client scope sends `sub` and nothing else. "Signed in" is honest; reaching for
    // `username` to fill the gap is how the original bug comes back.
    server.use(
      http.get(`${API_ORIGIN}/api/me`, () =>
        HttpResponse.json({
          username: "abc-123.6fa97fcf2c06",
          display_name: "",
          email: "",
          tenant: { id: "t-1", slug: "acme", name: "Acme" },
        }),
      ),
    );

    render(await TenantHome({ accessToken: "token-A" }));

    expect(screen.getByText("Signed in")).toBeInTheDocument();
    expect(screen.queryByText(/abc-123/)).toBeNull();
  });

  it("renders each session's own tenant, never the previous one's", async () => {
    // The isolation proof for this render path: two tokens in one process must produce two distinct
    // upstream calls and two distinct tenants. A hoisted response or a shared fetch cache entry
    // would show up here as Globex seeing Acme's name.
    const seen = mockMe();

    const first = render(await TenantHome({ accessToken: "token-A" }));
    expect(screen.getByRole("heading")).toHaveTextContent("Acme");
    first.unmount();

    render(await TenantHome({ accessToken: "token-B" }));
    expect(screen.getByRole("heading")).toHaveTextContent("Globex");

    expect(seen).toEqual(["Bearer token-A", "Bearer token-B"]);
  });

  it("redirects to the login page when the API rejects the token", async () => {
    // `redirect()` signals by throwing, so the assertion is that it throws NEXT_REDIRECT rather than
    // quietly rendering an error state that leaves the user stuck on a page they cannot use.
    server.use(http.get(`${API_ORIGIN}/api/me`, () => new HttpResponse(null, { status: 401 })));

    await expect(TenantHome({ accessToken: "expired" })).rejects.toThrow(/NEXT_REDIRECT/);
  });

  it("shows a generic message on an API failure, without echoing the reason", async () => {
    server.use(http.get(`${API_ORIGIN}/api/me`, () => new HttpResponse(null, { status: 500 })));

    render(await TenantHome({ accessToken: "token-A" }));

    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("Could not load your session.");
    expect(alert.textContent).not.toMatch(/500/);
  });
});

describe("TenantHome — caching", () => {
  it("opts the identity fetch out of the fetch cache", async () => {
    // An Authorization-bearing GET is not unconditionally excluded from Next's fetch cache, and one
    // tenant's identity landing in a shared entry is precisely the cross-tenant leak this project
    // exists to make impossible. MSW cannot observe the `cache` option, so the spy checks the call.
    mockMe();
    const original = globalThis.fetch;
    const seen: RequestInit[] = [];
    globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
      seen.push(init ?? {});
      return original(input, init);
    }) as typeof fetch;

    try {
      render(await TenantHome({ accessToken: "token-A" }));
    } finally {
      globalThis.fetch = original;
    }

    expect(seen).toHaveLength(1);
    expect(seen[0].cache).toBe("no-store");
  });
});
