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
    "DEFAULT_THROTTLE_RATES": {
        "query": os.environ.get("TENANTIQ_THROTTLE_QUERY", "30/min"),
        "upload": os.environ.get("TENANTIQ_THROTTLE_UPLOAD", "20/min"),
        "read": os.environ.get("TENANTIQ_THROTTLE_READ", "120/min"),
    },
}

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

# Embeddings + vector store (ADR-0004, #12). The embedder is pluggable like the token verifier: a
# deterministic, dependency-free hashing embedder under pytest (no network/secrets), Ollama otherwise.
# DIM is fixed because the pgvector column + index need a fixed width; changing the model means a
# migration plus a re-backfill (manage.py backfill_embeddings).
TENANTIQ_EMBEDDING_DIM = int(os.environ.get("TENANTIQ_EMBEDDING_DIM", "768"))
TENANTIQ_EMBEDDING_MODEL = os.environ.get("TENANTIQ_EMBEDDING_MODEL", "nomic-embed-text")
TENANTIQ_EMBED_BATCH_SIZE = int(os.environ.get("TENANTIQ_EMBED_BATCH_SIZE", "64"))
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
TENANTIQ_EMBEDDER_FACTORY = os.environ.get(
    "TENANTIQ_EMBEDDER_FACTORY",
    (
        "app.embeddings.build_fake_embedder"
        if "pytest" in sys.modules
        else "app.embeddings.build_default_embedder"
    ),
)

# RAG query engine — retrieval + prompt assembly (M3, #14). Tuned like the chunking knobs.
# TOP_K: how many tenant-scoped chunks to retrieve as candidate context.
# MIN_SIMILARITY: cosine similarity floor (1 - distance, in [-1, 1]); a candidate below it is
# dropped rather than padding the prompt, and if nothing clears the bar the query returns
# "no relevant context". Default 0.0 keeps anything at least orthogonal to the query; raise it once
# M5's eval calibrates the floor against the real embedding model.
TENANTIQ_RETRIEVAL_TOP_K = int(os.environ.get("TENANTIQ_RETRIEVAL_TOP_K", "5"))
TENANTIQ_RETRIEVAL_MIN_SIMILARITY = float(
    os.environ.get("TENANTIQ_RETRIEVAL_MIN_SIMILARITY", "0.0")
)

# Grounded answer generation (M3, #15, ADR-0008). The LLM client is pluggable like the embedder: a
# deterministic fake under pytest (no network/key), the Anthropic Messages API otherwise, with an
# Ollama fallback when no key is set. Anthropic is the answer LLM; Ollama's model is a local chat model.
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
TENANTIQ_LLM_MODEL = os.environ.get("TENANTIQ_LLM_MODEL", "claude-opus-4-8")
TENANTIQ_LLM_MAX_TOKENS = int(os.environ.get("TENANTIQ_LLM_MAX_TOKENS", "1024"))
TENANTIQ_LLM_OLLAMA_MODEL = os.environ.get("TENANTIQ_LLM_OLLAMA_MODEL", "llama3.1")
TENANTIQ_LLM_TIMEOUT_SECONDS = int(os.environ.get("TENANTIQ_LLM_TIMEOUT_SECONDS", "60"))
TENANTIQ_LLM_FACTORY = os.environ.get(
    "TENANTIQ_LLM_FACTORY",
    (
        "app.generation.build_fake_llm"
        if "pytest" in sys.modules
        else "app.generation.build_default_llm"
    ),
)
