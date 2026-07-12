# ADR-0006 — Local dev containerization (one-command full stack)

- **Status:** Accepted
- **Date:** 2026-07-12

## Context

The README advertised `make dev` as "the full stack," but compose only started `db`, `redis`, and a
profile-gated `keycloak` — there was no backend, no frontend, and critically **no Celery worker and
no Ollama**, so the merged M2 ingestion pipeline could not actually run via compose (#23). Worse,
nothing loaded `.env` into Django: a developer who copied `.env.example` and ran the app **silently
got SQLite with RLS absent**, because `settings.py` reads `os.environ` directly and no process
loaded the file. For a build-in-the-open portfolio, "clone it and it runs" is table stakes, and the
top devex defect was that it didn't.

Forces:

- **One command.** `docker compose up` should bring up everything needed to take a document from
  upload to READY — including the async worker and the embedder — with no extra manual steps.
- **Isolation stays sacred (ADR-0002).** The app must run as the non-superuser `tenantiq_app` role
  inside compose too, or RLS silently stops enforcing.
- **Two run modes.** Both host runs (`python manage.py …` against `docker compose up db redis`) and
  the fully-composed stack must get correct configuration from one `.env`.
- **Lean and reproducible.** Match the existing grain — no heavyweight orchestration, images that
  build from the same lockfile the app already uses.

## Decision

**Dockerize backend + worker (one image) and frontend, add Ollama as a service, and make `.env`
actually take effect.**

- **One backend image, two commands.** `backend/Dockerfile` builds the app once; the compose
  `backend` service runs `runserver`, the `worker` service runs `celery -A config worker`. They
  share a **`media` volume** so the worker can read the files the API wrote (ingestion opens
  `doc.file` from disk). The dev stack bind-mounts the source over `/app` for autoreload.
- **Ollama as a compose service**, not host-Ollama, so `docker compose up` is genuinely one command.
  A one-shot **`ollama-pull`** sidecar pulls the embedding model into a named volume and exits; the
  `worker` waits on `service_completed_successfully` so the first ingestion never races a missing
  model.
- **A one-shot `migrate` service** applies migrations once (as `tenantiq_app`) before `backend` and
  `worker` start (`depends_on … service_completed_successfully`), so neither races an unmigrated
  schema and there is no ambiguity about who runs migrations. Healthchecks on `db`/`redis`/`ollama`
  gate the dependents.
- **`.env` takes effect two ways.** For host runs, `settings.py` loads the repo-root `.env` via
  **python-dotenv** (skipped under pytest to keep the suite hermetic; `override=False` so a real
  environment variable always wins). For compose, each service sets the **infra hostnames
  explicitly** (`db`/`redis`/`ollama`, not `localhost`) and interpolates secrets/tunables from the
  repo `.env` with safe defaults — so a missing `.env` never breaks `up`.
- **`make dev`** seeds `.env` from `.env.example` on first run, then `docker compose up --build`.
  **`make smoke`** runs `manage.py smoke_ingest`, which pushes a sample document through the real
  broker → worker → embedder and waits for READY (the acceptance check).

### Rejected alternatives

- **Documented host-Ollama.** Lighter, but breaks the "one command" promise — `docker compose up`
  alone wouldn't embed. The service costs an image + a model pull, which is worth the true one-command
  story for a portfolio.
- **`env_file: .env` on each service.** The natural reading of the task, but it injects the file's
  `localhost` URLs into containers (wrong for the compose network, needing per-key overrides anyway)
  and, on this Compose version, a missing `.env` is a hard error. Explicit `environment:` +
  interpolation + the settings-level dotenv load is more robust and covers host runs too.
- **Separate backend and worker images.** Needless duplication — they run the same code with
  different entrypoints; one image with two commands is smaller and simpler.
- **Running migrations in the backend's start command.** Races the worker (which also needs the
  schema) and re-runs on every backend restart; a one-shot `migrate` service is unambiguous.

## Consequences

- `docker compose up` brings up db + redis + ollama + backend + worker + frontend; an uploaded
  document reaches READY through the real worker and embedder (proven by `manage.py smoke_ingest`).
- The stack depends on the Ollama image and a model pull on first run (cached in a named volume
  thereafter). CI is unaffected — it runs the hermetic pytest path (HashingEmbedder), not compose.
- `.env.example` finally takes effect; a fresh clone runs on Postgres with RLS, not silently on
  SQLite. Keycloak stays behind the `dev` profile — auth is orthogonal to the ingestion pipeline
  this stack exists to run.
- Implemented by #23; the truthful README/docs description of `make dev` lands with #56.
