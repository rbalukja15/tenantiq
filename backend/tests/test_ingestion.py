"""TDD for the ingestion pipeline core (app.ingestion.run_ingestion) — issue #11.

Calls the plain function directly (no Celery) so the logic is tested synchronously: a parseable
document becomes READY with ordered, tenant-scoped chunks; a bad/empty file becomes FAILED.
"""

from __future__ import annotations

import pytest
from django.conf import settings as django_settings
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


def test_ingestion_embeds_every_chunk():
    # READY must mean "chunked AND embedded" — every chunk carries a fixed-dim vector + its source.
    a = _tenant("acme")
    text = ("Paragraph one has several words. " * 20) + "\n\n" + ("Paragraph two too. " * 20)
    doc = _doc(a, body=text.encode())

    run_ingestion(doc.id, a.id)

    with tenant_context(a):
        chunks = list(Chunk.objects.filter(document=doc))
        assert chunks
        for chunk in chunks:
            assert chunk.embedding is not None
            assert len(list(chunk.embedding)) == django_settings.TENANTIQ_EMBEDDING_DIM
            assert chunk.embedding_model  # records which model produced the vector


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


# --- Observability: status, attempts, and surfaced failure reason (issue #13) ---


def test_new_document_has_observability_defaults():
    a = _tenant("acme")
    with tenant_context(a):
        doc = Document.objects.create(title="fresh")
    assert doc.error == ""
    assert doc.attempts == 0
    assert doc.updated_at is not None


def test_successful_ingestion_records_one_attempt_and_no_error():
    a = _tenant("acme")
    doc = _doc(a, body=b"Enough words here to make at least a single chunk of text.")

    run_ingestion(doc.id, a.id)

    with tenant_context(a):
        doc.refresh_from_db()
        assert doc.status == Document.Status.READY
        assert doc.attempts == 1
        assert doc.error == ""


def test_parse_failure_records_reason_and_attempt():
    a = _tenant("acme")
    doc = _doc(a, body=b"%PDF not a real pdf", content_type="application/pdf", name="bad.pdf")

    run_ingestion(doc.id, a.id)

    with tenant_context(a):
        doc.refresh_from_db()
        assert doc.status == Document.Status.FAILED
        assert doc.attempts == 1
        assert doc.error  # a human-readable reason is surfaced, not just the status


def test_transient_failure_propagates_and_leaves_processing(monkeypatch):
    # An embedding-backend outage is *transient*: run_ingestion must not swallow it (so the Celery
    # task can retry) and must not mark the document permanently FAILED. It stays PROCESSING, with
    # no chunks written. Marking a terminal failure is the task's job (after retries are exhausted).
    a = _tenant("acme")
    doc = _doc(a, body=b"Enough words here to make at least a single chunk of text.")

    def _boom(*args, **kwargs):
        raise RuntimeError("embedding backend unreachable")

    monkeypatch.setattr("app.ingestion.embed_in_batches", _boom)

    with pytest.raises(RuntimeError):
        run_ingestion(doc.id, a.id)

    with tenant_context(a):
        doc.refresh_from_db()
        assert doc.status == Document.Status.PROCESSING
        assert doc.attempts == 1
        assert Chunk.objects.filter(document=doc).count() == 0


def test_mark_ingestion_failed_records_terminal_error():
    from app.ingestion import mark_ingestion_failed

    a = _tenant("acme")
    doc = _doc(a, body=b"whatever")
    with tenant_context(a):
        doc.status = Document.Status.PROCESSING
        doc.save(update_fields=["status"])

    mark_ingestion_failed(doc.id, a.id, "embedding backend unreachable after 3 retries")

    with tenant_context(a):
        doc.refresh_from_db()
        assert doc.status == Document.Status.FAILED
        assert "unreachable" in doc.error


def test_mark_ingestion_failed_never_overwrites_a_ready_document():
    # A late or duplicate failing task must not stomp a document that already succeeded — otherwise
    # a concurrent retry could flip a READY document to FAILED.
    from app.ingestion import mark_ingestion_failed

    a = _tenant("acme")
    doc = _doc(a, body=b"whatever")
    with tenant_context(a):
        doc.status = Document.Status.READY
        doc.save(update_fields=["status"])

    mark_ingestion_failed(doc.id, a.id, "stale failure from a duplicate task")

    with tenant_context(a):
        doc.refresh_from_db()
        assert doc.status == Document.Status.READY
        assert doc.error == ""


def test_reingestion_is_idempotent():
    # A retry re-runs ingestion on a document that may already carry chunks; it must not trip the
    # unique (document, index) constraint, and must leave a single, consistent set of chunks.
    a = _tenant("acme")
    doc = _doc(a, body=b"Enough words here to make at least a single chunk of text.")

    run_ingestion(doc.id, a.id)
    with tenant_context(a):
        first_count = Chunk.objects.filter(document=doc).count()
    assert first_count >= 1

    run_ingestion(doc.id, a.id)  # e.g. via the retry endpoint

    with tenant_context(a):
        doc.refresh_from_db()
        assert doc.status == Document.Status.READY
        assert Chunk.objects.filter(document=doc).count() == first_count
