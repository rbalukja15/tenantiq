# ADR-0016 — Rendering a streamed, cited answer

- **Status:** Accepted
- **Date:** 2026-08-13

## Context

ADR-0009 settled how an answer _leaves_ the server: `POST /api/query` streams SSE frames — token
deltas, then one terminal frame that is either `citations` or `error`. #19 is the other half: how a
browser turns that into something a reader can trust, using the primitives from #74 and the citation
resolution from #51.

Three things made this more than plumbing.

**The browser cannot use `EventSource`.** It is GET-only and cannot set headers, and the query is a
`POST` that needs the BFF's CSRF header. So the stream is consumed with `fetch` + `ReadableStream`,
which means owning the wire format in the client — including the fact that a chunk boundary can fall
anywhere: mid-frame, mid-line, or mid-UTF-8 sequence.

**Citations arrive last.** They are the terminal frame, so for the whole time an answer is being read
on screen, nothing is yet known to resolve. Whatever the UI does with a `[1]` during streaming, it is
doing it without evidence.

**A refusal and a thin answer looked identical on the wire.** The no-context path emitted a token
frame carrying the refusal sentence and a `citations` frame with an empty list — and an answer whose
`[n]` markers all failed to resolve emitted exactly the same shape. #74 built a dedicated refusal
state precisely so this case would not be improvised, and then it could not be detected.

## Decision

**A marker becomes a citation chip only once it resolves.** `segmentAnswer` renders a chip for a
number that appears in the resolved citation list and leaves every other `[n]` as literal prose. Two
consequences follow, and both are correct rather than tolerated: during streaming there are no chips
at all, and a number the model invented — which ADR-0008 already drops from the citations list —
stays visible as text instead of becoming a control that leads nowhere. A citation you can click that
resolves to nothing is precisely the failure this product claims not to have.

**The stream states whether it refused.** `CitationsEvent` gains a `refused` flag, serialised on
every citations frame. This is a small backend change inside a frontend issue, and the alternative
was worse: the only client-side way to tell the two cases apart is to match the refusal wording,
which couples the UI to backend copy and breaks _silently_ when that copy is edited. `GroundedAnswer`
— the non-streaming #15 path — has carried `refused` since it was written; the streaming path simply
dropped it, so this is restored parity rather than a new concept. The client defaults a missing flag
to `false`: fail safe toward showing real text, never toward blanking an answer that was produced.

**Evidence is fetched per citation, after the stream.** Each resolves through #51's
`GET /api/chunks/<id>` (ADR-0015), independently — one failure must not blank the others. A 404 is an
expected state, not an error: ADR-0015 deliberately allows deleting a document while its answer is
still on screen, so a citation can outlive its chunk, and the card says so.

**A failure that arrives mid-answer keeps the answer.** The client distinguishes _throwing_ from
_yielding an error_: nothing delivered yet → throw, because there is no answer to preserve; tokens
already delivered → yield, because the reader is looking at real model output. A partial answer is
shown with the failure stated beside it rather than replaced by a banner.

**An unparseable frame ends the stream with an error.** The tempting alternative — skip the bad frame
and carry on — would truncate an answer _invisibly_, leaving something that still looks complete.
That is the one failure mode a grounded-answers product cannot have.

**Only state changes are announced, never the answer text.** `aria-live` on prose that grows token
by token re-announces the whole answer from the beginning on every update, so a screen-reader user
hears it restart and never reaches the end. The answer is read on demand; a single always-mounted
status region announces terminal states ("Answer complete. 2 sources.", "No supporting passage
found."). It has to be always-mounted: a live region only reports mutations made _after_ it is
registered, which is why the `aria-live` #74 put on the refusal heading announced nothing — that
subtree is inserted already containing its text.

## Consequences

- The wire contract now carries a fact the backend always knew, so the refusal state is rendered from
  data rather than inferred from prose. Any future client gets it for free.
- Chips appearing only at the end is a visible behaviour, not a bug: markers are inert text while the
  answer streams, then become controls together. If that reads as a flicker in practice, the fix is
  presentational (reserve the space), not to chip unresolved numbers.
- `SourceCard.similarity` had to become optional. It was typed as required by #74 and **no API
  supplies it** — it exists only on the backend's internal retrieval `Source` and is never
  serialised, so the component threw a `TypeError` on any real citation. The alternatives were to
  widen the API or to render a placeholder `0`; a fabricated measurement in the evidence panel is the
  worst of the three, so an absent score now renders nothing at all.
- The design mockup's answer meta strip ("5 chunks retrieved · min similarity 0.61 · model · 1.9 s")
  is **not** built. Retrieved-but-uncited counts, the similarity floor and the serving model are
  nowhere on the wire, and inventing them would be exactly the fabrication this project exists to
  avoid. Surfacing them properly means widening the stream — a separate issue.
- The client owns an SSE parser, which is code that could have been a library. It is ~40 lines, and
  the alternative was a dependency in the one place where a subtle bug corrupts answer text.
