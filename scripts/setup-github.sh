#!/usr/bin/env bash
#
# setup-github.sh — Scaffold the GitHub backlog for the TenantIQ flagship project.
# Creates labels, milestones, and ~30 fully-specified issues so your repo is
# recruiter-trackable and ready for Claude Code to implement issue-by-issue.
#
# PREREQUISITES:
#   1. Install GitHub CLI:  https://cli.github.com
#   2. Authenticate:        gh auth login
#   3. Create the repo:     gh repo create rbalukja15/tenantiq --public --clone
#   4. From inside the repo (or anywhere), run:  ./setup-github.sh rbalukja15/tenantiq
#
# Run ONCE. Re-running will duplicate issues (labels & milestones are idempotent).

set -uo pipefail

REPO="${1:-rbalukja15/tenantiq}"

command -v gh >/dev/null 2>&1 || { echo "ERROR: GitHub CLI (gh) not found. See https://cli.github.com"; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "ERROR: run 'gh auth login' first."; exit 1; }

echo "Target repo: $REPO"
echo

# ---------------------------------------------------------------------------
# Labels (idempotent via --force)
# ---------------------------------------------------------------------------
echo "Creating labels..."
mklabel() { gh label create "$1" --repo "$REPO" --color "$2" --description "$3" --force >/dev/null && echo "  label: $1"; }

mklabel "type:feature" "1f6feb" "New functionality"
mklabel "type:bug"     "d73a4a" "Something is broken"
mklabel "type:docs"    "0075ca" "Documentation"
mklabel "type:infra"   "5319e7" "Build, CI/CD, deployment"
mklabel "type:ai-llm"  "8957e5" "AI / LLM engineering"
mklabel "type:test"    "0e8a16" "Tests and evaluation"
mklabel "type:chore"   "ededed" "Maintenance / setup"
mklabel "area:backend" "b60205" "Django / API"
mklabel "area:frontend" "fbca04" "Next.js / UI"
mklabel "area:devops"  "006b75" "Containers / K8s / CI"
mklabel "area:ml"      "1d76db" "Models / data science"
mklabel "area:security" "e11d21" "Auth / isolation / privacy"
mklabel "prio:P0"      "b60205" "Must have"
mklabel "prio:P1"      "d93f0b" "Should have"
mklabel "prio:P2"      "fbca04" "Nice to have"
mklabel "effort:S"     "c2e0c6" "<= half a day"
mklabel "effort:M"     "fef2c0" "~1-2 days"
mklabel "effort:L"     "f9d0c4" "3+ days"
echo

# ---------------------------------------------------------------------------
# Milestones (idempotent)
# ---------------------------------------------------------------------------
echo "Creating milestones..."
ensure_milestone() {
  local title="$1" desc="$2"
  if gh api "repos/$REPO/milestones?state=all" --jq '.[].title' 2>/dev/null | grep -qxF "$title"; then
    echo "  exists: $title"
  else
    gh api -X POST "repos/$REPO/milestones" -f title="$title" -f description="$desc" >/dev/null \
      && echo "  created: $title"
  fi
}

M0="M0 — Project Setup & Documentation Foundation"
M1="M1 — Auth & Multi-Tenancy Foundation"
M2="M2 — Document Ingestion Pipeline"
M3="M3 — RAG Query Engine"
M4="M4 — Frontend & Streaming UX"
M5="M5 — Evaluation Harness"
M6="M6 — Deployment & CI/CD"
M7="M7 — Observability & Cost Dashboard"
M8="M8 — Polish & Recruiter-Ready Documentation"

ensure_milestone "$M0" "Repo scaffold, README, docs structure, CI skeleton, CLAUDE.md"
ensure_milestone "$M1" "Keycloak/Better Auth SSO and tenant isolation"
ensure_milestone "$M2" "Upload, parsing, chunking, embeddings into pgvector"
ensure_milestone "$M3" "Retrieval, grounded generation with citations, guardrails"
ensure_milestone "$M4" "Next.js app, streaming chat UI, document management"
ensure_milestone "$M5" "Retrieval + answer-faithfulness evaluation harness"
ensure_milestone "$M6" "Docker, Kubernetes/Kustomize, CD, live demo"
ensure_milestone "$M7" "Structured logging, tracing, per-tenant cost dashboard"
ensure_milestone "$M8" "Diagram, demo GIF, case-study post, v1.0 release"
echo

# ---------------------------------------------------------------------------
# Issues. Body is read from the heredoc on stdin.
# ---------------------------------------------------------------------------
echo "Creating issues..."
mkissue() {
  local title="$1" ms="$2" labels="$3" body
  body="$(cat)"
  gh issue create --repo "$REPO" --title "$title" --milestone "$ms" --label "$labels" --body "$body" >/dev/null \
    && echo "  issue: $title"
}

# ----- M0 -------------------------------------------------------------------
mkissue "Scaffold repository and developer tooling" "$M0" "type:chore,area:devops,prio:P0,effort:S" <<'EOF'
## Context
Set up the project skeleton so every later issue has a clean foundation.

## Tasks
- [ ] Create `backend/` (Django REST) and `frontend/` (Next.js + TS) directories
- [ ] Add ruff + black (Python) and eslint + prettier (TS) configs
- [ ] Add `.editorconfig`, `.gitignore`, and a `Makefile` (dev/test/lint targets)
- [ ] Add pre-commit hooks

## Acceptance criteria
- `make lint` and `make test` run (even if empty) without error.
EOF

mkissue "Write project README with vision, architecture, and roadmap" "$M0" "type:docs,prio:P0,effort:S" <<'EOF'
## Context
The README is the recruiter's first impression. It must explain the what/why and link the roadmap.

## Tasks
- [ ] One-line pitch + CI/license badges + (placeholder) live demo link
- [ ] Problem statement and target user
- [ ] Architecture section with a mermaid diagram
- [ ] Tech stack with rationale; local run instructions
- [ ] Roadmap linking to milestones

## Acceptance criteria
- A new reader understands the project and how to run it in under 2 minutes.
EOF

mkissue "Establish docs/ structure and ADR-0001 (stack & scope)" "$M0" "type:docs,prio:P0,effort:S" <<'EOF'
## Context
Architecture Decision Records make your reasoning visible to senior reviewers.

## Tasks
- [ ] Create `docs/architecture.md`, `docs/devlog.md`, `docs/evaluation.md`
- [ ] Create `docs/adr/` with an ADR template (Context / Decision / Consequences)
- [ ] Write ADR-0001: why a multi-tenant RAG SaaS, and the core stack choices

## Acceptance criteria
- ADR-0001 is committed and linked from the README.
EOF

mkissue "Configure CI skeleton (lint + test) with status badge" "$M0" "type:infra,area:devops,prio:P0,effort:S" <<'EOF'
## Context
A green CI badge signals you ship reliably.

## Tasks
- [ ] `.github/workflows/ci.yml` running lint + tests for backend and frontend on every PR
- [ ] Add issue templates and a PR template (PR template must prompt for `Closes #<n>`)
- [ ] Add the CI badge to the README

## Acceptance criteria
- CI runs and passes on a trivial PR.
EOF

mkissue "Add CLAUDE.md for Claude Code" "$M0" "type:docs,type:chore,prio:P1,effort:S" <<'EOF'
## Context
CLAUDE.md is auto-loaded by Claude Code each session and keeps it on your conventions.

## Tasks
- [ ] Run `/init` then refine: project context, commands, conventions, per-issue workflow
- [ ] Encode rules: Conventional Commits, one-issue-per-PR, LLM never computes numbers, tenant isolation is tested
- [ ] Keep it under ~200 lines

## Acceptance criteria
- CLAUDE.md committed at repo root.
EOF

# ----- M1 -------------------------------------------------------------------
mkissue "ADR-0002: tenant isolation strategy" "$M1" "type:docs,area:security,prio:P0,effort:S" <<'EOF'
## Context
Decide and document how tenant data is isolated before building it.

## Tasks
- [ ] Compare row-level scoping vs schema-per-tenant; pick one
- [ ] Document trade-offs and the chosen enforcement point

## Acceptance criteria
- ADR-0002 committed and referenced by the auth/middleware issues.
EOF

mkissue "Integrate SSO (Keycloak / Better Auth) with per-tenant providers" "$M1" "type:feature,area:backend,area:security,prio:P0,effort:L" <<'EOF'
## Context
Authentication with per-tenant identity — your senior differentiator.

## Tasks
- [ ] Wire OIDC login flow; map tokens to a tenant + user
- [ ] Support per-tenant provider configuration
- [ ] Protect API routes; handle token refresh

## Acceptance criteria
- A user can log in and is correctly associated with exactly one tenant.
EOF

mkissue "Tenant-scoped request middleware and row-level isolation" "$M1" "type:feature,area:backend,area:security,prio:P0,effort:M" <<'EOF'
## Context
Every query must be scoped to the caller's tenant automatically.

## Tasks
- [ ] Middleware resolves tenant from the authenticated principal
- [ ] Default query manager filters by tenant
- [ ] Reject/block any unscoped data access path

## Acceptance criteria
- No endpoint can return another tenant's rows.
EOF

mkissue "Tests: cross-tenant isolation cannot leak" "$M1" "type:test,area:security,prio:P0,effort:S" <<'EOF'
## Context
Prove the isolation guarantee with automated tests — a strong trust signal.

## Tasks
- [ ] Seed two tenants with distinct data
- [ ] Assert tenant A cannot read/query tenant B via any endpoint

## Acceptance criteria
- Isolation tests run in CI and pass.
EOF

# ----- M2 -------------------------------------------------------------------
mkissue "Document upload endpoint and storage" "$M2" "type:feature,area:backend,prio:P1,effort:M" <<'EOF'
## Context
Tenants upload documents to be indexed.

## Tasks
- [ ] Upload endpoint (PDF/text) with size/type validation, tenant-scoped
- [ ] Store the raw file and a Document record with status

## Acceptance criteria
- An uploaded doc is persisted and visible only to its tenant.
EOF

mkissue "ADR-0003 + parsing & chunking pipeline (Celery)" "$M2" "type:ai-llm,type:feature,area:backend,prio:P1,effort:M" <<'EOF'
## Context
Chunking quality drives retrieval quality. Decide the strategy, then build it async.

## Tasks
- [ ] ADR-0003: chunk size, overlap, and splitting strategy with rationale
- [ ] Celery task: parse document, split into chunks, persist
- [ ] Handle large files and failures gracefully

## Acceptance criteria
- A queued document produces stored, tenant-scoped chunks.
EOF

mkissue "Embedding generation and pgvector storage" "$M2" "type:ai-llm,type:feature,area:backend,prio:P0,effort:M" <<'EOF'
## Context
Chunks become vectors for semantic search.

## Tasks
- [ ] Generate embeddings for each chunk
- [ ] Store vectors in Postgres via pgvector with an appropriate index
- [ ] Backfill command for existing chunks

## Acceptance criteria
- A nearest-neighbour query returns relevant chunks for a sample question.
EOF

mkissue "Ingestion observability: status, retries, failures" "$M2" "type:feature,area:backend,prio:P2,effort:S" <<'EOF'
## Context
Ingestion is async; its state must be visible.

## Tasks
- [ ] Track per-document status (queued/processing/done/failed)
- [ ] Retry transient failures; surface terminal errors

## Acceptance criteria
- A failed ingestion is observable and retryable.
EOF

# ----- M3 -------------------------------------------------------------------
mkissue "Vector retrieval and prompt assembly" "$M3" "type:ai-llm,area:backend,prio:P0,effort:M" <<'EOF'
## Context
Given a question, retrieve the right tenant-scoped context.

## Tasks
- [ ] Embed the query; retrieve top-k tenant-scoped chunks
- [ ] Assemble a grounded prompt with source references
- [ ] Make k and thresholds configurable

## Acceptance criteria
- Retrieval returns only the asking tenant's chunks, ranked by relevance.
EOF

mkissue "Grounded answer generation with enforced citation schema" "$M3" "type:ai-llm,area:backend,prio:P0,effort:M" <<'EOF'
## Context
The model must answer from context and cite sources — never free-form.

## Tasks
- [ ] Call the LLM with the grounded prompt
- [ ] Enforce structured output (answer + citations to chunk IDs) via schema/function calling
- [ ] Refuse / say "not found" when context is insufficient

## Acceptance criteria
- Every answer includes valid citations resolvable to source chunks.
EOF

mkissue "PII redaction and prompt-injection guardrails" "$M3" "type:ai-llm,area:security,prio:P1,effort:M" <<'EOF'
## Context
Your security background shows here: protect inputs and outputs.

## Tasks
- [ ] Redact obvious PII on ingest/where appropriate
- [ ] Basic prompt-injection mitigations on retrieved content
- [ ] Document the threat model in docs/

## Acceptance criteria
- A document containing injection text cannot override system instructions in a test.
EOF

mkissue "Per-tenant cost and token accounting" "$M3" "type:feature,area:backend,prio:P1,effort:S" <<'EOF'
## Context
Production AI means knowing what each tenant costs.

## Tasks
- [ ] Record tokens + estimated cost per request, per tenant
- [ ] Expose an aggregate query/endpoint

## Acceptance criteria
- Cost per tenant is queryable for a time range.
EOF

# ----- M4 -------------------------------------------------------------------
mkissue "Next.js app shell with auth integration" "$M4" "type:feature,area:frontend,prio:P0,effort:M" <<'EOF'
## Context
The frontend hub, wired to SSO.

## Tasks
- [ ] App Router shell, layout, protected routes
- [ ] Login/logout via the OIDC flow; show current tenant

## Acceptance criteria
- An unauthenticated user is redirected to login; authenticated users see their tenant.
EOF

mkissue "Streaming chat UI with citation rendering" "$M4" "type:feature,area:frontend,prio:P0,effort:M" <<'EOF'
## Context
The core experience: ask a question, watch the grounded answer stream in with sources.

## Tasks
- [ ] Streaming responses (token-by-token)
- [ ] Render citations as links back to source documents/chunks
- [ ] Loading, empty, and error states

## Acceptance criteria
- A question returns a streamed answer with clickable citations.
EOF

mkissue "Document management UI (upload, list, status)" "$M4" "type:feature,area:frontend,prio:P1,effort:S" <<'EOF'
## Context
Tenants manage their corpus from the UI.

## Tasks
- [ ] Upload component with progress
- [ ] List documents with ingestion status
- [ ] Delete document (and its vectors)

## Acceptance criteria
- A user can upload, see status reach "done", then query that document.
EOF

# ----- M5 -------------------------------------------------------------------
mkissue "Build eval dataset and retrieval metrics" "$M5" "type:ai-llm,type:test,prio:P0,effort:M" <<'EOF'
## Context
THE differentiator: measure retrieval quality, don't just eyeball it.

## Tasks
- [ ] Curate a small Q/relevant-chunk dataset
- [ ] Implement precision@k / recall@k and a `make eval` runner
- [ ] Record results in docs/evaluation.md

## Acceptance criteria
- `make eval` prints retrieval metrics reproducibly.
EOF

mkissue "Answer-faithfulness evaluation (LLM-as-judge)" "$M5" "type:ai-llm,type:test,prio:P1,effort:M" <<'EOF'
## Context
Measure whether answers are grounded in the cited context.

## Tasks
- [ ] Faithfulness/grounding score via an LLM judge over the eval set
- [ ] Flag hallucinated or uncited claims
- [ ] Add results + methodology to docs/evaluation.md

## Acceptance criteria
- A faithfulness score is produced and documented with methodology.
EOF

# ----- M6 -------------------------------------------------------------------
mkissue "Dockerize services and docker-compose for local" "$M6" "type:infra,area:devops,prio:P0,effort:M" <<'EOF'
## Context
Reproducible local + deployable images.

## Tasks
- [ ] Dockerfiles for backend, frontend, worker
- [ ] docker-compose with Postgres+pgvector and Redis
- [ ] Document `make dev` via compose

## Acceptance criteria
- `docker compose up` brings the full stack up locally.
EOF

mkissue "Kubernetes manifests with Kustomize overlays" "$M6" "type:infra,area:devops,prio:P1,effort:M" <<'EOF'
## Context
Show the K8s + Kustomize skill from your real experience.

## Tasks
- [ ] Base manifests (deployments, services, config, secrets)
- [ ] `dev` and `prod` Kustomize overlays
- [ ] Document the deploy flow

## Acceptance criteria
- `kustomize build overlays/dev` produces valid manifests.
EOF

mkissue "CD pipeline and deploy to a live environment" "$M6" "type:infra,area:devops,prio:P0,effort:M" <<'EOF'
## Context
A live URL beats any screenshot.

## Tasks
- [ ] Build + push images in CI on main
- [ ] Deploy to a hosted environment (Fly.io / Render / Hetzner+k3s)
- [ ] Put the live demo link in the README

## Acceptance criteria
- Merging to main deploys; the demo link works.
EOF

# ----- M7 -------------------------------------------------------------------
mkissue "Structured logging and request tracing" "$M7" "type:infra,area:backend,prio:P2,effort:S" <<'EOF'
## Context
Production observability (mirrors your Pino experience).

## Tasks
- [ ] Structured logs with request + tenant correlation IDs
- [ ] Trace a request across API -> retrieval -> LLM

## Acceptance criteria
- Logs allow following a single request end-to-end.
EOF

mkissue "Per-tenant usage and cost dashboard" "$M7" "type:feature,area:frontend,prio:P2,effort:M" <<'EOF'
## Context
Surface the cost accounting as a visible dashboard.

## Tasks
- [ ] Charts for queries, tokens, and cost per tenant over time
- [ ] Date-range filter

## Acceptance criteria
- An admin can view usage and cost trends per tenant.
EOF

# ----- M8 -------------------------------------------------------------------
mkissue "Architecture diagram and demo GIF in README" "$M8" "type:docs,prio:P0,effort:S" <<'EOF'
## Context
Make the README instantly impressive.

## Tasks
- [ ] Finalize the mermaid architecture diagram
- [ ] Record a 30-second demo GIF/video and place it at the top of the README

## Acceptance criteria
- README opens with a demo visual and a clear diagram.
EOF

mkissue "Write the build-log / case-study post" "$M8" "type:docs,prio:P1,effort:S" <<'EOF'
## Context
Turn the work into a narrative — your interview story bank and inbound signal.

## Tasks
- [ ] Summarize key decisions (isolation, chunking, where you don't trust the LLM, eval results)
- [ ] Publish to dev.to/LinkedIn and link from the README

## Acceptance criteria
- A published post links back to the repo and the live demo.
EOF

mkissue "Final pass: CHANGELOG, pin repo, tag v1.0" "$M8" "type:docs,type:chore,prio:P1,effort:S" <<'EOF'
## Context
Ship it and make it findable.

## Tasks
- [ ] Write CHANGELOG.md
- [ ] Pin the repo on your profile
- [ ] Tag and publish the v1.0 release

## Acceptance criteria
- A v1.0 release exists; the repo is pinned with a working demo link.
EOF

echo
echo "Done. Review your Issues tab and milestone progress bars:"
echo "  https://github.com/$REPO/issues"
echo "  https://github.com/$REPO/milestones"
echo
echo "Next: create a PUBLIC GitHub Project board, add these issues, then start with issue #1."
