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
