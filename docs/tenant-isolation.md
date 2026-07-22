# Tenant isolation (how it works)

TenantIQ's central promise is that **no request can read or write another tenant's data**. The
strategy is [ADR-0002](adr/0002-tenant-isolation.md): shared database, shared schema, row-level
isolation, enforced in **two independent layers**. This page is the practical guide; #7 resolves
the tenant, #8 enforces it, #9 proves it.

## The current tenant

The tenant is derived **only** from the verified OIDC token (issue #7) — never from a header,
query param, or body. On each authenticated request the DRF authenticator calls
`activate_tenant(tenant)` (`app/tenant_context.py`), which sets the tenant in **both** layers.
`TenantContextMiddleware` clears the contextvar after the response; the Postgres GUC self-resets at
the end of the request transaction (`ATOMIC_REQUESTS`).

## Layer 1 — application (ORM)

`TenantOwnedModel` (`app/models.py`) is the abstract base for every tenant-owned table. It adds a
non-null `tenant` FK and a tenant-scoped default manager:

- `Model.objects` — filters every query to the active tenant, and **raises `NoActiveTenant`** if
  no tenant is in context. A forgotten scope is a loud error, never a silent all-tenant read.
- `Model.all_objects` — the explicit, unscoped manager for system paths (and Django internals, via
  `base_manager_name`). Use it only when you deliberately mean "across all tenants".
- `save()` binds a new row to the active tenant and refuses to write a row for a different one.

Outside a request (Celery tasks, the shell, tests) wrap work in `tenant_context(tenant)`.

### Adding a tenant-owned model

1. Subclass `TenantOwnedModel`.
2. `makemigrations`.
3. Add the new table name to `TENANT_OWNED_TABLES` in a new RLS migration modelled on
   `app/migrations/0003_tenant_rls.py` (so Layer 2 covers it too).
4. Add a cross-tenant isolation test (the standing rule in `CLAUDE.md`).

## Layer 2 — database (Postgres RLS)

`0003_tenant_rls.py` enables **and forces** row-level security on each tenant-owned table with a
policy `USING (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid)` (and the
same `WITH CHECK` for writes). The app sets `app.current_tenant` with `SET LOCAL` per request, so
the database filters every row even if the ORM is bypassed (raw SQL, a manager mistake). `NULLIF`
maps an unset/empty GUC to NULL, which matches no row — the safe default.

**This only works if the app connects as a non-superuser role.** Superusers and `BYPASSRLS` roles
skip RLS entirely, and `FORCE` is what makes the policy apply to the table owner too. So the app
connects as **`tenantiq_app`** (`NOSUPERUSER NOBYPASSRLS`), created by
`infra/postgres/init/10-app-role.sql` (compose) / the equivalent CI step. The superuser `tenantiq`
is bootstrap-only. RLS is Postgres-only; on SQLite (local unit tests) the migration is a no-op and
Layer 1 carries isolation.

## How it's tested

Isolation is proven by tests at every layer — a query path without a cross-tenant test is not done
(the standing rule in `CLAUDE.md`).

- **`tests/test_tenant_isolation.py` — the #9 adversarial proof.** Seeds two tenants and asserts A
  can never reach B:
  - **API, both directions** — A's token lists only A's documents; B's lists only B's.
  - **Forged client input** — a request carrying `?tenant_id=<B>` is *ignored*; scope comes from the
    verified `iss`, so client input can't widen it. An unauthenticated request gets 401.
  - **ORM** — inside A's context, fetching B's row by its exact id returns nothing (and `.get()`
    raises `DoesNotExist`).
  - **RLS backstop (Postgres only)** — with the application filter **deliberately removed** (the
    unscoped `all_objects` manager, then raw SQL), the database still returns only A's rows. Proves
    Layer 2 stands alone when Layer 1 is bypassed.
- `tests/test_query.py` — the streaming query path (#48): a tenant's question can never surface or
  cite another tenant's chunks (proven with an answer that echoes its sources, so a leak would show).
- `tests/test_authentication.py` / `tests/test_verifier.py` — the resolution seam (the one place a
  bug would undermine *both* layers): a valid token maps to its tenant; missing, malformed, expired,
  unknown-issuer, wrong-audience, bad-signature, and `alg:none` tokens are all rejected with 401 —
  and a token for a **deactivated** tenant (`is_active=False`) is rejected too (offboarding).
- `tests/test_scoped_manager.py` — Layer 1 in isolation: scoping, raise-on-no-tenant, write guard.
- `tests/test_rls.py` — Layer 2 in isolation: raw SQL as the app role can't read, insert, **update,
  or delete** across tenants. Includes a **meta-guard** that introspects `pg_class`/`pg_policies` and
  asserts *every* concrete `TenantOwnedModel` table has RLS enabled + forced with the tenant-isolation
  policy — so a new tenant-owned table added without its RLS migration fails CI (#50).
- `tests/test_documents_api.py` — the guarantee at the HTTP edge, plus context cleanup after the
  response.

The RLS tests are **skipped off Postgres**; CI runs the whole suite against pgvector Postgres as the
non-superuser `tenantiq_app` role, so Layer 2 is exercised for real. A CI guard
(`TENANTIQ_REQUIRE_POSTGRES`) **fails the job** if the suite isn't on Postgres or any of these
Postgres-only proofs silently skips — so "isolation is sacred" can never go unproven while CI stays
green (#50).
