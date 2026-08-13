import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import { QueryError, streamAnswer } from "@/lib/query";
import { server } from "./msw";

/**
 * The client half of the streaming query (#19, ADR-0009).
 *
 * It talks to the BFF's own origin (`/api/query`), never the API directly — the proxy is what holds
 * the tenant's bearer token. Everything here is about the shapes that arrive *instead of* an answer:
 * a session that ended, a tenant over its quota, a model that failed mid-stream. Each has a
 * different right response in the UI, so each has to be distinguishable here rather than collapsing
 * into "something went wrong".
 */

const QUERY_URL = "http://localhost:3000/api/query";

/** An SSE response whose body streams the given frames. */
function sse(...frames: string[]) {
  const stream = new ReadableStream({
    start(controller) {
      const encoder = new TextEncoder();
      for (const frame of frames) controller.enqueue(encoder.encode(frame));
      controller.close();
    },
  });
  return new HttpResponse(stream, { headers: { "content-type": "text/event-stream" } });
}

/** One frame per read, so the body is genuinely still arriving while the test runs. */
function lazySse(...frames: string[]) {
  let next = 0;
  const encoder = new TextEncoder();
  const stream = new ReadableStream({
    async pull(controller) {
      if (next >= frames.length) return controller.close();
      await new Promise((resolve) => setTimeout(resolve, 5));
      controller.enqueue(encoder.encode(frames[next++]));
    },
  });
  return new HttpResponse(stream, { headers: { "content-type": "text/event-stream" } });
}

function token(text: string) {
  return `event: token\ndata: ${JSON.stringify({ text })}\n\n`;
}

async function collect(question = "when is payment due") {
  const events = [];
  for await (const event of streamAnswer(question, { csrfToken: "tok" })) events.push(event);
  return events;
}

describe("streamAnswer", () => {
  it("posts the question to the BFF with the CSRF header", async () => {
    let seen: { body: unknown; csrf: string | null; contentType: string | null } | undefined;
    server.use(
      http.post(QUERY_URL, async ({ request }) => {
        seen = {
          body: await request.json(),
          csrf: request.headers.get("x-csrf-token"),
          contentType: request.headers.get("content-type"),
        };
        return sse('event: citations\ndata: {"citations":[]}\n\n');
      }),
    );

    await collect("what are the payment terms?");

    expect(seen?.body).toEqual({ question: "what are the payment terms?" });
    // Without this header the proxy answers 403 — the double-submit half that script must supply.
    expect(seen?.csrf).toBe("tok");
    expect(seen?.contentType).toContain("application/json");
  });

  it("yields tokens in order, then the citations", async () => {
    server.use(
      http.post(QUERY_URL, () =>
        sse(
          token("Invoices are "),
          token("payable net 30.[1]"),
          `event: citations\ndata: ${JSON.stringify({
            citations: [
              {
                number: 1,
                chunk_id: 12,
                document_id: 3,
                document_title: "MSA.pdf",
                chunk_index: 4,
                start_offset: 4821,
                end_offset: 5190,
              },
            ],
          })}\n\n`,
        ),
      ),
    );

    const events = await collect();

    expect(events).toEqual([
      { type: "token", text: "Invoices are " },
      { type: "token", text: "payable net 30.[1]" },
      {
        type: "citations",
        refused: false,
        citations: [
          {
            number: 1,
            chunk_id: 12,
            document_id: 3,
            document_title: "MSA.pdf",
            chunk_index: 4,
            start_offset: 4821,
            end_offset: 5190,
          },
        ],
      },
    ]);
  });

  it("carries the refusal flag so a refusal is not mistaken for a thin answer", async () => {
    // The two ways citations can be empty are different products of the system: refusing for lack
    // of evidence, versus answering with markers that resolved to nothing. They render differently.
    server.use(
      http.post(QUERY_URL, () =>
        sse(
          token("I don't have relevant information"),
          'event: citations\ndata: {"citations":[],"refused":true}\n\n',
        ),
      ),
    );

    const events = await collect();

    expect(events.at(-1)).toEqual({ type: "citations", citations: [], refused: true });
  });

  it("treats a citations frame with no refusal flag as an answer, not a refusal", async () => {
    // Fail safe in the direction that shows the reader real text: a missing flag must never blank
    // an answer that was actually produced.
    server.use(http.post(QUERY_URL, () => sse('event: citations\ndata: {"citations":[]}\n\n')));

    expect(await collect()).toEqual([{ type: "citations", citations: [], refused: false }]);
  });

  it("surfaces a mid-stream error frame as an event, not a throw", async () => {
    // Tokens already reached the reader, so this is not an exception — it is the end of a partial
    // answer, and the UI has to keep what arrived while saying it did not finish.
    server.use(
      http.post(QUERY_URL, () =>
        sse(
          token("Invoices are "),
          'event: error\ndata: {"message":"Answer generation failed."}\n\n',
        ),
      ),
    );

    expect(await collect()).toEqual([
      { type: "token", text: "Invoices are " },
      { type: "error", message: "Answer generation failed." },
    ]);
  });

  it("ends an empty stream without inventing an answer", async () => {
    server.use(http.post(QUERY_URL, () => sse()));

    expect(await collect()).toEqual([]);
  });

  it("treats an unparseable frame as a protocol failure rather than dropping it", async () => {
    // Silently skipping would truncate the answer invisibly — the one failure mode a grounded
    // product cannot have, because the result still looks like a complete answer.
    server.use(
      http.post(QUERY_URL, () => sse(token("Invoices "), "event: token\ndata: {oops\n\n")),
    );

    const events = await collect();

    expect(events[0]).toEqual({ type: "token", text: "Invoices " });
    expect(events[1]).toMatchObject({ type: "error" });
  });

  it("raises session_expired on a 401 so the page can send the user to log in", async () => {
    server.use(
      http.post(QUERY_URL, () => HttpResponse.json({ error: "session_expired" }, { status: 401 })),
    );

    await expect(collect()).rejects.toMatchObject({ kind: "session_expired" });
  });

  it("raises rate_limited on a 429 and keeps the Retry-After", async () => {
    server.use(
      http.post(QUERY_URL, () =>
        HttpResponse.json(
          { detail: "Request was throttled." },
          {
            status: 429,
            headers: { "retry-after": "42" },
          },
        ),
      ),
    );

    await expect(collect()).rejects.toMatchObject({ kind: "rate_limited", retryAfterSeconds: 42 });
  });

  it("raises rate_limited even when Retry-After is absent", async () => {
    server.use(http.post(QUERY_URL, () => HttpResponse.json({}, { status: 429 })));

    await expect(collect()).rejects.toMatchObject({
      kind: "rate_limited",
      retryAfterSeconds: undefined,
    });
  });

  it("raises invalid_question on a 400 and carries the reason", async () => {
    server.use(
      http.post(QUERY_URL, () =>
        HttpResponse.json({ detail: "A non-empty 'question' is required." }, { status: 400 }),
      ),
    );

    await expect(collect()).rejects.toMatchObject({
      kind: "invalid_question",
      message: "A non-empty 'question' is required.",
    });
  });

  it("raises unavailable when the proxy cannot reach the API", async () => {
    server.use(
      http.post(QUERY_URL, () =>
        HttpResponse.json({ error: "upstream_unavailable" }, { status: 502 }),
      ),
    );

    await expect(collect()).rejects.toMatchObject({ kind: "unavailable" });
  });

  it("raises unavailable when the identity provider is down", async () => {
    server.use(
      http.post(QUERY_URL, () =>
        HttpResponse.json({ error: "refresh_unavailable" }, { status: 503 }),
      ),
    );

    await expect(collect()).rejects.toMatchObject({ kind: "unavailable" });
  });

  it("raises rather than silently succeeding when a 200 is not an event stream", async () => {
    // A misconfigured proxy that buffers the response into JSON would otherwise look like an answer
    // with no tokens at all — an empty answer is indistinguishable from a refusal to the user.
    server.use(http.post(QUERY_URL, () => HttpResponse.json({ answer: "not streamed" })));

    await expect(collect()).rejects.toBeInstanceOf(QueryError);
  });

  it("stops reading when the caller aborts", async () => {
    // Deliberately a *lazy* stream. Enqueuing every frame up front (as `sse` does) buffers the whole
    // body before the first read, so the answer arrives in full whether abort works or not — the
    // test would pass on a client that ignored the signal entirely.
    server.use(http.post(QUERY_URL, () => lazySse(token("one"), token("two"), token("three"))));
    const controller = new AbortController();

    const seen = [];
    for await (const event of streamAnswer("q", { csrfToken: "tok", signal: controller.signal })) {
      seen.push(event);
      if (seen.length === 1) controller.abort();
    }

    expect(seen).toHaveLength(1);
  });
});
