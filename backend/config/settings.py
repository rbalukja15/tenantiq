"""Django settings for the TenantIQ backend.

Security-sensitive values come from the environment. Per-tenant OIDC config lives in the
database (see the Tenant model), not here — the only OIDC-ish setting here is the pluggable
token-verifier factory, which makes verification injectable for hermetic tests (ADR-0002, #7).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import dj_database_url
from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

# Load the repo-root .env for host (non-compose) runs, so a dev who copies .env.example actually
# gets Postgres + RLS instead of silently falling back to SQLite (#23). override=False means a real
# environment variable (what docker-compose injects per service) always wins over the file. Skipped
# under pytest so the suite stays hermetic — tests set their own environment explicitly.
if "pytest" not in sys.modules:
    load_dotenv(BASE_DIR.parent / ".env")


def _env_bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "django-insecure-dev-only-change-me")
DEBUG = _env_bool("DJANGO_DEBUG", False)
ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,testserver").split(",")

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.staticfiles",
    "rest_framework",
    "app",
]

MIDDLEWARE = [
    # Outermost: names the request on every line logged while serving it (#26, ADR-0024).
    "app.middleware.RequestLogMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
    # Clears the per-request tenant contextvar after the response (ADR-0002, #8).
    "app.middleware.TenantContextMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": []},
    }
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# SQLite by default so unit tests run with zero setup; DATABASE_URL switches to Postgres
# (the real datastore — pgvector, RLS in #8). CI sets DATABASE_URL to a pgvector service.
DATABASES = {
    "default": dj_database_url.config(
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
        conn_max_age=600,
    )
}
# Wrap each request in a transaction so the RLS session variable (set via SET LOCAL in the auth
# seam) is scoped to that request and self-resets at commit/rollback — safe across pooled
# connections (ADR-0002, #8).
DATABASES["default"]["ATOMIC_REQUESTS"] = True

AUTH_USER_MODEL = "app.User"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "app.auth.authentication.TenantOIDCAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
    ],
    # Per-tenant burst limits (#49, ADR-0011). The throttle classes (app.throttling) key the bucket
    # on the tenant, not the user — a tenant's whole workforce shares one budget and can never touch
    # another tenant's. Scopes: query (LLM-backed, expensive) < uploads < reads. Env-overridable so
    # limits are configuration, not code; the query endpoint must be bounded before any public deploy
    # (#25). None disables a scope.
    # 'discovery' is the odd one out: it bounds the unauthenticated tenant-discovery endpoint (#18),
    # which has no tenant to key on and is bucketed per requested *slug* instead. Keeping it a
    # separate scope means anonymous traffic can never spend a real tenant's 'read' budget; the
    # limit is per slug (not global), so it is sized as a per-tenant damper rather than a site cap.
    "DEFAULT_THROTTLE_RATES": {
        "query": os.environ.get("TENANTIQ_THROTTLE_QUERY", "30/min"),
        "upload": os.environ.get("TENANTIQ_THROTTLE_UPLOAD", "20/min"),
        "read": os.environ.get("TENANTIQ_THROTTLE_READ", "120/min"),
        "discovery": os.environ.get("TENANTIQ_THROTTLE_DISCOVERY", "60/min"),
    },
}

# Per-tenant cost & token accounting (#17, ADR-0012). Prices are USD per *million* tokens, read as
# Decimal strings (never floats — money). Defaults track Claude Opus 4.8 list pricing; an operator
# retunes them per deployment/model. Output tokens are the dearer side, hence separate prices. The
# price in effect at write time is baked into each UsageRecord, so a later change can't rewrite history.
TENANTIQ_LLM_PRICE_INPUT_PER_MTOK = os.environ.get("TENANTIQ_LLM_PRICE_INPUT_PER_MTOK", "5.00")
TENANTIQ_LLM_PRICE_OUTPUT_PER_MTOK = os.environ.get("TENANTIQ_LLM_PRICE_OUTPUT_PER_MTOK", "25.00")
# Per-model price overrides are defined with the LLM settings below (they key off the model names
# configured there): see TENANTIQ_LLM_PRICES.

# Per-tenant query *volume* quotas (#49, ADR-0011) — the counting half of the quota hooks, over a
# fixed calendar window. Distinct from the per-minute burst rates above: these cap total query
# requests per tenant per day/month, the guardrail against sustained LLM spend before #17's precise
# cost accounting lands. 0 = unlimited (the hook is present but disabled). Env-overridable.
TENANTIQ_QUERY_DAILY_QUOTA = int(os.environ.get("TENANTIQ_QUERY_DAILY_QUOTA", "1000"))
TENANTIQ_QUERY_MONTHLY_QUOTA = int(os.environ.get("TENANTIQ_QUERY_MONTHLY_QUOTA", "0"))

# Cache backend. Throttle/quota counters must be *shared* across worker processes to be correct, so
# production points the default cache at Redis (reusing REDIS_URL). Under pytest we use a local
# in-memory cache so the suite stays hermetic and each test starts from a clean slate (the autouse
# cache-clear fixture in conftest); a dev box without a cache URL also falls back to local memory
# (single process, still correct there).
_CACHE_URL = os.environ.get(
    "CACHE_URL",
    "" if "pytest" in sys.modules else os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
)
if _CACHE_URL:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": _CACHE_URL,
            "KEY_PREFIX": "tenantiq",
        }
    }
else:
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

# Dotted path to a zero-arg callable returning a TokenVerifier. Tests override this to inject
# a verifier backed by a local test key, so auth tests need no live Keycloak.
TENANTIQ_TOKEN_VERIFIER_FACTORY = "app.auth.verifier.build_default_verifier"

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"

# Uploaded files. Local filesystem now (behind Django's storage API); swap to object storage
# (django-storages) in M6 by config only. Files land under a per-tenant path
# (app.models.tenant_document_path) and are never served publicly.
MEDIA_ROOT = os.environ.get("MEDIA_ROOT", str(BASE_DIR / "media"))
MEDIA_URL = "/media/"

# Upload guardrails enforced by app.serializers.DocumentSerializer.
TENANTIQ_MAX_UPLOAD_BYTES = int(os.environ.get("TENANTIQ_MAX_UPLOAD_BYTES", str(25 * 1024 * 1024)))

# Celery / async ingestion (M2). Broker + result backend are Redis; CI/tests run tasks eagerly.
CELERY_BROKER_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
CELERY_RESULT_BACKEND = os.environ.get("CELERY_RESULT_BACKEND", CELERY_BROKER_URL)
# Run tasks inline by default under pytest, so the suite (and CI) needs no live broker.
CELERY_TASK_ALWAYS_EAGER = _env_bool("CELERY_TASK_ALWAYS_EAGER", "pytest" in sys.modules)
CELERY_TASK_EAGER_PROPAGATES = True

# Bound ingestion work (#47) so a crafted/pathological upload can't monopolize the shared worker.
# The Celery soft limit raises inside the task and is handled as a *permanent* failure (no retry
# amplification); the hard limit (soft + a small grace) SIGKILLs a task that ignores the soft raise.
TENANTIQ_INGEST_SOFT_TIME_LIMIT = int(os.environ.get("TENANTIQ_INGEST_SOFT_TIME_LIMIT", "240"))
TENANTIQ_INGEST_TIME_LIMIT = int(os.environ.get("TENANTIQ_INGEST_TIME_LIMIT", "300"))
# Documents a dead worker left behind (#55, ADR-0023). A task whose process is killed (the hard
# limit above, the OOM killer, a rollout that outlasts the pod's grace period) never reaches
# `on_failure`, so its document would stay PROCESSING forever. Celery beat sends a sweep every
# SWEEP_INTERVAL, and the sweep fails any PROCESSING document whose last change is older than
# REAP_AFTER. Every attempt refreshes that timestamp and the hard limit ends it, so REAP_AFTER has to
# clear a whole attempt and its retry's wait in the queue: `manage.py check`, which `migrate` runs,
# refuses less than twice the limit.
TENANTIQ_INGEST_REAP_AFTER_SECONDS = int(
    os.environ.get("TENANTIQ_INGEST_REAP_AFTER_SECONDS", "1800")
)
TENANTIQ_INGEST_SWEEP_INTERVAL_SECONDS = int(
    os.environ.get("TENANTIQ_INGEST_SWEEP_INTERVAL_SECONDS", "300")
)
CELERY_BEAT_SCHEDULE = {
    "sweep-stuck-documents": {
        "task": "app.tasks.sweep_stuck_documents",
        "schedule": TENANTIQ_INGEST_SWEEP_INTERVAL_SECONDS,
    },
}
# Parsing bounds (#47): reject an oversized document before it burns CPU/memory. Exceeding either is
# a permanent ParseError. Defaults are generous — real documents, not adversarial ones, drive them.
TENANTIQ_MAX_PDF_PAGES = int(os.environ.get("TENANTIQ_MAX_PDF_PAGES", "2000"))
TENANTIQ_MAX_EXTRACTED_CHARS = int(os.environ.get("TENANTIQ_MAX_EXTRACTED_CHARS", str(10_000_000)))

# PII redaction (#16, ADR-0010). Redact recognizable personal data (email, phone, US SSN, Luhn-valid
# payment card) from the extracted text *before* chunking, so it never lands in a stored chunk, the
# vector index, or a generated answer. On by default; disable only for evaluation baselines (#21)
# that need the raw extracted text. A re-ingestion (manage.py reingest_documents) applies it to
# documents ingested before this landed.
TENANTIQ_REDACT_PII = _env_bool("TENANTIQ_REDACT_PII", True)

# Chunking strategy (ADR-0003). Tunable; sized by a chars-per-token estimate until #12's tokenizer.
TENANTIQ_CHUNK_TARGET_TOKENS = int(os.environ.get("TENANTIQ_CHUNK_TARGET_TOKENS", "800"))
TENANTIQ_CHUNK_OVERLAP_TOKENS = int(os.environ.get("TENANTIQ_CHUNK_OVERLAP_TOKENS", "100"))

# Embeddings + vector store (ADR-0004, #12; provider chosen in #53/ADR-0020). The embedder is
# pluggable like the token verifier: a deterministic, dependency-free hashing embedder under pytest
# (no network/secrets), Ollama by default, Voyage in production via TENANTIQ_EMBEDDER_FACTORY.
#
# DIM is 1024 and is the same number everywhere on purpose. The pgvector column and its HNSW index
# are a fixed width baked into a migration, so the repo cannot hold a 768-dim dev and a 1024-dim
# prod without the migration history describing a schema neither environment has. `voyage-4` and
# `mxbai-embed-large` both emit 1024, which is what makes one width serve both. Changing it means a
# migration plus a full re-backfill (manage.py backfill_embeddings) — see docs/deployment.md.
TENANTIQ_EMBEDDING_DIM = int(os.environ.get("TENANTIQ_EMBEDDING_DIM", "1024"))
# The LOCAL (Ollama) embedding model. nomic-embed-text is 768-dim and no longer fits the column.
TENANTIQ_EMBEDDING_MODEL = os.environ.get("TENANTIQ_EMBEDDING_MODEL", "mxbai-embed-large")
TENANTIQ_EMBED_BATCH_SIZE = int(os.environ.get("TENANTIQ_EMBED_BATCH_SIZE", "64"))
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")

# Voyage AI — the hosted embedding provider (#53, ADR-0020). Named TENANTIQ_VOYAGE_MODEL rather than
# reusing TENANTIQ_EMBEDDING_MODEL: that name already means "the Ollama model" in every existing
# .env, and silently repurposing a documented variable is worse than an inconsistent one.
VOYAGE_API_KEY = os.environ.get("VOYAGE_API_KEY", "")
TENANTIQ_VOYAGE_MODEL = os.environ.get("TENANTIQ_VOYAGE_MODEL", "voyage-4")
TENANTIQ_VOYAGE_TIMEOUT_SECONDS = float(os.environ.get("TENANTIQ_VOYAGE_TIMEOUT_SECONDS", "30"))
# Empty counts as unset (`or`, not a get() default): compose forwards this as `${...:-}`, and an
# empty string would otherwise be imported as a dotted path and fail every ingestion and query.
TENANTIQ_EMBEDDER_FACTORY = os.environ.get("TENANTIQ_EMBEDDER_FACTORY") or (
    "app.embeddings.build_fake_embedder"
    if "pytest" in sys.modules
    else "app.embeddings.build_default_embedder"
)

# RAG query engine — retrieval + prompt assembly (M3, #14). Tuned like the chunking knobs.
# TOP_K: how many tenant-scoped chunks to retrieve as candidate context.
# MIN_SIMILARITY: cosine similarity floor (1 - distance, in [-1, 1]); a candidate below it is
# dropped rather than padding the prompt, and if nothing clears the bar the query returns
# "no relevant context". It is the only thing that lets the product refuse: at 0.0 nothing is ever
# dropped, so every question gets sources whether or not the corpus has an answer (#88).
# 0.50 is measured, not guessed (ADR-0021). On the default local embedder, mxbai-embed-large, the
# best match for every answerable question in the evaluation scores 0.567 or more, and for every
# unanswerable one 0.441 or less (docs/evaluation.md); 0.50 sits inside that window with room on
# both sides. The window belongs to one embedder and one corpus, so another model needs its own
# value from `make eval`.
# Empty counts as unset (`or`, not a get() default): compose forwards this as `${...:-}`.
TENANTIQ_RETRIEVAL_TOP_K = int(os.environ.get("TENANTIQ_RETRIEVAL_TOP_K", "5"))
TENANTIQ_RETRIEVAL_MIN_SIMILARITY = float(
    os.environ.get("TENANTIQ_RETRIEVAL_MIN_SIMILARITY") or "0.50"
)

# Grounded answer generation (M3, #15, ADR-0008). The LLM client is pluggable like the embedder: a
# deterministic fake under pytest (no network/key), the Anthropic Messages API otherwise, with an
# Ollama fallback when no key is set. Anthropic is the answer LLM; Ollama's model is a local chat model.
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
TENANTIQ_LLM_MODEL = os.environ.get("TENANTIQ_LLM_MODEL", "claude-opus-4-8")
TENANTIQ_LLM_MAX_TOKENS = int(os.environ.get("TENANTIQ_LLM_MAX_TOKENS", "1024"))
TENANTIQ_LLM_OLLAMA_MODEL = os.environ.get("TENANTIQ_LLM_OLLAMA_MODEL", "llama3.1")
TENANTIQ_LLM_TIMEOUT_SECONDS = int(os.environ.get("TENANTIQ_LLM_TIMEOUT_SECONDS", "60"))
# The context window the *local* model is run with (#90). Ollama defaults llama3.1 to 4096 tokens,
# which is smaller than the prompt this product builds: TOP_K x CHUNK_TARGET_TOKENS is 4,000 tokens
# of sources before the system prompt, so a question that retrieves five full-size chunks overflows
# by ~7% and comes back as an HTTP 500. Hosted models (Anthropic: 200k) are unaffected and ignore
# this. RESPONSE_HEADROOM reserves room for the answer itself, since the window covers both.
TENANTIQ_LLM_NUM_CTX = int(os.environ.get("TENANTIQ_LLM_NUM_CTX", "8192"))
# The local backend gets its own, longer timeout. TENANTIQ_LLM_TIMEOUT_SECONDS was sized for a hosted
# API; CPU inference over a prompt this size is minutes, not seconds, so the shared 60s value turned
# the #90 fix into a timeout instead of a 500 — a different failure with the same user-visible
# outcome. Hosted backends keep the shorter timeout, where 60s really does mean something is wrong.
TENANTIQ_LLM_OLLAMA_TIMEOUT_SECONDS = int(
    os.environ.get("TENANTIQ_LLM_OLLAMA_TIMEOUT_SECONDS", "300")
)
TENANTIQ_LLM_RESPONSE_HEADROOM_TOKENS = int(
    os.environ.get("TENANTIQ_LLM_RESPONSE_HEADROOM_TOKENS", "512")
)

# Faithfulness evaluation — the LLM-as-judge (M5, #22). Pluggable on the same seam as the answer
# LLM and the embedder: a deterministic fake under pytest so the suite never touches a network, the
# Anthropic Messages API when a key is configured, an Ollama fallback otherwise.
#
# The judge model is configured SEPARATELY from the answer model on purpose. Scoring an answer with
# the model that wrote it is self-assessment, which is known-optimistic — so the two are at least
# independently settable, and the report states whenever they resolved to the same model anyway
# (which they do by default in local development, where both are Ollama).
TENANTIQ_EVAL_JUDGE_MODEL = os.environ.get("TENANTIQ_EVAL_JUDGE_MODEL", "claude-opus-4-8")
TENANTIQ_EVAL_JUDGE_OLLAMA_MODEL = os.environ.get("TENANTIQ_EVAL_JUDGE_OLLAMA_MODEL", "llama3.1")
TENANTIQ_EVAL_JUDGE_TIMEOUT = int(os.environ.get("TENANTIQ_EVAL_JUDGE_TIMEOUT", "180"))
# The judge prompt carries up to TOP_K verbatim chunks plus the claims, so it overflows Ollama's
# 4096-token default for the same reason the answer prompt does (#90, above). Sized to fit; lower it
# on a memory-constrained machine, at the cost of losing the questions that retrieved most evidence.
TENANTIQ_EVAL_JUDGE_NUM_CTX = int(os.environ.get("TENANTIQ_EVAL_JUDGE_NUM_CTX", "8192"))
TENANTIQ_EVAL_JUDGE_FACTORY = os.environ.get(
    "TENANTIQ_EVAL_JUDGE_FACTORY",
    (
        "app.eval.judge.build_fake_judge"
        if "pytest" in sys.modules
        else "app.eval.judge.build_default_judge"
    ),
)

TENANTIQ_LLM_FACTORY = os.environ.get(
    "TENANTIQ_LLM_FACTORY",
    (
        "app.generation.build_fake_llm"
        if "pytest" in sys.modules
        else "app.generation.build_default_llm"
    ),
)

# Per-model price overrides for cost accounting (#17, ADR-0012), keyed by the model name the *serving*
# client reports (app.generation clients expose `.model`). A deployment with no Anthropic key answers
# from the local Ollama model, which carries no per-token cost — billing it at the Anthropic default
# would report spend that never happened — so local/fake models are priced at zero here. A model with
# no entry falls back to the global TENANTIQ_LLM_PRICE_* pair above.
TENANTIQ_LLM_PRICES: dict[str, dict[str, str]] = {
    TENANTIQ_LLM_OLLAMA_MODEL: {"input": "0", "output": "0"},
    "fake-llm-v1": {"input": "0", "output": "0"},  # the hermetic test / offline client
}

# Structured logging (#26, ADR-0024). Every line carries the request it belongs to and the tenant it
# ran for (app.log_context), so one request can be followed from the API through retrieval and the
# model call, and into the ingestion task it queued. JSON, one object per line, wherever DEBUG is off,
# for a collector to parse; under DEBUG, which is how compose runs, a readable line that prints the
# same fields. Lines go to stderr, as gunicorn's and Celery's own do, so a command's report on
# stdout stays clean. The Celery worker and beat log through this same configuration
# (config/celery.py) rather than Celery's own.
TENANTIQ_LOG_FORMAT = os.environ.get("TENANTIQ_LOG_FORMAT") or ("text" if DEBUG else "json")
if TENANTIQ_LOG_FORMAT not in ("json", "text"):
    raise ImproperlyConfigured(
        f"TENANTIQ_LOG_FORMAT must be 'json' or 'text', not {TENANTIQ_LOG_FORMAT!r}."
    )
TENANTIQ_LOG_LEVEL = (os.environ.get("TENANTIQ_LOG_LEVEL") or "INFO").upper()
if TENANTIQ_LOG_LEVEL not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
    raise ImproperlyConfigured(
        "TENANTIQ_LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR or CRITICAL, "
        f"not {TENANTIQ_LOG_LEVEL!r}."
    )
# Django's loggers and the Anthropic SDK's stop at INFO. Below it, Django prints every file the
# autoreloader watches and, with DEBUG on, every SQL statement with its parameters, and the SDK prints
# each request whole, the prompt and its passages included (docs/threat-model.md, T15).
_LIBRARY_LOG_LEVEL = "INFO" if TENANTIQ_LOG_LEVEL == "DEBUG" else TENANTIQ_LOG_LEVEL
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {"log_context": {"()": "app.log_context.LogContextFilter"}},
    "formatters": {
        "json": {"()": "app.log_context.JsonFormatter"},
        "text": {"()": "app.log_context.TextFormatter"},
    },
    "handlers": {
        "stderr": {
            "()": "app.log_context.StderrHandler",
            "filters": ["log_context"],
            "formatter": TENANTIQ_LOG_FORMAT,
        },
    },
    "root": {"handlers": ["stderr"], "level": TENANTIQ_LOG_LEVEL},
    "loggers": {
        # Django's own lines (django.request's 4xx and 5xx among them) reach the root handler only,
        # rather than also printing through Django's default console handler under DEBUG.
        "django": {"handlers": [], "level": _LIBRARY_LOG_LEVEL, "propagate": True},
        "anthropic": {"level": _LIBRARY_LOG_LEVEL},
    },
}
