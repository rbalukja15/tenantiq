"""TDD for the reingest_documents backfill command (#16).

Redaction runs at ingest, so documents ingested *before* #16 still carry raw PII in their stored
chunks. Re-ingestion is the backfill: it re-reads each document's file, redacts, re-chunks and
re-embeds — reusing the idempotent run_ingestion path — so no chunk is left holding PII and no
embedding goes stale. The command is tenant-scoped, so a backfill for one tenant never touches
another's data.
"""

from __future__ import annotations

import pytest
from django.core.files.base import ContentFile
from django.core.management import call_command

from app.ingestion import run_ingestion
from app.models import Chunk, Document, Tenant
from app.tenant_context import tenant_context

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _isolated_media(settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path)


def _tenant(slug: str) -> Tenant:
    return Tenant.objects.create(
        slug=slug,
        name=slug,
        oidc_issuer=f"https://keycloak.test/realms/{slug}",
        oidc_client_id=slug,
    )


def _doc(tenant: Tenant, *, body: bytes, name: str = "notes.txt") -> Document:
    with tenant_context(tenant):
        return Document.objects.create(
            title=name,
            content_type="text/plain",
            original_filename=name,
            size_bytes=len(body),
            file=ContentFile(body, name=name),
        )


def _chunk_text(tenant: Tenant, doc: Document) -> str:
    with tenant_context(tenant):
        return " ".join(Chunk.objects.filter(document=doc).values_list("text", flat=True))


def test_reingest_redacts_pii_in_documents_ingested_before_redaction(settings):
    a = _tenant("acme")
    body = ("Contact leak@example.com for the quarterly report. " * 30).encode()
    doc = _doc(a, body=body)

    settings.TENANTIQ_REDACT_PII = False  # simulate a pre-#16 ingest: raw PII stored
    run_ingestion(doc.id, a.id)
    assert "leak@example.com" in _chunk_text(a, doc)

    settings.TENANTIQ_REDACT_PII = True
    call_command("reingest_documents", "--tenant", "acme")

    after = _chunk_text(a, doc)
    assert "leak@example.com" not in after
    assert "[REDACTED_EMAIL]" in after
    with tenant_context(a):
        doc.refresh_from_db()
        assert doc.status == Document.Status.READY


def test_reingest_is_scoped_to_the_named_tenant(settings):
    # Isolation is sacred: a backfill scoped to one tenant must never re-ingest another's documents.
    a, b = _tenant("acme"), _tenant("globex")
    body = ("Reach me at raw@example.com about the deal. " * 30).encode()
    doc_a, doc_b = _doc(a, body=body), _doc(b, body=body)

    settings.TENANTIQ_REDACT_PII = False  # seed both tenants with raw PII (a pre-#16 ingest)
    run_ingestion(doc_a.id, a.id)
    run_ingestion(doc_b.id, b.id)

    settings.TENANTIQ_REDACT_PII = True
    call_command("reingest_documents", "--tenant", "acme")

    assert "raw@example.com" not in _chunk_text(a, doc_a)  # acme redacted
    assert "raw@example.com" in _chunk_text(b, doc_b)  # globex untouched


def test_reingest_backfills_a_non_ready_document_that_still_has_chunks(settings):
    # A failed/interrupted re-ingest can leave a non-READY document holding stale (possibly PII)
    # chunks. The default backfill targets documents that HAVE chunks, so those aren't stranded.
    a = _tenant("acme")
    body = ("Contact leak@example.com about the report. " * 30).encode()
    doc = _doc(a, body=body)

    settings.TENANTIQ_REDACT_PII = False
    run_ingestion(doc.id, a.id)  # READY with raw-PII chunks
    with tenant_context(a):
        doc.refresh_from_db()
        doc.status = Document.Status.FAILED  # simulate a doc left non-READY with stale chunks
        doc.save(update_fields=["status"])

    settings.TENANTIQ_REDACT_PII = True
    call_command("reingest_documents", "--tenant", "acme")

    assert "leak@example.com" not in _chunk_text(a, doc)
    assert "[REDACTED_EMAIL]" in _chunk_text(a, doc)
