"""A document a dead worker left in PROCESSING is reaped, and nothing else is (#55, ADR-0023).

ADR-0005 left one hole: a worker killed between ingestion's phase 1 and ``on_failure`` leaves its
document PROCESSING forever, which its tenant sees as work in progress and the retry endpoint
refuses. The sweep fails such a document once it has been unchanged for longer than any attempt
can run, so it carries a reason and can be retried.

The tests age a document by writing ``updated_at`` with a queryset update, because ``auto_now``
overwrites any value assigned before a save.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.db import connection
from django.utils import timezone
from rest_framework.test import APIClient

from app.auth.tenancy import tenant_for_issuer
from app.auth.verifier import TenantTokenVerifier
from app.checks import check_the_sweep_cannot_reap_running_work
from app.ingestion import REAPED_MESSAGE, _reap_tenant, reap_stuck_documents
from app.models import Document, Tenant
from app.tasks import sweep_stuck_documents
from app.tenant_context import tenant_context
from config.celery import app as celery_app
from tests.conftest import TEST_CLIENT_ID, TEST_ISSUER

pytestmark = pytest.mark.django_db

requires_postgres = pytest.mark.skipif(
    connection.vendor != "postgresql", reason="row-level security is a Postgres-only backstop"
)

BACKEND = Path(__file__).resolve().parents[1]
REAP_AFTER = 1800
TEXT = b"Enough words here to make at least one chunk of text for the retry to ingest."


@pytest.fixture(autouse=True)
def _settings(settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path)
    # Pinned here so the ages below mean the same thing whatever the shipped default becomes.
    settings.TENANTIQ_INGEST_TIME_LIMIT = 300
    settings.TENANTIQ_INGEST_REAP_AFTER_SECONDS = REAP_AFTER


def _tenant(slug: str, *, issuer: str | None = None, client_id: str | None = None) -> Tenant:
    return Tenant.objects.create(
        slug=slug,
        name=slug.title(),
        oidc_issuer=issuer or f"https://keycloak.test/realms/{slug}",
        oidc_client_id=client_id or slug,
    )


def _document(tenant: Tenant, status: str, *, age: int, error: str = "") -> Document:
    """A document in ``status`` whose last change was ``age`` seconds ago."""
    with tenant_context(tenant):
        document = Document.objects.create(
            title="notes.txt",
            content_type="text/plain",
            original_filename="notes.txt",
            size_bytes=len(TEXT),
            file=ContentFile(TEXT, name="notes.txt"),
            status=status,
            error=error,
            attempts=0 if status == Document.Status.PENDING else 1,
        )
        Document.objects.filter(pk=document.pk).update(
            updated_at=timezone.now() - timedelta(seconds=age)
        )
        document.refresh_from_db()
    return document


def _reload(tenant: Tenant, document: Document) -> Document:
    with tenant_context(tenant):
        document.refresh_from_db()
    return document


# --- what is reaped, and what is not ------------------------------------------------------------


def test_a_document_processing_for_longer_than_the_threshold_is_failed_with_a_reason():
    acme = _tenant("acme")
    stuck = _document(acme, Document.Status.PROCESSING, age=REAP_AFTER + 60)

    assert reap_stuck_documents() == 1

    stuck = _reload(acme, stuck)
    assert stuck.status == Document.Status.FAILED
    assert stuck.error == REAPED_MESSAGE
    # The reap is a state change like any other, and it is not an attempt.
    assert timezone.now() - stuck.updated_at < timedelta(seconds=60)
    assert stuck.attempts == 1


def test_a_document_inside_the_threshold_is_left_processing():
    """It may be mid-attempt, or waiting for its retry; either way something will still finish it."""
    acme = _tenant("acme")
    working = _document(acme, Document.Status.PROCESSING, age=REAP_AFTER - 60)

    assert reap_stuck_documents() == 0
    assert _reload(acme, working).status == Document.Status.PROCESSING


@pytest.mark.parametrize(
    "status", [Document.Status.PENDING, Document.Status.READY, Document.Status.FAILED]
)
def test_only_a_processing_document_is_reaped(status):
    acme = _tenant("acme")
    document = _document(acme, status, age=REAP_AFTER * 10, error="an earlier reason")

    assert reap_stuck_documents() == 0
    document = _reload(acme, document)
    assert document.status == status
    assert document.error == "an earlier reason"


def test_every_tenant_is_swept():
    acme, globex = _tenant("acme"), _tenant("globex")
    a_doc = _document(acme, Document.Status.PROCESSING, age=REAP_AFTER + 60)
    b_doc = _document(globex, Document.Status.PROCESSING, age=REAP_AFTER + 60)

    assert reap_stuck_documents() == 2
    assert _reload(acme, a_doc).status == Document.Status.FAILED
    assert _reload(globex, b_doc).status == Document.Status.FAILED


def test_each_reaped_document_is_logged_with_its_tenant(caplog):
    acme = _tenant("acme")
    stuck = _document(acme, Document.Status.PROCESSING, age=REAP_AFTER + 60)

    with caplog.at_level(logging.WARNING, logger="app.ingestion"):
        reap_stuck_documents()

    (record,) = [r for r in caplog.records if r.getMessage().startswith("Reaped document")]
    assert f"document {stuck.id} (tenant {acme.id})" in record.getMessage()


# --- tenant isolation ---------------------------------------------------------------------------


def test_one_tenants_sweep_leaves_another_tenants_document_alone():
    acme, globex = _tenant("acme"), _tenant("globex")
    a_doc = _document(acme, Document.Status.PROCESSING, age=REAP_AFTER + 60)
    b_doc = _document(globex, Document.Status.PROCESSING, age=REAP_AFTER + 60)
    now = timezone.now()

    assert _reap_tenant(acme, cutoff=now - timedelta(seconds=REAP_AFTER), now=now) == 1

    assert _reload(acme, a_doc).status == Document.Status.FAILED
    assert _reload(globex, b_doc).status == Document.Status.PROCESSING


@requires_postgres
def test_row_level_security_alone_keeps_a_sweep_inside_its_tenant(monkeypatch):
    """The sweep with its ORM scope removed, as if someone had written it against the unscoped
    manager: inside acme's transaction the database still shows it only acme's rows."""
    acme, globex = _tenant("acme"), _tenant("globex")
    a_doc = _document(acme, Document.Status.PROCESSING, age=REAP_AFTER + 60)
    b_doc = _document(globex, Document.Status.PROCESSING, age=REAP_AFTER + 60)
    now = timezone.now()

    monkeypatch.setattr(Document, "objects", Document.all_objects)
    assert _reap_tenant(acme, cutoff=now - timedelta(seconds=REAP_AFTER), now=now) == 1
    monkeypatch.undo()

    assert _reload(acme, a_doc).status == Document.Status.FAILED
    assert _reload(globex, b_doc).status == Document.Status.PROCESSING


# --- a reaped document is retryable -------------------------------------------------------------


def test_a_reaped_document_can_be_retried_through_the_api(
    settings, rsa_keys, mint_token, django_capture_on_commit_callbacks
):
    _, public_pem = rsa_keys
    settings.TENANTIQ_TOKEN_VERIFIER_FACTORY = lambda: TenantTokenVerifier(
        key_resolver=lambda token, tenant: public_pem, tenant_lookup=tenant_for_issuer
    )
    acme = _tenant("acme", issuer=TEST_ISSUER, client_id=TEST_CLIENT_ID)
    stuck = _document(acme, Document.Status.PROCESSING, age=REAP_AFTER + 60)
    api = APIClient()
    token = {"HTTP_AUTHORIZATION": f"Bearer {mint_token(sub='alice')}"}

    # Before the sweep, the retry endpoint refuses it: only a FAILED document can be retried.
    assert api.post(f"/api/documents/{stuck.id}/retry", **token).status_code == 409

    reap_stuck_documents()
    with django_capture_on_commit_callbacks(execute=True):
        response = api.post(f"/api/documents/{stuck.id}/retry", **token)

    assert response.status_code == 200, response.content
    assert response.json()["error"] == ""
    stuck = _reload(acme, stuck)
    assert stuck.status == Document.Status.READY
    assert stuck.attempts == 2
    with tenant_context(acme):
        assert stuck.chunks.count() >= 1


# --- the schedule -------------------------------------------------------------------------------


def test_beat_sends_the_sweep_at_the_configured_interval(settings):
    entry = celery_app.conf.beat_schedule["sweep-stuck-documents"]

    assert entry["task"] == sweep_stuck_documents.name == "app.tasks.sweep_stuck_documents"
    assert entry["schedule"] == settings.TENANTIQ_INGEST_SWEEP_INTERVAL_SECONDS


def test_the_scheduled_task_reaps():
    acme = _tenant("acme")
    stuck = _document(acme, Document.Status.PROCESSING, age=REAP_AFTER + 60)

    assert sweep_stuck_documents.apply().get() == 1
    assert _reload(acme, stuck).status == Document.Status.FAILED


# --- the configuration check --------------------------------------------------------------------


def test_a_threshold_under_twice_the_time_limit_is_refused(settings):
    settings.TENANTIQ_INGEST_REAP_AFTER_SECONDS = 599

    errors = check_the_sweep_cannot_reap_running_work(None)

    assert [error.id for error in errors] == ["tenantiq.E001"]
    assert "at least 600" in errors[0].hint


def test_twice_the_time_limit_is_enough(settings):
    settings.TENANTIQ_INGEST_REAP_AFTER_SECONDS = 600

    assert check_the_sweep_cannot_reap_running_work(None) == []


@pytest.mark.parametrize("interval", [0, -300])
def test_the_sweep_interval_must_be_positive(settings, interval):
    settings.TENANTIQ_INGEST_SWEEP_INTERVAL_SECONDS = interval

    assert [error.id for error in check_the_sweep_cannot_reap_running_work(None)] == [
        "tenantiq.E002"
    ]


def test_the_settings_as_loaded_pass(settings):
    import config.settings as loaded

    settings.TENANTIQ_INGEST_TIME_LIMIT = loaded.TENANTIQ_INGEST_TIME_LIMIT
    settings.TENANTIQ_INGEST_REAP_AFTER_SECONDS = loaded.TENANTIQ_INGEST_REAP_AFTER_SECONDS

    call_command("check")


def test_a_release_stops_at_migrate_when_the_threshold_is_unsafe():
    """`manage.py migrate` is the first thing a release runs (the migrate Job), and from the command
    line it runs the system checks before it touches the database. `call_command` skips them by
    default, so this runs the real command, with the value arriving the way a deployment sets it.
    `--check` keeps it from applying anything should the check ever stop firing."""
    result = subprocess.run(
        [sys.executable, "manage.py", "migrate", "--check", "--noinput"],
        cwd=BACKEND,
        env={**os.environ, "TENANTIQ_INGEST_REAP_AFTER_SECONDS": "60"},
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode != 0
    assert "tenantiq.E001" in result.stderr, result.stderr
