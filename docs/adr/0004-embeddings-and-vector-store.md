# ADR-0004 — Embeddings & vector store

- **Status:** Accepted
- **Date:** 2026-06-30

## Context

Chunks (ADR-0003, #11) are only retrievable once they are vectors. Issue #12 must choose an
embedding source, store vectors in Postgres with an index that scales nearest-neighbour search, and
keep retrieval tenant-scoped like every other data path. Forces at play:

- **Anthropic has no embeddings API.** The project's LLM is Anthropic with an Ollama fallback
  (ADR-0001), but Anthropic offers no embeddings endpoint — embeddings need a different source.
- **Tests/CI must stay hermetic.** No network, no secrets, fast — the same constraint that made the
  token verifier injectable (#7).
- **Tenant isolation is sacred (ADR-0002).** Vectors are tenant data; vector search must not leak
  across tenants.
- **The embedding model may change.** Chunk sizing (ADR-0003) was deliberately model-agnostic, so
  the storage shape must not hard-bake one model forever without an escape hatch.

## Decision

**A pluggable embedder, pgvector storage behind an HNSW cosine index, embedding inside ingestion.**

- **Pluggable embedder** via `TENANTIQ_EMBEDDER_FACTORY` (a dotted path to a zero-arg callable),
  mirroring the injectable token verifier (#7):
  - **tests/CI:** `HashingEmbedder` — deterministic feature-hashing, standard-library only, 768-dim,
    L2-normalized. It carries no semantics beyond lexical overlap, which is enough to exercise
    storage, indexing, and nearest-neighbour *ordering* offline.
  - **dev/prod:** `OllamaEmbedder` — a local Ollama server's `/api/embed` (`nomic-embed-text`,
    768-dim) over stdlib `urllib`, so no new runtime dependency. A hosted provider (e.g. Voyage) can
    be added later as another factory without touching callers.
- **Fixed dimension (768, `TENANTIQ_EMBEDDING_DIM`).** A pgvector column + index need a fixed width.
  Changing models means a migration + re-backfill, not an in-place re-embed. `Chunk.embedding_model`
  records which model produced each vector, so a mixed or stale index is detectable.
- **Storage.** `Chunk.embedding` is a nullable `VectorField(768)`; a per-table **HNSW** index with
  `vector_cosine_ops`. The extension and index are Postgres-only and ride **vendor-guarded
  migrations** (0007 `VectorExtension` + the field; 0008 the HNSW index), exactly like the RLS
  migrations. SQLite tolerates the column type (lax typing), so the fast unit-test path still runs;
  the `<=>` search itself is Postgres-only.
- **Extension provisioning.** pgvector 0.8 is not a *trusted* extension, so the non-superuser app
  role (ADR-0002) cannot `CREATE EXTENSION`. A superuser provisions it in `template1` (compose init
  + the CI step), so every database — including the pytest test database, cloned from `template1` —
  inherits it, and the migration's `CREATE EXTENSION IF NOT EXISTS` becomes a privilege-free no-op.
- **Embedding inside ingestion.** `run_ingestion` embeds chunks after splitting and before `READY`,
  so READY means "chunked **and** embedded → searchable". A parse failure stays permanent
  (`FAILED`); an embedding failure (e.g. Ollama unreachable) is transient and propagates, so the
  Celery task retries with backoff rather than marking the document failed.
- **Retrieval.** `app.retrieval.nearest_chunks(query, k)` embeds the query and orders the
  tenant-scoped `Chunk` queryset by cosine distance. Scoping is automatic (Layer 1 manager + Layer 2
  RLS), so vector search inherits the isolation guarantee — proven by a cross-tenant retrieval test.
- **Backfill.** `manage.py backfill_embeddings [--tenant slug] [--batch-size N]` fills NULL
  embeddings tenant by tenant inside a `tenant_context`; idempotent.

### Rejected alternatives

- **Hosted embeddings (Voyage / OpenAI) as the default.** Best quality, but needs a paid key and
  can't run in CI without secrets. Kept as a future drop-in via the factory.
- **Local sentence-transformers in-process.** No service to run, but pulls in torch/transformers — a
  heavy install that slows CI, against the lean-dependency grain (ADR-0003).
- **IVFFlat index.** Needs training data and list tuning; HNSW gives strong recall with no training
  and is pgvector's modern default.
- **A separate embedding table (multi-model history).** YAGNI for v1 — one nullable column plus
  `embedding_model` suffices; a separate table can come if we ever serve several models at once.
- **Letting the app role create the extension.** Would require granting it elevated privilege,
  undermining the non-superuser/RLS posture; provisioning as superuser keeps the app role
  least-privileged.

## Consequences

- Retrieval quality in CI reflects lexical overlap only (hashing), not true semantics; semantic
  quality is verified manually with Ollama (`make dev`). The acceptance criterion (the relevant
  chunk ranks first) holds in both because the test corpus has clear lexical signal.
- Changing the embedding model is a migration + `backfill_embeddings`, never an in-place change.
- Provisioning pgvector is now an operational prerequisite (compose init + a CI step); a brand-new
  database needs the superuser step before the app's migrations run.
- Vendor-guarded, Postgres-only migrations are now a settled pattern — used three times (0003 and
  0006 for RLS, 0008 for the vector index).
- Implemented by #12; consumed by the RAG query engine (M3) and the evaluation harness (M5).

## Addendum (2026-07-05, #44): iterative index scans for tenant-filtered search

**Context.** The shared, all-tenant HNSW index interacts badly with tenant filtering. Postgres runs
the tenant predicate (scoped manager + RLS) as a *post-filter* over the index's bounded candidate
list (`hnsw.ef_search`, default 40). Reproduced on `pgvector/pgvector:pg16` (0.8.3): once a tenant
is large enough (~25k+ chunks) that the planner prefers the HNSW path over the `tenant_id` btree,
and another tenant's corpus dominates the query's neighbourhood, every candidate is filtered out and
`nearest_chunks` returns **fewer than `k` — possibly zero — rows**, even though the tenant has
relevant chunks. It is a recall bug, not a leak: RLS still holds; results are silently *missing*.
Small tenants are unaffected (the planner uses the btree + exact sort), which is why the original
`test_retrieval.py` fixtures never surfaced it.

**Decision.** `nearest_chunks` sets `SET LOCAL hnsw.iterative_scan = relaxed_order` (pgvector 0.8+),
so the HNSW scan re-scans with a growing candidate list until `k` rows survive the tenant filter.
`relaxed_order` is chosen over `strict_order` because it recalls more within the scan budget —
empirically `strict_order` stopped early and still returned fewer than `k` on the regression case —
and exact "nearest first" ordering is cheaply restored by re-ranking the `k` survivors in Python.
`SET LOCAL` scopes the setting to the surrounding transaction (`tenant_context` opens one on
Postgres), so it can't leak across a pooled connection; off Postgres it's a no-op.

**Rejected / deferred.**
- **`strict_order`** — preserves index order but under-recalls here; ordering is handled in Python
  instead, so recall is the only axis left and `relaxed_order` wins it.
- **Per-tenant partial indexes / table partitioning by tenant** — the real scale fix (each tenant's
  search hits an index containing only its rows, so post-filtering can't starve it), but a larger
  change than #44 warrants. It becomes worthwhile when single-tenant corpora or tenant count grow;
  tracked as the scale-up path here.
- **Raising `ef_search` globally** — only pushes the cliff out; a dominating tenant with more than
  `ef_search` near neighbours still starves the others, and it slows every query.

**Consequences.** Retrieval recall is correct regardless of relative tenant sizes, proven by a
regression test that forces the HNSW path at fixture scale (`enable_seqscan`/`sort` off, small
`ef_search`) and asserts a starved tenant still gets `k`. The retrieval path now depends on pgvector
**0.8+** (the compose image and CI are 0.8.3). Implemented by #44.

## Addendum (2026-07-06, #46): validating the embedding backend's response

**Context.** "READY means chunked **and** embedded" is only true if there is exactly one
correctly-sized vector per chunk. But `run_ingestion` did `zip(pieces, vectors)` with no length
check, and `OllamaEmbedder.embed_documents` returned the HTTP response's `embeddings` verbatim. A
backend returning fewer vectors than inputs (contract drift, a truncated response, a future
provider) silently **dropped the tail chunks and still marked the document READY** — data loss
against the invariant. A wrong-dimension vector (an operator pointing at, say, a 1024-dim model
while `TENANTIQ_EMBEDDING_DIM=768`) slipped past into a cryptic pgvector write, and — being a
*permanent* config error — burned all three retry backoffs before failing unhelpfully.

**Decision.** Validate the backend's response at `embed_in_batches` — the single choke point both
`run_ingestion` and the `backfill_embeddings` command route through — before any caller `zip`s
vectors onto chunks. The count must equal the input count (`EmbeddingCountError`) and every vector
must be `embedder.dim` wide (`EmbeddingDimensionError`); both messages name the actual numbers and
the model. The two failures are classified differently on purpose:

- **Count mismatch → transient.** It can be a truncated / interrupted response, so `run_ingestion`
  lets it propagate and the Celery task retries; if it persists, the retries exhaust into an
  observable FAILED document carrying the clear reason (via `on_failure`). Never READY.
- **Dimension mismatch → permanent.** A model whose width doesn't match the column/index is a static
  mis-configuration that cannot self-heal, so `run_ingestion` records FAILED immediately (like a
  `ParseError`) rather than burning retry backoff on an error every attempt would hit.

`zip(..., strict=True)` at both write sites is a cheap belt-and-suspenders invariant backing the
boundary check.

**Rejected / deferred.**
- **Validating only with `zip(strict=True)`** — catches the under-count at the write site but raises
  an opaque `ValueError` ("zip() argument 2 is shorter…"), not a message naming the real problem,
  and does nothing for a wrong *dimension* or for the backfill path. Kept it as the secondary guard,
  not the primary one.
- **Validating inside each `Embedder`** — the count guard must live on the shared path so a generic
  stub / any provider is covered; a per-embedder check wouldn't catch a mis-implemented one and would
  duplicate the logic.

**Consequences.** A count or dimension mismatch can never produce a READY document with
missing/mismatched chunks; the failure reason names the actual problem, and a permanent config error
fails fast instead of after three backoffs. Implemented by #46.
