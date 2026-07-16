# ADR-0007 — Grounded prompt assembly & retrieval threshold

- **Status:** Accepted
- **Date:** 2026-07-16

## Context

The RAG query engine (M3) turns a question into a cited answer. Retrieval itself shipped in #12/#44
(`app/retrieval.py::nearest_chunks` — tenant-scoped nearest-neighbour over pgvector). The engine
then spans three issues: **#14** assembles a grounded prompt from the retrieved chunks, **#15** calls
the LLM and enforces a citation schema, and **#48** exposes the HTTP endpoint and streams the answer.
This ADR fixes the **assembly seam** that #15 and #48 both build on, before either is written.

Forces:

- **The grounding invariant is the product** (CLAUDE.md): the LLM never computes numbers and never
  invents citations; answers are grounded only in retrieved tenant-scoped chunks, and every citation
  must resolve to a real chunk. Assembly is where that contract is first expressed.
- **Citations must be verifiable, not decorative.** A cited answer is only trustworthy if `[n]` maps
  back to a specific chunk (and, via #45, a character span of the original document).
- **Off-topic questions must not be padded.** Feeding the top-k chunks into the prompt regardless of
  relevance invites a confident answer from irrelevant context — the opposite of grounding.
- **Clean seam for generation + streaming.** #15 needs the prompt and the source→chunk mapping; #48
  needs to retrieve *inside* the tenant transaction but generate *outside* it (its own ADR). So this
  step must make no call to the answer-generating LLM — #15/#48 own that call and its transaction
  boundary — while retrieval (which embeds the query) and prompt assembly happen here, synchronously.

## Decision

**Assembly is a pure function that returns a grounded prompt plus a numbered, resolvable source set;
retrieval applies a configurable similarity floor and refuses rather than pads.**

- **No generation call in the seam.** `app/rag.py::retrieve_context(question, *, k, min_similarity)`
  returns an `AssembledContext` — `system_prompt`, `user_prompt`, and a tuple of `Source` objects —
  and **never calls the answer-generating LLM**. It does retrieve: a tenant-scoped pgvector search
  that first embeds the query (a network call to the embedder in production). `build_grounded_prompt(
  question, sources)` is split out as a *fully pure* function — no DB, no network — so the prompt
  format is unit-testable without a database. This is the seam #15 (generation) and #48
  (endpoint/streaming) call.
- **Sources are numbered and carry real identifiers.** Each `Source` has a 1-based citation
  `number`, the `chunk_id`, its `document`, and the `start_offset`/`end_offset` span from #45. The
  prompt lists sources as `[n] <title>\n<text>`; the answer cites `[n]`; #15/#48 resolve `[n]` →
  `chunk_id` → a verifiable span. Citations are structured data, never free text the model invents.
- **The system prompt encodes the contract:** answer only from the numbered sources, never use
  outside knowledge, never invent facts/figures/citations, cite every claim by number, and say
  "I don't have enough information in the provided documents" when the sources don't answer.
- **Similarity floor, not padding.** Retrieval keeps a candidate only if its cosine similarity
  (`1 - distance`) clears `min_similarity`. If nothing clears the bar, `has_context` is false and
  the prompt instructs a refusal — the query returns "no relevant context" instead of grounding an
  answer in irrelevant chunks. `k` and `min_similarity` are settings
  (`TENANTIQ_RETRIEVAL_TOP_K`, `TENANTIQ_RETRIEVAL_MIN_SIMILARITY`), tuned like the chunking knobs.
- **Default floor is conservative (0.0)** — keep anything at least orthogonal to the query — because
  the right threshold depends on the embedding model's similarity distribution, which the M5 eval
  harness will calibrate. A too-aggressive default would silently refuse answerable questions.

### Rejected alternatives

- **A retrieval/prompt framework (LangChain / LlamaIndex).** A `Retriever` + `PromptTemplate` would
  work, but the same lean, hand-rolled stance as ADR-0003 applies: the logic is a few dozen readable
  lines, and a framework hides the grounding contract behind indirection we'd have to audit anyway.
- **Always answer from the top-k, no threshold.** Simplest, but it grounds answers in whatever came
  back — the classic RAG failure where an off-topic question gets a confident, wrong answer. The
  floor + explicit "no relevant context" path is the honest behaviour.
- **Embed citation markers into the chunk text.** Baking `[n]` into the stored text couples storage
  to presentation and can't carry offsets. Structured `Source` objects keep the mapping explicit and
  resolvable.

## Consequences

- #15 calls `retrieve_context`, enforces its structured answer-with-citations schema against the
  numbered `Source`s, and short-circuits to a refusal when `has_context` is false.
- #48 retrieves inside the tenant transaction — including the query-embedding call — then streams
  the answer *outside* it and closes with the `Source`s as the terminal citations payload. Retrieval
  and assembly are synchronous and make no generation call, so nothing holds the tenant transaction
  open during the long streaming LLM call (#48 owns that boundary decision in its own ADR).
- The `min_similarity` default is a floor to be calibrated: M5's eval suite tunes it against the real
  embedding model, and until then off-topic refusal depends mostly on retrieval ordering.
- Resolving each source's document title is an N+1 over the retrieved chunks; negligible at
  retrieval `k` (single digits) and not worth pre-fetching yet.
- Implemented by #14; retrieval was proven tenant-scoped in #12, and `test_rag.py` adds a
  cross-tenant proof for the assembly path (a tenant's question can never be grounded in another
  tenant's chunks).
