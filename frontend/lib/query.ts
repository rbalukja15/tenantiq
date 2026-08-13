import { readSseFrames } from "@/lib/sse";

/**
 * The client half of the streaming query (#19, ADR-0009).
 *
 * Calls the BFF's **own origin** (`/api/query`), never the API directly: the proxy is what holds the
 * tenant's bearer token, and moving that into the browser would undo ADR-0013 entirely.
 *
 * The shape of this module is driven by what arrives *instead of* an answer. A session that ended, a
 * tenant over quota, an IdP outage and a model that failed mid-sentence each call for a different
 * response on screen — one to log in again, one to wait, one to retry, one to keep the partial text.
 * Collapsing them into "something went wrong" would be the easy version and the wrong one.
 *
 * The split between *throwing* and *yielding an error event* is the load-bearing distinction:
 * nothing was delivered → throw, because there is no answer to preserve; tokens were already
 * delivered → yield, because the reader is looking at real text that must not vanish.
 */

/** One resolved citation, exactly as `app.generation.Citation` serialises it. No chunk text — #51's
 *  `GET /api/chunks/<id>` is what turns this into readable evidence. */
export type Citation = {
  number: number;
  chunk_id: number;
  document_id: number;
  document_title: string;
  chunk_index: number;
  start_offset: number;
  end_offset: number;
};

export type AnswerEvent =
  | { type: "token"; text: string }
  | { type: "citations"; citations: Citation[]; refused: boolean }
  | { type: "error"; message: string };

export type QueryErrorKind =
  "session_expired" | "rate_limited" | "invalid_question" | "unavailable" | "protocol";

/** A failure that happened *before* any of the answer arrived, so there is nothing to keep. */
export class QueryError extends Error {
  readonly kind: QueryErrorKind;
  /** Present only when the server said how long to wait (429 + `Retry-After`). */
  readonly retryAfterSeconds?: number;

  constructor(kind: QueryErrorKind, message: string, retryAfterSeconds?: number) {
    super(message);
    this.name = "QueryError";
    this.kind = kind;
    this.retryAfterSeconds = retryAfterSeconds;
  }
}

/** `Retry-After` in its delay-seconds form. The HTTP-date form is valid but DRF never sends it. */
function retryAfterSeconds(response: Response): number | undefined {
  const header = response.headers.get("retry-after");
  if (header === null) return undefined;
  const seconds = Number.parseInt(header, 10);
  return Number.isFinite(seconds) ? seconds : undefined;
}

async function detailOf(response: Response, fallback: string): Promise<string> {
  try {
    const body = await response.json();
    const detail = (body as { detail?: unknown })?.detail;
    return typeof detail === "string" && detail.length > 0 ? detail : fallback;
  } catch {
    return fallback;
  }
}

async function failureFor(response: Response): Promise<QueryError> {
  if (response.status === 401) {
    return new QueryError("session_expired", "Your session has ended. Please sign in again.");
  }
  if (response.status === 429) {
    return new QueryError(
      "rate_limited",
      await detailOf(response, "Too many requests. Please wait a moment."),
      retryAfterSeconds(response),
    );
  }
  if (response.status === 400) {
    return new QueryError(
      "invalid_question",
      await detailOf(response, "That question was rejected."),
    );
  }
  return new QueryError("unavailable", "TenantIQ is temporarily unavailable. Please try again.");
}

/**
 * A terminal citations frame.
 *
 * `refused` says *why* the list is empty, which the list itself cannot: refusing for lack of
 * retrieved evidence is the product working, while an answer whose `[n]` markers all failed to
 * resolve is still an answer, and the two are rendered completely differently. Absence defaults to
 * `false` — fail safe in the direction that keeps showing real text, never one that blanks an answer
 * the model actually produced.
 */
function parseCitations(data: string): { citations: Citation[]; refused: boolean } {
  const payload = JSON.parse(data) as { citations?: unknown; refused?: unknown };
  return {
    citations: Array.isArray(payload.citations) ? (payload.citations as Citation[]) : [],
    refused: payload.refused === true,
  };
}

/**
 * Ask a question and stream the grounded answer.
 *
 * @param csrfToken the double-submit token read from the CSRF cookie at call time — read *fresh*
 *   rather than captured at render, so a token rotated by a login in another tab does not turn the
 *   next question into a 403.
 */
export async function* streamAnswer(
  question: string,
  options: { csrfToken: string; signal?: AbortSignal },
): AsyncGenerator<AnswerEvent> {
  let response: Response;
  try {
    response = await fetch("/api/query", {
      method: "POST",
      headers: { "content-type": "application/json", "x-csrf-token": options.csrfToken },
      body: JSON.stringify({ question }),
      signal: options.signal,
    });
  } catch {
    if (options.signal?.aborted) return;
    throw new QueryError("unavailable", "TenantIQ could not be reached. Please try again.");
  }

  if (!response.ok) throw await failureFor(response);

  // A 200 that is not an event stream means something buffered the response — a proxy, a
  // misconfiguration. Reading it as a stream would yield no tokens at all, which on screen is
  // indistinguishable from the model having nothing to say.
  if (!response.headers.get("content-type")?.includes("text/event-stream")) {
    throw new QueryError("protocol", "The server did not return a streamed answer.");
  }
  if (response.body === null) {
    throw new QueryError("protocol", "The server returned an empty response.");
  }

  try {
    for await (const frame of readSseFrames(response.body)) {
      // Checked here rather than relying on `fetch` to tear down the body stream. Whether an abort
      // errors an in-flight read depends on the runtime, and any bytes already buffered would be
      // delivered regardless — so the only reliable place to stop is the loop itself.
      if (options.signal?.aborted) return;
      if (frame.event === "token") {
        yield { type: "token", text: (JSON.parse(frame.data) as { text: string }).text };
      } else if (frame.event === "citations") {
        yield { type: "citations", ...parseCitations(frame.data) };
      } else if (frame.event === "error") {
        yield { type: "error", message: (JSON.parse(frame.data) as { message: string }).message };
        return;
      }
      // Any other event name is ignored: a future frame type must not break an existing client.
    }
  } catch {
    // An abort is the caller's own doing, not a failure to report.
    if (options.signal?.aborted) return;
    // Everything else — a dropped connection, a frame that will not parse — ends the answer here.
    // Skipping the bad frame and carrying on would truncate the answer *invisibly*, which in a
    // product whose claim is faithfulness is the one failure that must never look like success.
    yield { type: "error", message: "The answer was interrupted and may be incomplete." };
  }
}
