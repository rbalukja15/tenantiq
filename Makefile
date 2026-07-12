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

eval: ## Run the retrieval / faithfulness evaluation suite
	cd backend && python -m app.eval.run

.PHONY: help dev test lint eval
