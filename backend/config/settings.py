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

BASE_DIR = Path(__file__).resolve().parent.parent


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
}

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

# Chunking strategy (ADR-0003). Tunable; sized by a chars-per-token estimate until #12's tokenizer.
TENANTIQ_CHUNK_TARGET_TOKENS = int(os.environ.get("TENANTIQ_CHUNK_TARGET_TOKENS", "800"))
TENANTIQ_CHUNK_OVERLAP_TOKENS = int(os.environ.get("TENANTIQ_CHUNK_OVERLAP_TOKENS", "100"))
