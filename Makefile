.DEFAULT_GOAL := help

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

dev: ## Run the full stack locally (compose)
	docker compose up --build

test: ## Run backend (pytest) and frontend (vitest) tests
	cd backend && pytest -q
	cd frontend && npm test --if-present

lint: ## Lint backend and frontend
	cd backend && ruff check . && black --check .
	cd frontend && npm run lint

eval: ## Run the retrieval / faithfulness evaluation suite
	cd backend && python -m app.eval.run

.PHONY: help dev test lint eval
