import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { AskScreen } from "@/app/components/AskScreen";
import { server } from "./msw";

/**
 * The ask screen (#19) — the product's signature surface.
 *
 * Semantics only, as everywhere in this suite: jsdom applies no CSS Module, so the layout is proven
 * by `next build` and screenshots. What is asserted here is what actually goes wrong — whether the
 * answer appears as it streams, whether a citation is only ever offered when it resolves to real
 * evidence, and whether a refusal is unmistakably a refusal rather than a thin-looking answer.
 */

const QUERY_URL = "http://localhost:3000/api/query";
const CHUNK_URL = "http://localhost:3000/api/chunks/:id";

function frames(...parts: string[]) {
  const stream = new ReadableStream({
    start(controller) {
      const encoder = new TextEncoder();
      for (const part of parts) controller.enqueue(encoder.encode(part));
      controller.close();
    },
  });
  return new HttpResponse(stream, { headers: { "content-type": "text/event-stream" } });
}

const token = (text: string) => `event: token\ndata: ${JSON.stringify({ text })}\n\n`;

const citations = (list: unknown[], refused = false) =>
  `event: citations\ndata: ${JSON.stringify({ citations: list, refused })}\n\n`;

const CITATION_ONE = {
  number: 1,
  chunk_id: 12,
  document_id: 3,
  document_title: "MSA_Acme_Northwind_2026.pdf",
  chunk_index: 4,
  start_offset: 4821,
  end_offset: 5190,
};

const CHUNK_ONE = {
  id: 12,
  index: 4,
  text: "Customer shall pay each undisputed invoice within thirty (30) days of the invoice date.",
  char_count: 87,
  start_offset: 4821,
  end_offset: 5190,
  document: { id: 3, title: "MSA_Acme_Northwind_2026.pdf" },
};

/** The double-submit cookie the client must echo; without it the proxy answers 403. */
beforeEach(() => {
  document.cookie = "tiq_csrf=test-token";
});

afterEach(() => {
  document.cookie = "tiq_csrf=; max-age=0";
});

async function ask(question = "what are the payment terms?") {
  const user = userEvent.setup();
  render(<AskScreen />);
  await user.type(screen.getByLabelText(/question/i), question);
  await user.click(screen.getByRole("button", { name: /ask/i }));
  return user;
}

describe("AskScreen", () => {
  it("invites a question before anything has been asked", () => {
    render(<AskScreen />);

    expect(screen.getByLabelText(/question/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /ask/i })).toHaveAttribute("type", "submit");
  });

  it("streams the answer into view", async () => {
    server.use(
      http.post(QUERY_URL, () =>
        frames(token("Invoices are payable "), token("net 30 days."), citations([])),
      ),
    );

    await ask();

    await waitFor(() =>
      expect(screen.getByText(/Invoices are payable net 30 days\./)).toBeInTheDocument(),
    );
  });

  it("sends the question the user typed", async () => {
    let body: unknown;
    server.use(
      http.post(QUERY_URL, async ({ request }) => {
        body = await request.json();
        return frames(citations([]));
      }),
    );

    await ask("when is payment due?");

    await waitFor(() => expect(body).toEqual({ question: "when is payment due?" }));
  });

  it("will not send an empty question", async () => {
    // No handler is registered: if a request were made, the suite's unhandled-request guard fails.
    const user = userEvent.setup();
    render(<AskScreen />);

    await user.click(screen.getByRole("button", { name: /ask/i }));

    expect(screen.getByLabelText(/question/i)).toBeInvalid();
  });

  it("shows the cited passage beside the answer", async () => {
    server.use(
      http.post(QUERY_URL, () => frames(token("Net 30 applies.[1]"), citations([CITATION_ONE]))),
      http.get(CHUNK_URL, () => HttpResponse.json(CHUNK_ONE)),
    );

    await ask();

    // The whole point of the screen: the claim and the evidence for it, on screen together.
    await waitFor(() => expect(screen.getByText(CHUNK_ONE.text)).toBeInTheDocument());
    expect(screen.getByText("MSA_Acme_Northwind_2026.pdf")).toBeInTheDocument();
  });

  it("offers a citation control only for a marker that resolved", async () => {
    // The model wrote [1] and [9]; only [1] was retrieved. A chip for [9] would be a dead citation.
    server.use(
      http.post(QUERY_URL, () =>
        frames(token("Both [1] and [9] say so."), citations([CITATION_ONE])),
      ),
      http.get(CHUNK_URL, () => HttpResponse.json(CHUNK_ONE)),
    );

    await ask();

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Source 1" })).toBeInTheDocument(),
    );
    expect(screen.queryByRole("button", { name: "Source 9" })).toBeNull();
    expect(screen.getByText(/\[9\]/)).toBeInTheDocument(); // still readable as written
  });

  it("links a citation to its source card", async () => {
    server.use(
      http.post(QUERY_URL, () => frames(token("Net 30.[1]"), citations([CITATION_ONE]))),
      http.get(CHUNK_URL, () => HttpResponse.json(CHUNK_ONE)),
    );

    const user = await ask();
    const chip = await screen.findByRole("button", { name: "Source 1" });

    await user.click(chip);

    expect(chip).toHaveAttribute("aria-pressed", "true");
  });

  it("says plainly when nothing was found, instead of showing a bare answer", async () => {
    server.use(
      http.post(QUERY_URL, () =>
        frames(
          token("I don't have relevant information in your documents to answer that question."),
          citations([], true),
        ),
      ),
    );

    await ask();

    // The refusal state (#74), not the answer pane: it must never read as a confident answer that
    // simply happens to have no sources.
    await waitFor(() =>
      expect(
        screen.getByRole("heading", { name: /no supporting passage found/i }),
      ).toBeInTheDocument(),
    );
  });

  it("keeps a partial answer when generation fails mid-stream", async () => {
    server.use(
      http.post(QUERY_URL, () =>
        frames(
          token("Invoices are payable "),
          'event: error\ndata: {"message":"Answer generation failed. Please try again."}\n\n',
        ),
      ),
    );

    await ask();

    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(/generation failed/i));
    // The tokens that did arrive are real output and must not be thrown away.
    expect(screen.getByText(/Invoices are payable/)).toBeInTheDocument();
  });

  it("reports a quota refusal as something to wait out, not a broken app", async () => {
    server.use(
      http.post(QUERY_URL, () =>
        HttpResponse.json(
          { detail: "Request was throttled." },
          {
            status: 429,
            headers: { "retry-after": "30" },
          },
        ),
      ),
    );

    await ask();

    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(/throttled|wait/i));
  });

  it("tells the user to sign in again when the session has ended", async () => {
    server.use(
      http.post(QUERY_URL, () => HttpResponse.json({ error: "session_expired" }, { status: 401 })),
    );

    await ask();

    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(/sign in/i));
  });

  it("does not make the streaming answer itself a live region", async () => {
    // `aria-live` on the growing answer is the classic screen-reader disaster: the node's entire
    // text is re-announced on every token, so the listener hears the answer restart from the
    // beginning dozens of times and never reaches the end. The answer is read on demand; only the
    // state changes are announced.
    server.use(http.post(QUERY_URL, () => frames(token("Invoices are payable "), citations([]))));

    await ask();

    const answer = await screen.findByText(/Invoices are payable/);
    expect(answer.closest("[aria-live]")).toBeNull();
  });

  it("announces that the answer finished, once", async () => {
    server.use(
      http.post(QUERY_URL, () => frames(token("Net 30.[1]"), citations([CITATION_ONE]))),
      http.get(CHUNK_URL, () => HttpResponse.json(CHUNK_ONE)),
    );

    await ask();

    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent(/answer complete/i));
  });

  it("announces a refusal", async () => {
    // The refusal region cannot announce itself: a live region only reports mutations made *after*
    // it is registered, and NoEvidence is inserted into the DOM already containing its heading text.
    // A region that is always mounted is what actually speaks.
    server.use(
      http.post(QUERY_URL, () =>
        frames(token("I don't have relevant information"), citations([], true)),
      ),
    );

    await ask();

    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent(/no supporting passage/i),
    );
  });

  it("does not present a truncated answer as a finished one", async () => {
    // The backend always ends a stream with a terminal frame — citations, or error (ADR-0009). If
    // the body simply stops after some tokens, the connection died mid-answer. Rendering that as a
    // completed answer is the single worst failure this UI can have: the text looks whole, and the
    // reader has no way to know a sentence went missing.
    server.use(http.post(QUERY_URL, () => frames(token("Invoices are payable net "))));

    await ask();

    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent(/incomplete|interrupted/i),
    );
    expect(screen.getByText(/Invoices are payable net/)).toBeInTheDocument();
  });

  it("labels the quote with the offsets that belong to it", async () => {
    // The citation frame and the chunk endpoint are two reads of the same row, and a re-ingestion
    // between them can move a chunk's span (#45). The offsets shown must be the ones that describe
    // the text on screen, or the card asserts a span its own quote does not occupy.
    server.use(
      http.post(QUERY_URL, () => frames(token("Net 30.[1]"), citations([CITATION_ONE]))),
      http.get(CHUNK_URL, () =>
        HttpResponse.json({ ...CHUNK_ONE, start_offset: 100, end_offset: 187 }),
      ),
    );

    await ask();

    await waitFor(() => expect(screen.getByText(/100.187/)).toBeInTheDocument());
  });

  it("does not call a passage deleted when it merely failed to load", async () => {
    // `resolveEvidence` distinguishes a 404 (the chunk is genuinely gone) from any other failure,
    // and the screen must keep that distinction. Telling a user their document was deleted because
    // of a transient 500 is not a cosmetic slip — it is the UI stating something false about their
    // data, and the obvious "just show the fallback" catch-all is exactly how it happens.
    server.use(
      http.post(QUERY_URL, () => frames(token("Net 30.[1]"), citations([CITATION_ONE]))),
      http.get(CHUNK_URL, () => new HttpResponse(null, { status: 500 })),
    );

    await ask();

    await waitFor(() => expect(screen.getByText(/could not be loaded/i)).toBeInTheDocument());
    expect(screen.queryByText(/has been deleted/i)).toBeNull();
  });

  it("says the evidence is gone when its document was deleted since the answer", async () => {
    // ADR-0015 allows deleting a document while its answer is still on screen, so this is a real
    // state, not a hypothetical — and it must not look like a loading spinner that never resolves.
    server.use(
      http.post(QUERY_URL, () => frames(token("Net 30.[1]"), citations([CITATION_ONE]))),
      http.get(CHUNK_URL, () => HttpResponse.json({ detail: "Not found." }, { status: 404 })),
    );

    await ask();

    await waitFor(() =>
      expect(screen.getByText(/no longer available|has been deleted/i)).toBeInTheDocument(),
    );
  });
});
