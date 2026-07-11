# ADR-0003 — Document chunking strategy

- **Status:** Accepted
- **Date:** 2026-06-27

## Context

Retrieval quality in a RAG system is bounded by how documents are split: chunks that are too large
dilute relevance and blow the embedding/context budget; chunks that are too small lose the context
needed to answer. We must fix the strategy before building the pipeline (issue #11), because the
embeddings (#12) and the retriever (M3) are built on whatever shape we choose, and re-chunking later
means re-ingesting every document.

Forces at play:

- **Quality first.** Chunk boundaries should fall on natural semantic breaks, not arbitrary byte
  offsets, and adjacent chunks should overlap so an answer that straddles a boundary isn't lost.
- **Dependency-light.** This is a one-person portfolio codebase; a heavyweight framework
  (LangChain, unstructured.io) is more surface area, licensing, and CI weight than one task needs.
- **Tenant isolation is sacred (ADR-0002).** Chunks are tenant data, so they live in a
  tenant-owned, RLS-protected table like everything else.
- **The embedding model isn't chosen yet (#12).** Exact token budgets depend on it, so sizing must
  not hard-depend on a specific tokenizer today.

## Decision

**Recursive, structure-aware splitting with overlap, hand-rolled, sized by a token estimate.**

- **Extraction.** `pypdf` (BSD-licensed) for PDFs; native decode for `text/plain` / `text/markdown`.
- **Splitting.** Try the largest natural boundary that fits and recurse into smaller ones only when
  a piece is still too big: **paragraph (`\n\n`) → line → sentence → word → hard character cut.**
  Pieces are then packed greedily into chunks.
- **Size & overlap.** Target **~800 tokens per chunk** with **~100 tokens (~12%) of overlap** carried
  from the end of one chunk into the start of the next. 800 tokens fits comfortably inside every
  mainstream embedding context window while staying granular enough for precise retrieval.
- **Token sizing.** A dependency-free **~4-characters-per-token estimate** for now. When the
  embedding model is chosen (#12), this can be swapped for that model's real tokenizer without
  touching the splitting algorithm — the estimate only feeds the size targets.
- **Storage.** Each chunk is a row in a tenant-owned `Chunk` table (FK to `Document`, ordered by
  `index`), so it inherits both isolation layers (ORM scoping + Postgres RLS).
- **Async + failure handling.** A Celery task runs the pipeline off the request path and — having no
  request — sets the tenant explicitly (ADR-0002). Unreadable/empty files are a permanent failure
  (document → `FAILED`); transient errors retry with backoff.

### Rejected alternatives

- **Fixed-size character chunking.** Simplest, but cuts mid-sentence and mid-word, hurting
  retrieval — the recursive boundaries cost little and read far better.
- **LangChain / unstructured.io.** Less code to write, but a large dependency tree (and, for
  `unstructured`, slow installs) for what is a small, well-understood algorithm. Chosen against to
  keep the codebase lean and the dependencies legible.
- **Semantic / embedding-based chunking.** Higher quality in theory, but needs embeddings to chunk
  (circular with #12) and is overkill for v1.

## Consequences

- Chunking lives in two small, independently testable modules (`app/parsing.py`, `app/chunking.py`)
  plus an orchestration function (`app/ingestion.py`); the splitter is pure and unit-tested.
- Changing the strategy (size, overlap, separators) requires **re-ingesting** existing documents to
  re-chunk them; there is no in-place migration of chunk boundaries.
- The token estimate is approximate, so chunks may run slightly over/under the true token count;
  #12 tightens this when the embedding model's limits are known.
- New tenant-owned tables (like `Chunk`) each need their own RLS migration (see
  `0006_chunk_rls.py`), mirroring `0003`.

Implemented by: #11 (parsing + chunking + Celery); consumed by #12 (embeddings) and M3 (retrieval).

## Addendum (2026-07-11, #45): verbatim, offset-addressable chunks

**Context.** The first implementation split with `text.split(sep)` (which *discards* the separator)
and re-joined pieces with a single space, so for any document past the ~3200-char target the stored
`Chunk.text` was **not a substring of the source** — sentence periods and all paragraph/line
structure were destroyed (a realistic document produced 3 chunks, 0 of them substrings). That breaks
verbatim citation quoting (#15), makes character-offset citations impossible, and corrupts
faithfulness evaluation (M5).

**Decision.** Rewrite the splitter to work in **offsets**: a forward scan chooses a cut point with
the same boundary preference (paragraph → line → sentence → word → hard cut, via `rfind` within the
target window) and emits `(start_offset, end_offset)` spans, so each chunk is exactly
`source[start_offset:end_offset]` — separators stay attached and nothing is mutated. Overlap is
expressed as consecutive spans that *share* a range (the next span starts `overlap` chars before the
previous cut, snapped to a word boundary), so every chunk stays individually verbatim while still
carrying boundary context. Offsets are relative to the extracted source text passed into
`chunk_text`, giving citations a stable anchor. `Chunk` gains `start_offset` / `end_offset` columns
(migration `0010`), populated during ingestion.

**Consequences.**
- Every stored chunk is an exact, offset-addressable slice of the extracted source, proven by a
  substring-fidelity test (`chunk["text"] == source[start:end]` for every chunk).
- **Chunks created before #45 carry mutated text and default `(0, 0)` offsets.** They are refreshed
  by a **re-ingestion** — the `POST /api/documents/<id>/retry` endpoint (#13) re-runs the pipeline
  and rewrites the chunk set; `backfill_embeddings` only fills embeddings and does **not** refresh
  text/offsets. There is no in-place migration of chunk boundaries (consistent with the re-ingestion
  note above).
- Retrieval and embeddings are unaffected (same chunk sizes and ordering); only the text is now
  faithful and offset-addressable. Implemented by #45; consumed by #15 (citations) and M5 (eval).
