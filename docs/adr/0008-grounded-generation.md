# ADR-0008 — Grounded generation & citation enforcement

- **Status:** Accepted
- **Date:** 2026-07-17

## Context

#14 assembles a grounded prompt: numbered sources plus a system prompt that fixes the grounding
contract (ADR-0007). #15 is where the model actually answers — and where the product's central
promise is enforced: **the LLM never invents a citation, and every citation resolves to a real,
retrieved chunk** (CLAUDE.md). Three constraints, flagged in the issue's post-M2 review, shape the
decision:

- **Structured citations vs. free text.** An answer is only trustworthy if each `[n]` maps to a
  specific chunk. Parsing citations out of prose is brittle; the model must return them as data.
- **Chunk identity is unstable across re-ingestion.** The #13 retry path *deletes and recreates*
  chunks, so a chunk's primary key changes. A citation that persists a chunk PK dangles after a
  re-ingestion. This ADR must decide the citation's durable anchor.
- **The streaming tension is not ours.** Schema-enforced structured output arrives at the *end* of
  generation, while the M4 UI (#19) wants token-by-token streaming. That transport decision lives in
  #48; #15 owns the citation schema itself, and #51 owns the resolution endpoint the UI will call.

## Decision

**Generate a structured `{answer, citations}` result, resolve cited source *numbers* to real chunks,
anchor each citation to a re-ingestion-stable reference, and refuse without a model call when there
is no context.**

- **The model cites numbers, we resolve them.** Generation requires a structured result — an
  `answer` string plus `citations`, a list of the source **numbers** (`[1]`, `[2]` …) the answer
  relies on. `generate_answer` maps each number back to the `Source` it was assigned in #14 and
  **drops any number that doesn't match one**. The model can't emit a chunk ID it was never shown,
  and a hallucinated `[99]` simply resolves to nothing — so a surfaced citation always points at a
  retrieved chunk. Duplicates collapse to first-seen order.
- **Schema-enforced output, per backend.** Anthropic uses `output_config.format` (a JSON schema) so
  the reply is valid JSON; the Ollama fallback uses its `format` schema. A shared, tolerant
  `_coerce_result` validates the untrusted payload (missing answer → empty; non-integer citation
  entries dropped) rather than trusting the model's JSON blindly.
- **Citations anchor to a stable reference, not the PK.** A `Citation` carries
  `(document_id, chunk_index, start_offset, end_offset)` — the durable anchor a resolver (#51) can
  re-locate the span by after a re-ingestion (offsets are faithful since #45) — **alongside**
  `chunk_id` as the current snapshot. We deliberately do **not** make re-ingestion preserve chunk
  PKs: that would constrain the ingestion path for the benefit of citations, whereas the anchor
  makes citations robust without touching ingestion.
- **No context → refuse, spend nothing.** If retrieval returned no sources (`has_context` is false),
  `generate_answer` returns a canned refusal and never calls the LLM. "Insufficient context" is a
  first-class outcome, not a degenerate answer.
- **The LLM is pluggable** (like the embedder, ADR-0004): a deterministic `FakeLLM` under pytest
  (no key, no network) keeps the suite hermetic; otherwise `AnthropicLLM` (the answer model,
  `claude-opus-4-8`) with an `OllamaLLM` fallback when no API key is set. `TENANTIQ_LLM_FACTORY`
  selects the backend, read fresh so tests can override it.

### Rejected alternatives

- **Persist the chunk PK as the citation key.** Simplest, but a re-ingestion (#13) silently breaks
  every stored citation. The stable anchor is a few extra fields for durability we'll need in #51.
- **Have the model return chunk IDs directly.** It invites hallucinated IDs and leaks internal PKs
  into the prompt; citing the small, prompt-local source numbers is safer and cheaper, and keeps the
  mapping ours to validate.
- **Parse citations from the prose `[n]` markers only.** The system prompt already asks for `[n]`
  markers (they read well for humans), but relying on a regex over streamed prose as the *enforced*
  contract is brittle. Structured output is the contract; #48 may still surface the in-prose markers
  for rendering.
- **A generation framework (LangChain output parser).** Same lean stance as ADR-0003/0007 — the
  resolution + validation is a few readable lines, and a framework hides the grounding contract.

## Consequences

- **#48** wraps this in the `POST /api/query` endpoint: it retrieves inside the tenant transaction,
  streams the answer, and closes with these `Citation`s as the terminal citations event — resolving
  the structured-output-vs-streaming tension in its own ADR. Because `generate_answer` makes no DB
  query (it operates on the already-retrieved `AssembledContext`), the tenant scoping from #14 is
  inherited and nothing holds a DB transaction open during the model call.
- **#51** resolves a `Citation` to a document span via the stable anchor, so a cited answer stays
  verifiable even after the document is re-ingested.
- CI stays hermetic and free: the fake backend runs under pytest, so no key or network is needed;
  the Anthropic/Ollama adapters are the untested edge, with their shared parse step (`_coerce_result`)
  covered directly.
- Whether generation is reached at all is governed by the retrieval similarity floor (ADR-0007):
  below it, #14 returns no context and #15 refuses.
- Implemented by #15; the streaming endpoint (#48) and citation-resolution endpoint (#51) build on it.
