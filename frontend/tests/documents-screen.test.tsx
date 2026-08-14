import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { DocumentsScreen } from "@/app/components/DocumentsScreen";

import { server } from "./msw";

/**
 * The document list (#20) — the screen the issue's acceptance criterion is written about: upload,
 * watch the status reach ready, then go and query it.
 *
 * The polling interval is passed in rather than faked with `vi.useFakeTimers()`. Fake timers and
 * `userEvent` disagree about who owns the event loop, and the resulting tests are the kind that pass
 * for reasons nobody can explain; a real 10 ms interval against a fake server is both faster and
 * actually exercises the chained-timeout loop.
 */

const DOCUMENTS_URL = "http://localhost:3000/api/documents";
const DOCUMENT_URL = "http://localhost:3000/api/documents/:id";

const MSA = {
  id: 3,
  title: "MSA_Acme_Northwind_2026.pdf",
  status: "ready",
  error: "",
  size_bytes: 184_320,
  created_at: "2026-08-14T09:12:00Z",
};

const AMENDMENT = {
  id: 4,
  title: "Amendment_2_Payment_Terms.txt",
  status: "processing",
  error: "",
  size_bytes: 2048,
  created_at: "2026-08-14T09:30:00Z",
};

/** Serve a different list on each successive GET, holding on the last one. */
function serveSequence(...pages: unknown[][]) {
  let call = 0;
  const counted = {
    get calls() {
      return call;
    },
  };
  server.use(
    http.get(DOCUMENTS_URL, () => {
      const page = pages[Math.min(call, pages.length - 1)];
      call += 1;
      return HttpResponse.json(page);
    }),
  );
  return counted;
}

beforeEach(() => {
  document.cookie = "tiq_csrf=test-token";
});

afterEach(() => {
  document.cookie = "tiq_csrf=; max-age=0";
});

const rowFor = (title: string) => screen.getByRole("row", { name: new RegExp(title, "i") });

describe("DocumentsScreen", () => {
  it("lists the workspace's documents with their ingestion state", async () => {
    serveSequence([MSA]);

    render(<DocumentsScreen pollIntervalMs={10} />);

    const row = await screen.findByRole("row", { name: /MSA_Acme_Northwind_2026\.pdf/i });
    expect(within(row).getByText("Ready")).toBeInTheDocument();
    expect(within(row).getByText("184.3 kB")).toBeInTheDocument();
    // The machine-readable instant, not the rendered one: that is locale- and timezone-dependent,
    // and asserting on it would pin the suite to whatever CI happens to run under.
    expect(within(row).getByText(/2026/, { selector: "time" })).toHaveAttribute(
      "datetime",
      "2026-08-14T09:12:00Z",
    );
  });

  it("says the corpus is empty when it is empty", async () => {
    serveSequence([]);

    render(<DocumentsScreen pollIntervalMs={10} />);

    expect(await screen.findByRole("heading", { name: /no documents yet/i })).toBeInTheDocument();
  });

  it("does not present a failure to load as an empty corpus", async () => {
    // The single most misleading thing this screen could do: tell a tenant whose documents are all
    // still there that they have none, because one request failed.
    server.use(
      http.get(DOCUMENTS_URL, () =>
        HttpResponse.json({ error: "upstream_unavailable" }, { status: 502 }),
      ),
    );

    render(<DocumentsScreen pollIntervalMs={10} />);

    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(/unavailable/i));
    expect(screen.queryByRole("heading", { name: /no documents yet/i })).toBeNull();
  });

  it("can be retried after a failed load", async () => {
    let attempts = 0;
    server.use(
      http.get(DOCUMENTS_URL, () => {
        attempts += 1;
        return attempts === 1
          ? HttpResponse.json({ error: "upstream_unavailable" }, { status: 502 })
          : HttpResponse.json([MSA]);
      }),
    );
    const user = userEvent.setup();
    render(<DocumentsScreen pollIntervalMs={10} />);
    await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: /try again/i }));

    expect(await screen.findByRole("row", { name: /MSA_Acme/i })).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("does not keep hammering a backend that is down", async () => {
    // A failed first load stops at once and offers the button. Retrying forever against a dead API
    // is how a background tab turns into a denial of service on your own infrastructure.
    let attempts = 0;
    server.use(
      http.get(DOCUMENTS_URL, () => {
        attempts += 1;
        return HttpResponse.json({ error: "upstream_unavailable" }, { status: 502 });
      }),
    );

    render(<DocumentsScreen pollIntervalMs={10} />);
    await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());
    await new Promise((resolve) => setTimeout(resolve, 120));

    expect(attempts).toBe(1);
  });

  it("follows a document to ready without the user reloading", async () => {
    // The issue's acceptance criterion, at the layer that has to deliver it: ingestion is
    // asynchronous, so the row has to learn about a transition this screen did not cause.
    serveSequence(
      [{ ...AMENDMENT, status: "pending" }],
      [AMENDMENT],
      [{ ...AMENDMENT, status: "ready" }],
    );

    render(<DocumentsScreen pollIntervalMs={10} />);

    expect(await screen.findByText("Pending")).toBeInTheDocument();
    expect(await screen.findByText("Ready")).toBeInTheDocument();
  });

  it("announces that a document became ready", async () => {
    // The user is very likely looking somewhere else by now — that is the whole reason to poll.
    serveSequence([{ ...AMENDMENT, status: "processing" }], [{ ...AMENDMENT, status: "ready" }]);

    render(<DocumentsScreen pollIntervalMs={10} />);

    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent(
        /Amendment_2_Payment_Terms\.txt is ready to query/i,
      ),
    );
  });

  it("does not announce the documents that were already there", async () => {
    // Announcing every row on first load buries the one line that matters under the whole corpus.
    serveSequence([MSA]);

    render(<DocumentsScreen pollIntervalMs={10} />);
    await screen.findByRole("row", { name: /MSA_Acme/i });

    expect(screen.getByRole("status")).toHaveTextContent("");
  });

  it("stops polling once nothing is moving", async () => {
    // Otherwise an idle tab asks for the list every few seconds forever, for every signed-in user.
    const counted = serveSequence([MSA]);

    render(<DocumentsScreen pollIntervalMs={10} />);
    await screen.findByRole("row", { name: /MSA_Acme/i });
    await new Promise((resolve) => setTimeout(resolve, 120));

    expect(counted.calls).toBe(1);
  });

  it("shows why a document failed, not just that it did", async () => {
    serveSequence([{ ...MSA, status: "failed", error: "No extractable text in this PDF." }]);

    render(<DocumentsScreen pollIntervalMs={10} />);

    const row = await screen.findByRole("row", { name: /MSA_Acme/i });
    expect(within(row).getByText("Failed")).toBeInTheDocument();
    expect(within(row).getByText(/no extractable text/i)).toBeInTheDocument();
  });

  it("asks before deleting, and says what deleting destroys", async () => {
    // Deleting takes the chunks with it (#51), so answers already on screen lose their sources. A
    // one-click delete of a whole document with no confirmation is not a thing to ship.
    serveSequence([MSA]);
    const user = userEvent.setup();
    render(<DocumentsScreen pollIntervalMs={10} />);
    await screen.findByRole("row", { name: /MSA_Acme/i });

    await user.click(screen.getByRole("button", { name: `Delete ${MSA.title}` }));

    expect(screen.getByText(/removes every passage retrieved from it/i)).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: `Confirm deleting ${MSA.title}` }),
    ).toBeInTheDocument();
  });

  it("does not delete anything when the confirmation is declined", async () => {
    // No DELETE handler is registered: a request here trips the suite's unhandled-request guard.
    serveSequence([MSA]);
    const user = userEvent.setup();
    render(<DocumentsScreen pollIntervalMs={10} />);
    await screen.findByRole("row", { name: /MSA_Acme/i });

    await user.click(screen.getByRole("button", { name: `Delete ${MSA.title}` }));
    await user.click(screen.getByRole("button", { name: `Keep ${MSA.title}` }));

    expect(screen.getByRole("row", { name: /MSA_Acme/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: `Delete ${MSA.title}` })).toBeInTheDocument();
  });

  it("deletes a document and takes its row with it", async () => {
    let deleted: string | null = null;
    let csrf: string | null = null;
    let listed = 0;
    server.use(
      http.get(DOCUMENTS_URL, () => {
        listed += 1;
        return HttpResponse.json(listed === 1 ? [MSA] : []);
      }),
      http.delete(DOCUMENT_URL, ({ params, request }) => {
        deleted = String(params.id);
        csrf = request.headers.get("x-csrf-token");
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const user = userEvent.setup();
    render(<DocumentsScreen pollIntervalMs={10} />);
    await screen.findByRole("row", { name: /MSA_Acme/i });

    await user.click(screen.getByRole("button", { name: `Delete ${MSA.title}` }));
    await user.click(screen.getByRole("button", { name: `Confirm deleting ${MSA.title}` }));

    await waitFor(() => expect(screen.queryByRole("row", { name: /MSA_Acme/i })).toBeNull());
    expect(deleted).toBe("3");
    expect(csrf).toBe("test-token");
  });

  it("keeps the row when the delete was refused", async () => {
    // The opposite failure of the one above, and the more dangerous one: a row that vanishes from a
    // delete the server rejected tells the user their document is gone when it is still there.
    serveSequence([MSA]);
    server.use(
      http.delete(DOCUMENT_URL, () => HttpResponse.json({ error: "forbidden" }, { status: 403 })),
    );
    const user = userEvent.setup();
    render(<DocumentsScreen pollIntervalMs={10} />);
    await screen.findByRole("row", { name: /MSA_Acme/i });

    await user.click(screen.getByRole("button", { name: `Delete ${MSA.title}` }));
    await user.click(screen.getByRole("button", { name: `Confirm deleting ${MSA.title}` }));

    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(/stale|reload/i));
    expect(rowFor("MSA_Acme")).toBeInTheDocument();
  });

  it("shows a newly uploaded document without a reload", async () => {
    let listed = 0;
    server.use(
      http.get(DOCUMENTS_URL, () => {
        listed += 1;
        return HttpResponse.json(listed === 1 ? [] : [{ ...MSA, status: "pending" }]);
      }),
      http.post(DOCUMENTS_URL, () =>
        HttpResponse.json({ ...MSA, status: "pending" }, { status: 201 }),
      ),
    );
    const user = userEvent.setup();
    render(<DocumentsScreen pollIntervalMs={10} />);
    await screen.findByRole("heading", { name: /no documents yet/i });

    await user.upload(
      screen.getByLabelText(/add a document/i),
      new File(["contract"], "MSA_Acme_Northwind_2026.pdf", { type: "application/pdf" }),
    );
    await user.click(screen.getByRole("button", { name: /^upload$/i }));

    expect(await screen.findByRole("row", { name: /MSA_Acme/i })).toBeInTheDocument();
  });
});
