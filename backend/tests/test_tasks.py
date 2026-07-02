"""TDD for the ingestion task's retry + terminal-failure handling (issue #13).

Two guarantees:
- a *transient* failure is retried (not failed outright), and
- once the task gives up, its ``on_failure`` hook marks the document FAILED with a reason, so the
  failure is observable instead of a document wedged in PROCESSING.

Note on eager mode: under ``task_always_eager`` (the pytest default) an autoretry surfaces as
celery's ``Retry`` on the first attempt — the retry loop that eventually reaches ``on_failure``
only runs in a real worker. So we assert the retry via ``Retry`` and exercise ``on_failure``
directly, rather than trying to drive the loop to exhaustion in-process.
"""

from __future__ import annotations

import pytest
from celery.exceptions import Retry
from django.core.files.base import ContentFile

from app.models import Document, Tenant
from app.tasks import ingest_document
from app.tenant_context import tenant_context

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _isolated_media(settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path)


def _tenant(slug):
    return Tenant.objects.create(
        slug=slug,
        name=slug,
        oidc_issuer=f"https://keycloak.test/realms/{slug}",
        oidc_client_id=slug,
    )


def test_transient_failure_is_retried(monkeypatch):
    a = _tenant("acme")
    with tenant_context(a):
        doc = Document.objects.create(
            title="notes.txt",
            content_type="text/plain",
            file=ContentFile(b"Enough words here to make at least one chunk of text.", "notes.txt"),
        )

    def _boom(*args, **kwargs):
        raise RuntimeError("embedding backend unreachable")

    monkeypatch.setattr("app.ingestion.embed_in_batches", _boom)

    # A transient error triggers a retry (autoretry_for) rather than an immediate terminal failure.
    with pytest.raises(Retry):
        ingest_document.apply(args=[doc.id, a.id])


def test_on_failure_marks_document_failed_and_records_reason():
    # When the task finally gives up, on_failure turns a stuck PROCESSING document into an
    # observable FAILED one carrying the reason (this hook fires in the worker after retries).
    a = _tenant("acme")
    with tenant_context(a):
        doc = Document.objects.create(title="notes.txt", status=Document.Status.PROCESSING)

    exc = RuntimeError("embedding backend unreachable")
    ingest_document.on_failure(exc, "task-id-123", [doc.id, a.id], {}, None)

    with tenant_context(a):
        doc.refresh_from_db()
        assert doc.status == Document.Status.FAILED
        assert "unreachable" in doc.error
