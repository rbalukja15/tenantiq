# ADR-0001 — Stack & scope

- **Status:** Accepted
- **Date:** 2026-06-26

## Context

TenantIQ is a portfolio-grade, production-shaped multi-tenant RAG SaaS. It needs to
demonstrate senior full-stack + DevOps ability and modern AI engineering, while staying
small enough for one engineer to ship and operate. The constraints that shaped this:

- Strict per-tenant data isolation is a first-class requirement, not an afterthought.
- Retrieval and vectors should live close to the relational data so isolation is uniform.
- The AI layer must be *measurable* (evaluated), not just demoable.
- The stack should mirror real, in-demand production tooling.

## Decision

- **Backend: Django REST Framework.** Mature ORM makes tenant scoping enforceable in one
  place; batteries-included reduces incidental work.
- **Frontend: Next.js (App Router) + TypeScript.** First-class streaming UI and type safety.
- **Vector store: Postgres + pgvector.** A single datastore keeps chunks, embeddings, and
  tenant ownership in the same scoped rows — no second system to keep isolated and in sync.
- **Async: Celery + Redis.** Ingestion (parse → chunk → embed) is slow and must not block
  requests.
- **Auth: OIDC with per-tenant identity providers (Keycloak in dev).** The tenant is resolved only
  from a verified token claim.
- **LLM: Anthropic API with an Ollama fallback.** Quality by default; a local/cost lever.
- **Scope for v1.0:** document Q&A with citations, multi-tenant isolation, an evaluation
  harness, and a live deployment. Out of scope: billing, org/role management beyond tenants,
  multi-modal documents.

## Consequences

- Isolation logic centralizes in the ORM layer and middleware (see ADR-0002).
- pgvector index choice and chunking strategy become explicit, tested decisions (ADR-0003).
- The evaluation harness (M5) is a deliverable, not optional — it is the project's
  differentiator and gates "done".
- Choosing one datastore trades some best-of-breed vector features for operational
  simplicity and stronger isolation guarantees — an acceptable trade at this scale.
