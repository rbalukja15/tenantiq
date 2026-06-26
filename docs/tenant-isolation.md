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

- `tests/test_scoped_manager.py` — Layer 1: scoping, raise-on-no-tenant, write guard (SQLite).
- `tests/test_documents_api.py` — the guarantee at the HTTP edge + middleware cleanup.
- `tests/test_rls.py` — Layer 2: raw-SQL reads/writes blocked across tenants; **skipped off
  Postgres**. CI runs the whole suite against pgvector Postgres as `tenantiq_app`.
