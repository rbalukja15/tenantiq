.DEFAULT_GOAL := help

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

dev: ## Run the full stack locally (compose: db, redis, ollama, backend, worker, frontend)
	@[ -f .env ] || cp .env.example .env  # seed .env on first run so secrets/tunables take effect
	docker compose up --build

smoke: ## Ingest a sample doc through the running stack and wait for READY (real worker + embedder)
	docker compose exec backend python manage.py smoke_ingest

test: ## Run backend (pytest) and frontend (vitest) tests
	cd backend && pytest -q
	cd frontend && npm test --if-present

lint: ## Lint backend and frontend
	cd backend && ruff check . && black --check .
	cd frontend && npm run lint

eval: ## Measure retrieval quality against the curated dataset (real embedder, in the running stack)
	# Inside the stack on purpose: retrieval numbers are only meaningful against the real embedder,
	# and on the host TENANTIQ_EMBEDDER_FACTORY points at an Ollama that is usually not reachable.
	# Every report states the embedder, model and dimension that produced it, so a run that used
	# the lexical stand-in can never be mistaken for a baseline (#21).
	docker compose exec backend python -m app.eval.run

eval-faithfulness: ## Also generate + judge an answer per question (#22). Two model calls each: minutes.
	docker compose exec backend python -m app.eval.run --faithfulness

.PHONY: help dev smoke test lint eval eval-faithfulness
