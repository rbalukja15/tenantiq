import { render, screen, within } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import { UsageDashboard } from "@/app/components/UsageDashboard";
import type { DailyUsage, UsageDay } from "@/lib/usage";

import { API_ORIGIN } from "./env";
import { server } from "./msw";

/**
 * The usage dashboard (#27). The acceptance criterion: an admin can view usage and cost trends per
 * tenant. There is one role today, so "an admin" is whoever is signed in, and "per tenant" is the
 * tenant they are signed in to: the API only ever reports the caller's own spend.
 *
 * Rendered as a Server Component, the way `TenantHome` is tested: awaited, then rendered, against
 * an MSW stand-in for Django that checks what the page actually asked for.
 */

const DAILY_URL = `${API_ORIGIN}/api/usage/daily`;
const TODAY = "2026-10-22";

function day(date: string, requests = 0, input = 0, output = 0, cost = "0.000000"): UsageDay {
  return {
    date,
    requests,
    input_tokens: input,
    output_tokens: output,
    estimated_cost_usd: cost,
  };
}

const ACME_WEEK: DailyUsage = {
  start: "2026-10-16",
  end: "2026-10-22",
  timezone: "UTC",
  currency: "USD",
  totals: {
    requests: 3,
    input_tokens: 3333,
    output_tokens: 2277,
    estimated_cost_usd: "0.044154",
  },
  days: [
    day("2026-10-16", 2, 3000, 1500, "0.031500"),
    day("2026-10-17"),
    day("2026-10-18"),
    day("2026-10-19", 1, 333, 777, "0.012654"),
    day("2026-10-20"),
    day("2026-10-21"),
    day("2026-10-22"),
  ],
};

/** Answer the daily endpoint, recording each request it received. */
function serve(body: DailyUsage | null, status = 200) {
  const seen: Request[] = [];
  server.use(
    http.get(DAILY_URL, ({ request }) => {
      seen.push(request.clone());
      return body ? HttpResponse.json(body, { status }) : new HttpResponse(null, { status });
    }),
  );
  return seen;
}

describe("UsageDashboard", () => {
  it("shows the range's totals, its charts and every day, exactly", async () => {
    serve(ACME_WEEK);

    render(await UsageDashboard({ accessToken: "token-A", today: TODAY }));

    const tiles = screen.getByLabelText(/^Totals,/);
    expect(within(tiles).getByText("Answers").nextElementSibling).toHaveTextContent("3");
    expect(within(tiles).getByText("Input tokens").nextElementSibling).toHaveTextContent("3,333");
    expect(within(tiles).getByText("Output tokens").nextElementSibling).toHaveTextContent("2,277");
    // Exact below a dollar: rounding to "$0.04" would hide the number the page exists to show.
    expect(within(tiles).getByText("Estimated cost").nextElementSibling).toHaveTextContent(
      "$0.044154",
    );

    // Three charts, each with a sentence a screen reader hears in place of the drawing.
    expect(
      screen.getByRole("img", {
        name: /^Answers per day, .*: 3 in total, the most on Fri, Oct 16, 2026 with 2\.$/,
      }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("img", { name: /3,333 input and 2,277 output in total/ }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("img", {
        name: /\$0\.044154 in total, the most on Fri, Oct 16, 2026 at \$0\.0315\./,
      }),
    ).toBeInTheDocument();

    // The table is the chart's accessible twin: every day, the most recent first.
    const table = screen.getByRole("table", { name: /^Usage per day,/ });
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(7);
    expect(rows[0]).toHaveTextContent("Thu, Oct 22, 2026");
    expect(within(rows[3]).getByRole("rowheader")).toHaveTextContent("Mon, Oct 19, 2026");
    expect(rows[3]).toHaveTextContent("$0.012654");
  });

  it("asks the API for the URL's range, with the session's token, never from a cache", async () => {
    const seen = serve(ACME_WEEK);
    const original = globalThis.fetch;
    const inits: RequestInit[] = [];
    globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
      inits.push(init ?? {});
      return original(input, init);
    }) as typeof fetch;

    try {
      render(
        await UsageDashboard({
          accessToken: "token-A",
          start: "2026-10-16",
          end: "2026-10-22",
          today: TODAY,
        }),
      );
    } finally {
      globalThis.fetch = original;
    }

    expect(seen).toHaveLength(1);
    const url = new URL(seen[0].url);
    expect(url.searchParams.get("start")).toBe("2026-10-16");
    expect(url.searchParams.get("end")).toBe("2026-10-22");
    expect(seen[0].headers.get("authorization")).toBe("Bearer token-A");
    // One tenant's numbers in a shared cache entry is the cross-tenant leak this project exists to
    // make impossible, and MSW cannot see the option, so the spy checks the call itself.
    expect(inits[0].cache).toBe("no-store");
  });

  it("passes an odd range through encoded, for the API to judge", async () => {
    const seen = serve(null, 400);

    render(await UsageDashboard({ accessToken: "token-A", start: "a&end=b", today: TODAY }));

    const url = new URL(seen[0].url);
    expect(url.searchParams.get("start")).toBe("a&end=b");
    expect(url.searchParams.has("end")).toBe(false);
  });

  it("asks for nothing in particular when the URL names no range", async () => {
    const seen = serve({ ...ACME_WEEK, start: "2026-09-23" });

    render(await UsageDashboard({ accessToken: "token-A", today: TODAY }));

    expect(new URL(seen[0].url).search).toBe("");
  });

  it("fills the form with the range the API reported, and marks its preset", async () => {
    // Asked through December; the API reports through today, and the form must say so rather than
    // keep showing a range that was not the one drawn.
    serve(ACME_WEEK);

    render(
      await UsageDashboard({
        accessToken: "token-A",
        start: "2026-10-16",
        end: "2026-12-31",
        today: TODAY,
      }),
    );

    expect(screen.getByLabelText("From")).toHaveValue("2026-10-16");
    expect(screen.getByLabelText("To")).toHaveValue("2026-10-22");
    const presets = screen.getByRole("navigation", { name: "Date range" });
    expect(within(presets).getByRole("link", { name: "Last 7 days" })).toHaveAttribute(
      "aria-current",
      "true",
    );
    expect(within(presets).getByRole("link", { name: "Last 30 days" })).not.toHaveAttribute(
      "aria-current",
    );
    expect(within(presets).getByRole("link", { name: "Last 30 days" })).toHaveAttribute(
      "href",
      "/usage?start=2026-09-23&end=2026-10-22",
    );
  });

  it("never lets the form pick a day after today", async () => {
    serve(ACME_WEEK);

    render(await UsageDashboard({ accessToken: "token-A", today: TODAY }));

    expect(screen.getByLabelText("From")).toHaveAttribute("max", TODAY);
    expect(screen.getByLabelText("To")).toHaveAttribute("max", TODAY);
  });

  it("says how to fix a range the API refused, and keeps what was typed", async () => {
    serve(null, 400);

    render(
      await UsageDashboard({
        accessToken: "token-A",
        start: "2026-10-22",
        end: "2026-10-01",
        today: TODAY,
      }),
    );

    expect(screen.getByRole("status")).toHaveTextContent(
      /Pick a start no later than the end or today/,
    );
    expect(screen.getByLabelText("From")).toHaveValue("2026-10-22");
    expect(screen.getByLabelText("To")).toHaveValue("2026-10-01");
    expect(screen.queryByRole("img")).toBeNull();
  });

  it("shows a generic message on an API failure, without echoing the reason", async () => {
    serve(null, 500);

    render(await UsageDashboard({ accessToken: "token-A", today: TODAY }));

    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("Could not load usage.");
    expect(alert.textContent).not.toMatch(/500/);
  });

  it("redirects to the login page when the API rejects the token", async () => {
    serve(null, 401);

    await expect(UsageDashboard({ accessToken: "expired", today: TODAY })).rejects.toThrow(
      /NEXT_REDIRECT/,
    );
  });

  it("says there is nothing yet, rather than drawing three flat lines", async () => {
    serve({
      ...ACME_WEEK,
      totals: { requests: 0, input_tokens: 0, output_tokens: 0, estimated_cost_usd: "0.000000" },
      days: ACME_WEEK.days.map((d) => day(d.date)),
    });

    render(await UsageDashboard({ accessToken: "token-A", today: TODAY }));

    expect(screen.getByRole("heading", { name: "No answers in this range" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Ask a question" })).toHaveAttribute("href", "/");
    expect(screen.queryByRole("img")).toBeNull();
    expect(screen.queryByRole("table")).toBeNull();
  });

  it("renders each session's own numbers, never the previous one's", async () => {
    // The isolation proof for this render path: two tokens in one process are two upstream calls
    // and two different dashboards. A hoisted response or a shared fetch cache entry would show up
    // here as Globex seeing Acme's spend.
    const seen: (string | null)[] = [];
    server.use(
      http.get(DAILY_URL, ({ request }) => {
        const auth = request.headers.get("authorization");
        seen.push(auth);
        if (auth === "Bearer token-A") return HttpResponse.json(ACME_WEEK);
        return HttpResponse.json({
          ...ACME_WEEK,
          totals: {
            requests: 1,
            input_tokens: 7,
            output_tokens: 7,
            estimated_cost_usd: "0.000126",
          },
          days: [
            ...ACME_WEEK.days.slice(0, 6).map((d) => day(d.date)),
            day(TODAY, 1, 7, 7, "0.000126"),
          ],
        });
      }),
    );

    const cost = () =>
      within(screen.getByLabelText(/^Totals,/)).getByText("Estimated cost").nextElementSibling;

    const first = render(await UsageDashboard({ accessToken: "token-A", today: TODAY }));
    expect(cost()).toHaveTextContent("$0.044154");
    first.unmount();

    render(await UsageDashboard({ accessToken: "token-B", today: TODAY }));
    expect(cost()).toHaveTextContent("$0.000126");
    expect(screen.queryByText("$0.044154")).toBeNull();

    expect(seen).toEqual(["Bearer token-A", "Bearer token-B"]);
  });
});
