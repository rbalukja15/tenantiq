"""TDD for the ingestion pipeline core (app.ingestion.run_ingestion) — issue #11.

Calls the plain function directly (no Celery) so the logic is tested synchronously: a parseable
document becomes READY with ordered, tenant-scoped chunks; a bad/empty file becomes FAILED.
"""

from __future__ import annotations

import pytest
from django.core.files.base import ContentFile

from app.ingestion import run_ingestion
from app.models import Chunk, Document, Tenant
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


def _doc(tenant, *, body, content_type="text/plain", name="notes.txt"):
    with tenant_context(tenant):
        return Document.objects.create(
            title=name,
            content_type=content_type,
            original_filename=name,
            size_bytes=len(body),
            file=ContentFile(body, name=name),
        )


def test_ingestion_produces_ready_tenant_scoped_chunks():
    a = _tenant("acme")
    text = ("Paragraph one has several words. " * 20) + "\n\n" + ("Paragraph two too. " * 20)
    doc = _doc(a, body=text.encode())

    run_ingestion(doc.id, a.id)

    with tenant_context(a):
        doc.refresh_from_db()
        assert doc.status == Document.Status.READY
        chunks = list(Chunk.objects.filter(document=doc).order_by("index"))
        assert len(chunks) >= 1
        assert [c.index for c in chunks] == list(range(len(chunks)))
        assert all(c.tenant_id == a.id for c in chunks)
        assert all(c.text for c in chunks)


def test_ingestion_marks_failed_on_unparseable_file():
    a = _tenant("acme")
    doc = _doc(a, body=b"%PDF not a real pdf", content_type="application/pdf", name="bad.pdf")

    run_ingestion(doc.id, a.id)

    with tenant_context(a):
        doc.refresh_from_db()
        assert doc.status == Document.Status.FAILED
        assert Chunk.objects.filter(document=doc).count() == 0


def test_ingestion_marks_failed_on_empty_text():
    a = _tenant("acme")
    doc = _doc(a, body=b"   \n\n  ")

    run_ingestion(doc.id, a.id)

    with tenant_context(a):
        doc.refresh_from_db()
        assert doc.status == Document.Status.FAILED


def test_chunks_are_tenant_scoped():
    a, b = _tenant("acme"), _tenant("globex")
    doc_a = _doc(a, body=b"Acme content here, plenty enough to make at least one chunk of text.")

    run_ingestion(doc_a.id, a.id)

    with tenant_context(b):
        assert Chunk.objects.count() == 0
    with tenant_context(a):
        assert Chunk.objects.count() >= 1
