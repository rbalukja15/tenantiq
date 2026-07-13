# Changelog

All notable changes follow [Keep a Changelog](https://keepachangelog.com/) and
[Semantic Versioning](https://semver.org/). Nothing is released yet; entries are grouped by the
milestone that delivered them.

## [Unreleased]

### Added
- **Project foundation (M0).** Repository scaffold, documentation structure, the ADR process, a CI
  skeleton with status badge, and CLAUDE.md.
- **Two-layer tenant isolation (M1).** OIDC token verification with the tenant resolved **only**
  from a verified claim (never client input); a tenant-scoped ORM manager that *raises* when no
  tenant is in context; and forced Postgres row-level security as an independent backstop. Proven
  by an adversarial cross-tenant test suite (ADR-0002; #6–#9).
- **Document upload & storage (M2).** Tenant-scoped endpoint that persists an uploaded document and
  enqueues ingestion (#10).
- **Async ingestion pipeline (M2).** A Celery worker parses, chunks, and embeds uploaded documents
  into pgvector, advancing each document PENDING → PROCESSING → READY/FAILED (ADR-0003, ADR-0004;
  #11, #12).
- **Ingestion observability & retry (M2).** Per-document status and error surfacing with bounded
  autoretry for transient failures (ADR-0005; #13).
- **One-command local stack (M6).** `docker compose up` brings up Postgres(pgvector) + Redis +
  Ollama + backend + Celery worker + frontend; `.env` now takes effect, so a fresh clone runs on
  Postgres with RLS enforced rather than silently on SQLite (ADR-0006; #23).

### Changed
- **Retrieval recall (M3).** Tenant-filtered vector search no longer hits the shared-HNSW recall
  cliff that starved large tenants (#44).

### Fixed
- **Faithful chunk text (M3).** Chunks are stored as exact, offset-addressable slices of the
  source, so a citation resolves to a real span of the document (ADR-0003; #45).
- **Embedding integrity (M3).** Ingestion refuses to mark a document READY when the embedding count
  or dimension doesn't match its chunks, instead of persisting a partial or malformed set
  (ADR-0004; #46).
