# ADR-0002 — Tenant isolation strategy

- **Status:** Accepted
- **Date:** 2026-06-26

## Context

TenantIQ is multi-tenant: many organizations ("tenants") store private documents and query
them via RAG. The product's central promise — and its hardest security requirement — is that
**no request may ever read or surface data belonging to another tenant.** A single leak (one
missing `WHERE tenant_id = …`) would be a critical, trust-destroying failure.

We must decide *where* tenant data lives and *where* isolation is enforced before any
tenant-owned model is written, because the auth and middleware work (issues #7–#9) builds
directly on this decision. The forces at play:

- **Security is paramount.** Isolation must survive ordinary developer mistakes, not merely
  hold when everyone remembers to filter.
- **One small team / one operator.** Whatever we choose, we migrate, back up, and operate it
  ourselves. Per-tenant operational fan-out is expensive.
- **Vectors live in Postgres (pgvector — see [ADR-0001](0001-stack-and-scope.md)).** Chunks,
  embeddings, and ownership should sit in the same scoped rows, so there is no second datastore
  to keep isolated and in sync.
- **Moderate scale.** Tens to low-hundreds of tenants, not tens of thousands — so the heaviest
  isolation models are not required.

Three isolation models are on the table: **row-level** (shared DB, shared schema),
**schema-per-tenant** (shared DB, one Postgres schema per tenant), and **database-per-tenant**.

## Decision

**Shared database, shared schema, with row-level isolation — enforced in two independent
layers (defense in depth).**

**Data model.** Every tenant-owned table carries a non-null `tenant_id` foreign key to
`Tenant`. A `TenantOwnedModel` abstract base (issue #8) makes this the default for all such
models, so "owned by a tenant" is structural rather than per-developer discipline. Tenant
primary keys are **UUIDs** (non-guessable, safe to carry in tokens and DB session variables).

**Tenant resolution.** The current tenant is derived **only** from a verified token claim
(e.g. `tenant`/`org` in the OIDC JWT, validated in issue #7) — **never** from a client-supplied
header, query parameter, or body field. The client cannot assert which tenant it is.

**Layer 1 — Application (ORM).** A request-scoped "current tenant" is set from the verified
token and stored in a `contextvar`. A `TenantScopedManager` adds `WHERE tenant_id = <current>`
to the default queryset of every tenant-owned model. Accessing a tenant-owned model with **no**
tenant in context **raises**, rather than silently returning all rows. The safe path is the
default path, and "forgot to scope" becomes a loud error instead of a silent leak.

**Layer 2 — Database (Postgres Row-Level Security).** RLS is enabled and forced on every
tenant-owned table with a policy of the form
`USING (tenant_id = current_setting('app.current_tenant')::uuid)`. The session variable
`app.current_tenant` is set with `SET LOCAL` inside each request's transaction, so it resets
automatically at commit/rollback and can never leak across pooled connections. Even if a bug
bypasses the ORM — raw SQL, a manager mistake, a careless future query — the database itself
refuses to read or write another tenant's rows.

The layers are independent: Layer 1 is fast and ergonomic; Layer 2 is the backstop that holds
when Layer 1 fails. Isolation is considered real only once it is **proven by tests at every
layer** (issue #9), including a test that the database blocks cross-tenant reads *with the
application filter deliberately removed*.

### Rejected alternatives

- **Schema-per-tenant** (one Postgres schema per tenant). Stronger physical separation, but
  every migration must fan out across N schemas, `search_path`/connection management grows
  complex, and pgvector indexes are duplicated per schema. Operationally heavy for a one-person
  team at our scale, with no isolation benefit that RLS does not already provide.
- **Database-per-tenant.** Strongest isolation and the cleanest "delete a tenant" story, but
  provisioning, migrating, backing up, monitoring, and pooling N databases is disproportionate
  at tens–hundreds of tenants, and fleet-wide analytics (e.g. per-tenant cost reporting in M7)
  become painful. Overkill for v1.
- **Single-layer row scoping (ORM only, no RLS).** Simpler, but one forgotten filter or one raw
  query leaks everything. The entire product promise rides on this guarantee, so the database
  backstop earns its keep.

## Consequences

- Isolation logic centralizes in two well-defined places — `TenantScopedManager` + middleware
  and the RLS migration (both issue #8) — rather than being scattered across views. New
  tenant-owned models inherit isolation by extending `TenantOwnedModel`.
- Every tenant-owned model and query path ships with a test proving no cross-tenant leak
  (issue #9). This is a standing requirement, encoded in `CLAUDE.md`.
- Each request must run with the current tenant set in **both** the contextvar and the Postgres
  GUC; the middleware (#8) owns setting and clearing both. `SET LOCAL` makes the GUC self-resetting
  per transaction, so connection pooling is safe by construction.
- Background work has no request: Celery ingestion tasks (M2) must set the tenant explicitly at
  the start of each task, in both layers.
- The tenant-*resolution* step (issue #7) is the one place a bug would undermine *both* layers,
  so token verification and claim extraction are the highest-value things to test.

Implemented by: #7 (token → tenant), #8 (`TenantScopedManager` + middleware + RLS); proven by #9.
