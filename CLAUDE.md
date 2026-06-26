# TenantIQ — Multi-Tenant RAG SaaS

## Project context
Multi-tenant SaaS: each tenant uploads documents and queries them via RAG with citations.
- Backend: Django REST (`backend/`)
- Frontend: Next.js App Router + TypeScript (`frontend/`)
- Storage: Postgres + pgvector
- Async: Celery + Redis for ingestion
- LLM: Anthropic API, with an Ollama fallback

## Commands
- `make dev`   — run the full stack locally (compose: backend, frontend, postgres+pgvector, redis)
- `make test`  — run pytest + vitest (run before every commit)
- `make lint`  — ruff + black --check + eslint
- `make eval`  — run the retrieval / faithfulness evaluation suite

## Conventions
- Conventional Commits (`feat:`, `fix:`, `docs:`, `test:`, `chore:`, `infra:`).
- One issue per PR. The PR body MUST contain `Closes #<n>`.
- Branch naming: `feat/<issue#>-slug`, `docs/<issue#>-slug`, `fix/<issue#>-slug`.
- TypeScript strict, no `any`. Python: full type hints, ruff clean, black formatted.
- **The LLM never computes numbers and never invents citations.** Answers are grounded
  in retrieved tenant-scoped chunks; citations resolve to real chunk IDs.
- **Tenant isolation is sacred.** Every data path is tenant-scoped; any new query path
  ships with a test proving no cross-tenant leak.
- Every real decision gets an ADR in `docs/adr/` (Context / Decision / Consequences).
- After finishing an issue, add a dated entry to `docs/devlog.md`.

## Per-issue workflow
When I say "implement issue #N":
1. Run `gh issue view N` and follow its tasks and acceptance criteria exactly.
2. Write the code AND its tests.
3. Update any relevant docs (README, ADR, devlog, evaluation).
4. Open a PR whose body includes `Closes #N`, following the PR template.
Ask before destructive actions; keep diffs scoped to the one issue.
