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
