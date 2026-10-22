.DEFAULT_GOAL := help

# `make bootstrap` builds this venv and every other target runs out of it. Naming the interpreter
# here rather than trusting an activated shell is the whole point of #54: a target that calls a bare
# `pytest` runs whatever happens to be on PATH, which is how a lockfile ends up guaranteeing nothing.
PYTHON     ?= python3.12
VENV       := $(CURDIR)/backend/.venv
VENV_BIN   := $(VENV)/bin
NODE_MAJOR := 22

# Locks are compiled inside the image CI and the deployed container install from. Dependency
# resolution is platform- and interpreter-specific, so a lock compiled on a developer's macOS
# describes an install nothing in this project ever performs (ADR-0018).
LOCK_IMAGE         := python:3.12-slim
PIP_COMPILE_FLAGS  := --quiet --strip-extras --allow-unsafe --generate-hashes

# The digit in the character class below is load-bearing: without it `k8s-dev` and `k8s-prod` exist,
# work, and are invisible here — a target nobody can discover is a target nobody runs. The comment
# lives outside the recipe because make echoes recipe lines, comments included.
help: ## Show this help
	@grep -E '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

bootstrap: ## Install backend (venv, from the lockfile) + frontend deps — run this first on a fresh clone
	@$(PYTHON) -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null || { \
	  echo "error: '$(PYTHON)' is missing or older than 3.12 (pyproject requires >=3.12)." >&2; \
	  echo "       Install Python 3.12+, or point make at one: make bootstrap PYTHON=/path/to/python3.12" >&2; \
	  exit 1; }
	@major=$$(node -v 2>/dev/null | sed 's/^v//; s/\..*//'); \
	 [ -n "$$major" ] && [ "$$major" -ge $(NODE_MAJOR) ] || { \
	  echo "error: Node $(NODE_MAJOR)+ required, found $$(node -v 2>/dev/null || echo 'no node')." >&2; \
	  echo "       .nvmrc pins the line this project builds against — run 'nvm use'." >&2; \
	  exit 1; }
	$(PYTHON) -m venv $(VENV)
	$(VENV_BIN)/pip install --disable-pip-version-check --quiet --require-hashes -r backend/requirements-dev.lock
	# --no-deps: the lock above already resolved everything, and re-resolving from pyproject's
	# floors here would quietly reintroduce the floating install this issue exists to remove.
	$(VENV_BIN)/pip install --disable-pip-version-check --quiet --no-deps -e backend
	cd frontend && npm ci
	@echo "bootstrap complete — next: make test"

lock: ## Recompile backend/requirements*.lock from pyproject.toml (needs Docker; see LOCK_IMAGE above)
	docker run --rm -v "$(CURDIR)/backend:/w" -w /w $(LOCK_IMAGE) sh -euc \
	  'pip install --quiet --no-cache-dir --disable-pip-version-check pip-tools && \
	   pip-compile $(PIP_COMPILE_FLAGS) --output-file=requirements.lock pyproject.toml && \
	   pip-compile $(PIP_COMPILE_FLAGS) --extra=dev --output-file=requirements-dev.lock pyproject.toml'
	@echo "locks recompiled — review the diff, then 'make bootstrap' to install them"

dev: ## Run the full stack locally (compose: db, redis, ollama, backend, worker, beat, frontend)
	@[ -f .env ] || cp .env.example .env  # seed .env on first run so secrets/tunables take effect
	docker compose up --build

smoke: ## Ingest a sample doc through the running stack and wait for READY (real worker + embedder)
	docker compose exec backend python manage.py smoke_ingest

dev-realm: ## Provision the local Keycloak realm, client and Tenant row so sign-in works (idempotent)
	./scripts/dev-realm.sh

smoke-auth: ## Drive a real sign-in end to end against the running stack, then upload/query/delete (#86)
	./scripts/smoke-auth.sh

test: $(VENV_BIN)/pytest ## Run backend (pytest) and frontend (vitest) tests
	cd backend && $(VENV_BIN)/pytest -q
	cd frontend && npm test --if-present

lint: $(VENV_BIN)/ruff ## Lint backend and frontend
	cd backend && $(VENV_BIN)/ruff check . && $(VENV_BIN)/black --check .
	cd frontend && npm run lint

k8s-dev: ## Render the dev Kubernetes overlay to stdout (pipe it: make k8s-dev | kubectl apply -f -)
	@kubectl kustomize infra/k8s/overlays/dev

k8s-prod: ## Render the prod Kubernetes overlay to stdout (see docs/deployment.md for the ordering)
	@kubectl kustomize infra/k8s/overlays/prod

eval: ## Measure retrieval quality against the curated dataset (real embedder, in the running stack)
	# Inside the stack on purpose: retrieval numbers are only meaningful against the real embedder,
	# and on the host TENANTIQ_EMBEDDER_FACTORY points at an Ollama that is usually not reachable.
	# Every report states the embedder, model and dimension that produced it, so a run that used
	# the lexical stand-in can never be mistaken for a baseline (#21).
	docker compose exec backend python -m app.eval.run

eval-faithfulness: ## Also generate + judge an answer per question (#22). Two model calls each: minutes.
	docker compose exec backend python -m app.eval.run --faithfulness

# Not .PHONY on purpose. These are real files, so make skips the recipe when the venv is present and
# runs it — failing with the fix named — when it is not. A `make test` that dies on "pytest: command
# not found" is the fresh-clone experience #54 was filed about.
$(VENV_BIN)/pytest $(VENV_BIN)/ruff:
	@echo "error: the backend venv is missing or incomplete — run 'make bootstrap'" >&2; exit 1

.PHONY: help bootstrap lock dev smoke dev-realm smoke-auth k8s-dev k8s-prod test lint eval eval-faithfulness
