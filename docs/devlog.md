# Dev log

Short, dated notes per milestone: what shipped, what was hard, what I'd change.

## 2026-06-26 — M0: foundation
Scaffolded the repo: README with architecture diagram, docs/ + ADR-0001 (stack & scope),
CI skeleton (lint + test), Makefile, CLAUDE.md for Claude Code, and the full issue/milestone
backlog. Next: M1 — auth and the tenant-isolation guarantee, starting with ADR-0002.

## 2026-06-26 — M1 #7: per-tenant OIDC auth
Turned `backend/` into a Django project and made the API an OAuth2 resource server: it validates
Bearer JWTs against each tenant's Keycloak realm (JWKS), routing by the verified `iss`. Decisions:
a custom `User` keyed on `(oidc_issuer, oidc_sub)` — a `sub` is only unique within an issuer;
strict RS256 with required `exp/iss/aud/sub` + 60s leeway; and a post-verify
`iss == tenant.oidc_issuer` check so a token from one realm can't be replayed as another tenant.
Hardest part: keeping auth tests hermetic (no live Keycloak in CI) while still exercising the real
routing — solved by making the verifier's key-resolver injectable and signing test tokens with a
local key. The DB-level RLS backstop lands next in #8.

## 2026-06-27 — M1 #8: tenant-scoped ORM + Postgres RLS
Implemented ADR-0002's two enforcement layers. Layer 1: a `TenantOwnedModel` base + a
`TenantScopedManager` that filters every query by a request-scoped contextvar and *raises* when no
tenant is set (a forgotten scope is a loud error, not a silent all-tenant read). Layer 2: forced
Postgres row-level security on every tenant-owned table, reading an `app.current_tenant` GUC the app
sets with `SET LOCAL` per request. Two things were subtle. First, DRF resolves the tenant *inside*
the view (after Django middleware), so the tenant is activated at the auth seam while a thin
middleware only bounds cleanup — not the "middleware does everything" the ADR sketched. Second, RLS
is bypassed by superusers, so it does nothing unless the app connects as a non-superuser role:
added a `tenantiq_app` (`NOSUPERUSER NOBYPASSRLS`) that owns the schema, in compose + CI. Testing on
real Postgres caught a real bug — once the GUC has been set on a pooled connection it reads back as
`''` (not NULL), and `''::uuid` raised instead of matching nothing; fixed with `NULLIF(…, '')`.
Cross-layer adversarial proofs (isolation holds with the ORM filter deleted) come next in #9.

## 2026-06-27 — M1 #9: cross-tenant isolation proof — M1 complete
Added `tests/test_tenant_isolation.py`: an adversarial suite that seeds two tenants and asserts A can
never reach B — at the API edge (both directions, and with a forged `?tenant_id` that's correctly
ignored because the tenant comes from the verified `iss`), through the ORM (can't even fetch B's row
by id), and — the headline — with the application filter deliberately bypassed, where Postgres RLS
alone still hides B's rows from the unscoped manager and from raw SQL. To prove the suite isn't
vacuously green I removed the manager's filter and watched four tests go red, then restored it. This
closes M1: every tenant data path is scoped, and the guarantee is now enforced in two layers *and*
proven in CI. Next: M2 — document ingestion (upload → chunk → embed in pgvector), where Celery tasks
will set the tenant explicitly since they have no request.

## 2026-06-27 — M2 #10: document upload + storage
Opened M2 by turning `Document` from #8's placeholder into a real uploaded file: a multipart
`POST /api/documents` validates type (PDF/text/Markdown) + size, stores the raw bytes, and persists
a `PENDING` row. Storage is the local filesystem behind Django's `FileField` (so M6 can swap to S3
by config) under a per-tenant, non-guessable path `tenants/<tenant_id>/documents/<uuid>/…` — files
are isolated on disk, not just in the DB. The endpoint became a DRF `ListCreateAPIView` +
`DocumentSerializer`; because `Document.objects` is already tenant-scoped, the upload is bound to the
caller's tenant and the list can only return their rows — so the isolation proof extends to the new
write path for free (a cross-tenant upload test confirms B never sees A's file). Files are stored but
never served publicly; a scoped download endpoint can come later. The row waits at `PENDING` for
#11's parsing/chunking pipeline.

## 2026-06-27 — M2 #11: parsing & chunking pipeline (Celery) + ADR-0003
Wrote **ADR-0003** (recursive, structure-aware chunking; ~800-token chunks with ~100 overlap;
hand-rolled splitter + `pypdf`, no LangChain) then built it: `app/parsing.py` (extract text, turning
any bad/attacker-supplied file into a `ParseError`), `app/chunking.py` (a pure recursive splitter
that prefers paragraph→sentence→word→hard-cut boundaries and carries overlap forward), and
`app/ingestion.py` tying them together. A Celery task runs it off the request path; per ADR-0002 it
takes the tenant id explicitly (no request) and writes tenant-owned `Chunk` rows (new `app_chunk`
RLS migration, mirroring `0003`). The upload view enqueues the task in `transaction.on_commit` so the
worker can't race the request transaction. Two gotchas worth noting: bulk_create skips `save()`, so
the tenant is set explicitly on each chunk to satisfy the RLS `WITH CHECK`; and getting Celery to run
inline in tests took making `task_always_eager` default on under pytest (mutating `conf` after the
app reads it from Django settings doesn't stick) plus `ignore_result=True` so no result backend is
touched. Logic is split from plumbing — `run_ingestion` is a plain, synchronously-tested function;
the task is a thin wrapper with retry/backoff. Next: #12 — embeddings into pgvector.

## 2026-06-30 — M2 #12: embeddings + pgvector storage + ADR-0004
Wrote **ADR-0004** then built it: chunks now become vectors and are searchable. The embedder is
pluggable behind `TENANTIQ_EMBEDDER_FACTORY` (same trick as the token verifier) — a deterministic,
stdlib-only `HashingEmbedder` under pytest so CI stays offline and hermetic, and an `OllamaEmbedder`
(`nomic-embed-text`, 768-dim, over `urllib` — no new dependency) for `make dev`. Anthropic has no
embeddings API, so the project's Ollama fallback is the real source here. `run_ingestion` now embeds
chunks before marking a document READY (a parse failure stays permanent → `FAILED`; an embedding
failure is transient → it propagates so Celery retries). Vectors live in a nullable `Chunk.embedding`
`VectorField(768)` behind a Postgres-only **HNSW** cosine index; `app.retrieval.nearest_chunks`
orders the tenant-scoped queryset by cosine distance, so vector search inherits the isolation
guarantee — a cross-tenant retrieval test proves B never sees A's chunks. A `backfill_embeddings`
command fills NULL embeddings tenant by tenant, idempotently.

The sharp edge was provisioning. pgvector 0.8 isn't a *trusted* extension, so the non-superuser
`tenantiq_app` role (the very role that makes RLS bite) can't `CREATE EXTENSION`. On a throwaway
pgvector container I watched the migration fail as the app role, then fixed it by provisioning the
extension as a superuser in `template1` (compose init + CI) — so every database, including the pytest
test DB cloned from `template1`, inherits it and the migration's `CREATE EXTENSION IF NOT EXISTS`
no-ops. SQLite tolerates the `vector` column (lax typing), so the fast unit path still runs; the HNSW
index and `<=>` search are Postgres-only, on the same vendor-guarded-migration pattern as RLS (0003,
0006, now 0008). Next: #13 — ingestion observability (status surfacing + retry/metrics).

## 2026-07-02 — M2 #13: ingestion observability + retry + ADR-0005 — M2 complete
Wrote **ADR-0005** then closed the async pipeline's biggest blind spot: a *transient* failure that
exhausted its retries used to leave a document wedged in `PROCESSING` forever, with no record of why.
Now the Celery task carries an `IngestTask.on_failure` hook that fires only when retries are spent
and records `FAILED` + the reason via `mark_ingestion_failed`; permanent `ParseError`s are still
recorded immediately (no retry). Three fields make state observable — `error` (the surfaced reason,
capped), `attempts` (bumped per try), and `updated_at` (so a stuck doc is findable by age) — all
read-only over the API. A tenant-scoped `POST /api/documents/<id>/retry` re-ingests a FAILED
document: the lookup goes through the scoped manager, so another tenant's id is a 404, not a
cross-tenant action (a test proves B can't retry or observe A's doc); a non-FAILED doc is a 409.

The sharp edge was transactions. `tenant_context` opens `transaction.atomic()` on Postgres (to scope
the RLS `SET LOCAL`), so my first cut — record the attempt and do the work in one block — quietly
rolled the `attempts` increment back with every transient failure. Real-Postgres tests caught it
(the doc read back `pending`, not `processing`). The fix splits `run_ingestion` into two phases: a
tiny first transaction commits "we are attempting this" (PROCESSING + `attempts++`), then a second
does the risky parse/embed/persist atomically — so a transient failure rolls back only the work and
`attempts` stays honest. Verified the whole suite on a throwaway pgvector container as the
non-superuser `tenantiq_app` role (RLS live), not just SQLite. That closes M2: upload → parse/chunk
→ embed → observe/retry. Next: M3 — the RAG query engine.

## 2026-07-05 — M3 #44: retrieval recall cliff (HNSW + tenant filter)
A whole-project review (a Fable 5 multi-agent pass, kicked off after M2) empirically found a recall
bug hiding under the vector search before M3 could build on it. The single, shared HNSW index spans
every tenant's rows; Postgres applies the tenant filter (scoped manager + RLS) as a *post-filter*
over the index's bounded `ef_search` candidate list. So once a tenant is large enough that the
planner prefers the HNSW path over the `tenant_id` btree, and another tenant's corpus owns the
query's neighbourhood, `nearest_chunks` returns fewer than `k` — reproduced returning **zero** rows
for a tenant holding tens of thousands of chunks. Not a leak (RLS held throughout); results were
silently *missing*, which is the worst kind of retrieval bug — the answer engine would just say
"not found". The original fixtures (1–8 chunks) never saw it because at that scale the planner uses
an exact btree sort, not the index.

The fix is one line of intent: `SET LOCAL hnsw.iterative_scan = relaxed_order` on the retrieval
path (pgvector 0.8+), so the scan keeps widening its candidate list until `k` rows survive the
tenant filter. Two things I only got right by testing on real Postgres: `strict_order` *under*-recalls
(it stopped at 4 of 5 on the regression case) so `relaxed_order` is the correct choice, with exact
"nearest first" restored by re-ranking the `k` survivors in Python; and the regression test has to
*force* the HNSW path at fixture scale (`enable_seqscan`/`sort` off, a small `ef_search`) because the
real cliff only appears at ~25k+ rows — impractical to seed in CI. Recorded as an ADR-0004 addendum,
with per-tenant partial indexes / partitioning noted as the scale-up path. Next in M3: #45 (faithful
chunk text) and #14/#48 (retrieval + the query/streaming endpoint).

## 2026-07-06 — M3 #46: validate embedding count & dimension
Closed a silent-data-loss gap the same review surfaced: ingestion `zip(pieces, vectors)`d with no
length check and the embedder returned the backend's `embeddings` verbatim, so a backend handing
back fewer vectors than chunks (contract drift, a truncated response) **dropped the tail chunks and
still marked the document READY** — a direct violation of the suite's own "READY means chunked AND
embedded" invariant. A wrong-dimension vector (operator points at a 1024-dim model with the column at
768) was worse: it sailed past into a cryptic pgvector error, and being a permanent config mistake,
burned all three retry backoffs first.

The guard lives at `embed_in_batches`, the one choke point both `run_ingestion` and the
`backfill_embeddings` command share — so a single check covers every ingestion path and works with
any embedder (including the stubs the tests inject). It raises `EmbeddingCountError` on a count
mismatch and `EmbeddingDimensionError` on a wrong width, each message naming the actual numbers and
the model. The interesting call was classifying the two: a **count** mismatch is treated as
*transient* (it may be a truncated response) so it propagates and the task retries, exhausting into
an observable FAILED doc if it persists; a **dimension** mismatch is *permanent* (a static
mis-config that can't self-heal) so ingestion fails the document immediately instead of wasting the
backoff — directly answering the "burns 3 retries" complaint. `zip(..., strict=True)` at both write
sites backs the boundary check belt-and-braces. Proven on real Postgres as `tenantiq_app`: the
wrong-dim document now fails at the embedder boundary with a config hint, never reaching pgvector.
Recorded as an ADR-0004 addendum. Next in M3: #45 (faithful chunk text) and #14/#48 (query engine).
