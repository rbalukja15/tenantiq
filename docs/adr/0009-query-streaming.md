# ADR-0009 — Query API: streaming transport & transaction boundary

- **Status:** Accepted
- **Date:** 2026-07-20

## Context

#14 assembles a grounded prompt and #15 generates a cited answer. #48 exposes them over HTTP as
`POST /api/query`. Three real constraints make the transport a decision, not plumbing:

- **The M4 UI wants token-by-token streaming** (#19), but #15's citation enforcement is a *structured*
  `{answer, citations}` result that only exists once generation finishes. Streaming from the first
  token and getting end-of-generation structured citations pull in opposite directions.
- **`ATOMIC_REQUESTS` wraps the whole request in a transaction.** A streaming LLM call *inside* it
  would pin a database connection — and the RLS `SET LOCAL` GUC — open for the entire duration of the
  stream. Retrieval must run inside the tenant transaction; generation must run outside it.
- **The browser transport is constrained.** Native `EventSource` can't send an `Authorization`
  header, and every request here is Bearer-authenticated. So the client must consume the stream with
  `fetch` + `ReadableStream`, parsing SSE frames itself.

## Decision

**Retrieve inside the request transaction, stream the answer outside it as SSE frames, and resolve
citations from the answer's `[n]` markers at stream end.**

- **Transaction boundary — retrieve eager, generate lazy.** `QueryView.post` calls
  `retrieve_context` *in the view body*, so retrieval runs inside the request's tenant transaction and
  is tenant-scoped (ADR-0002). It then returns a `StreamingHttpResponse`. Django produces a streaming
  body *after* the view returns and `ATOMIC_REQUESTS` has committed, so **no DB connection is held
  open while the model streams**. Generation issues no query at all — it operates on the already
  materialized `AssembledContext` — which a test pins by asserting **zero queries** run while the body
  streams.
- **SSE frames over `StreamingHttpResponse`.** `Content-Type: text/event-stream`, `Cache-Control:
  no-cache`, `X-Accel-Buffering: no` (so a proxy doesn't buffer the stream). Each event is a frame:
  `event: <type>\ndata: <json>\n\n`.
- **Event shape:** `token` (an incremental text delta), a terminal `citations` (the resolved
  citations, each carrying `chunk_id` + the re-ingestion-stable anchor from ADR-0008), and `error`
  (a mid-stream generation failure). The no-context refusal uses the *same* shape — it streams the
  refusal as `token`(s) then an empty `citations` event — so the client has one code path.
- **Citations from the prose markers (the tension resolved).** The streaming path streams the model's
  *prose*, which already carries `[n]` markers because ADR-0007's system prompt instructs the model to
  cite that way. When the stream ends, we parse the `[n]` markers out of the accumulated text and run
  them through **the same #15 resolver** — mapping each number to its `Source` and dropping any that
  don't match. So a `[99]` still resolves to nothing: **a citation can't be invented**, exactly as in
  the non-streaming path, while the answer streams from the first token. #15's structured,
  non-streaming `generate_answer` stays for callers that want the whole result at once (e.g. the M5
  eval harness).
- **A mid-stream failure ends with an `error` event**, not a citations event — already-streamed
  tokens remain, and the client can show the partial answer and offer a retry.

### Rejected alternatives

- **Native `EventSource`.** Can't attach the `Authorization` header, so it can't authenticate. The
  `fetch` + `ReadableStream` client is the price of Bearer auth.
- **Two model calls — stream prose, then a second structured call for citations.** Doubles latency and
  cost for a mapping the in-prose `[n]` markers already encode; the single streamed call plus a parse
  is simpler and cheaper.
- **Hold the transaction open across the stream.** Pins a pooled connection and the RLS GUC for the
  model's (seconds-long) duration and directly violates the acceptance criterion. Retrieve-eager /
  generate-lazy avoids it with no special connection handling.
- **WebSocket.** Bidirectional and heavier to operate; a query is one-way (server → client) streamed
  text, which SSE models exactly.
- **Buffer the whole answer, return it non-streaming.** Defeats the token-by-token UX #19 needs; #15's
  `generate_answer` already covers the non-streaming case.

## Consequences

- The M4 client (#19) consumes `POST /api/query` with `fetch` + `ReadableStream`, parses the SSE
  frames, renders `token` deltas live, and renders the terminal `citations`. #51 resolves each
  citation's stable anchor to a document span for display.
- Because retrieval is eager and generation is DB-free, the "transaction not held open during
  generation" guarantee holds by construction — no streaming-aware connection management needed.
- The in-prose `[n]` markers are the citation source of truth for the *streaming* path; the
  *structured* output is the source of truth for #15's non-streaming path. Both flow through the same
  resolver, so both enforce "no invented citation."
- DRF runs request-side content negotiation *before* the view, and its default renderer set is
  JSON-only — so an SSE client's `Accept: text/event-stream` would 406 before the stream ever starts.
  The view therefore uses a content-negotiation class that accepts any `Accept` header; the streamed
  200 body owns its own SSE framing (a `StreamingHttpResponse` bypasses response rendering), and the
  default JSON renderer is used only for the view's error responses.
- Implemented by #48; builds on #14 (retrieval), #15 (generation + resolver, ADR-0008), and ADR-0007's
  citing prompt.
