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
