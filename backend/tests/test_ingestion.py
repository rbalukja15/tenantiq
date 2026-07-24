"""TDD for the ingestion pipeline core (app.ingestion.run_ingestion) — issue #11.

Calls the plain function directly (no Celery) so the logic is tested synchronously: a parseable
document becomes READY with ordered, tenant-scoped chunks; a bad/empty file becomes FAILED.
"""

from __future__ import annotations

import pytest
from django.conf import settings as django_settings
from django.core.files.base import ContentFile

from app.chunking import chunk_text
from app.embeddings import EmbeddingCountError, HashingEmbedder
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


def test_ingestion_stores_offset_addressable_chunks():
    # #45: every stored chunk must be an exact slice of the extracted source, addressable by its
    # (start_offset, end_offset) — the anchor citations will resolve against.
    from app.parsing import extract_text

    a = _tenant("acme")
    body = _multi_chunk_body()
    _assert_multi_chunk(body)
    doc = _doc(a, body=body)

    run_ingestion(doc.id, a.id)

    with tenant_context(a):
        with doc.file.open("rb") as handle:
            source = extract_text(handle, doc.content_type)
        chunks = list(Chunk.objects.filter(document=doc).order_by("index"))
        assert len(chunks) > 1  # genuinely multi-chunk, so fidelity is under real pressure
        for chunk in chunks:
            assert chunk.text == source[chunk.start_offset : chunk.end_offset]  # verbatim slice
            assert chunk.end_offset > chunk.start_offset


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


# --- Embedding count / dimension validation: never READY with missing chunks (issue #46) ---


class _DropsTailEmbedder(HashingEmbedder):
    """Returns one fewer vector than chunks — the silent tail-drop #46 must refuse."""

    def embed_documents(self, texts):
        return super().embed_documents(texts)[:-1]


class _WrongDimEmbedder(HashingEmbedder):
    """Returns vectors of the wrong width — a permanent model/dimension mis-configuration."""

    def embed_documents(self, texts):
        return [[0.0] * (self.dim + 1) for _ in texts]


def _multi_chunk_body() -> bytes:
    """A body large enough to split into several chunks, so tail-drop has a *tail* to drop.

    The headline #46 bug is a *multi-chunk* document losing its last chunk(s), so the regression
    tests must feed a genuinely multi-chunk document — a single-chunk doc can't exhibit it. Sized
    comfortably past the ~800-token (~3200-char) chunk target; asserted below so it can't silently
    regress to one chunk if chunk sizing changes.
    """
    paragraph = "Revenue for the quarter grew across every region and product line. " * 10
    return "\n\n".join(paragraph for _ in range(12)).encode()


def _assert_multi_chunk(body: bytes) -> None:
    pieces = chunk_text(
        body.decode(),
        target_tokens=django_settings.TENANTIQ_CHUNK_TARGET_TOKENS,
        overlap_tokens=django_settings.TENANTIQ_CHUNK_OVERLAP_TOKENS,
    )
    assert len(pieces) > 1, "test premise: body must produce multiple chunks to exercise tail-drop"


def test_ingestion_never_ready_when_backend_returns_too_few_vectors(monkeypatch):
    # A backend returning n-1 vectors used to drop the tail chunk of a multi-chunk document and
    # still mark it READY. A short count is treated as *transient* (it may be a truncated response),
    # so run_ingestion propagates for the task to retry — the document stays PROCESSING (never
    # READY, never permanently FAILED), with the attempt recorded and not a single partial chunk.
    a = _tenant("acme")
    body = _multi_chunk_body()
    _assert_multi_chunk(body)
    doc = _doc(a, body=body)
    monkeypatch.setattr(
        "app.ingestion.get_embedder",
        lambda: _DropsTailEmbedder(django_settings.TENANTIQ_EMBEDDING_DIM),
    )

    with pytest.raises(EmbeddingCountError):
        run_ingestion(doc.id, a.id)

    with tenant_context(a):
        doc.refresh_from_db()
        assert doc.status == Document.Status.PROCESSING  # transient: left for the task to retry
        assert doc.attempts == 1
        assert doc.error == ""  # not marked with a terminal reason yet
        assert Chunk.objects.filter(document=doc).count() == 0  # no partial write


def test_ingestion_fails_permanently_on_wrong_dimension(monkeypatch):
    # A wrong-dim model is a *permanent* config error: fail the document immediately with a clear
    # reason instead of burning retry backoff on an error every attempt will hit. Use a multi-chunk
    # document so the atomic "all chunks or none" rollback is genuinely exercised, not just 1 chunk.
    a = _tenant("acme")
    body = _multi_chunk_body()
    _assert_multi_chunk(body)
    doc = _doc(a, body=body)
    monkeypatch.setattr(
        "app.ingestion.get_embedder",
        lambda: _WrongDimEmbedder(django_settings.TENANTIQ_EMBEDDING_DIM),
    )

    run_ingestion(doc.id, a.id)  # permanent -> recorded, does not raise (no retry)

    with tenant_context(a):
        doc.refresh_from_db()
        assert doc.status == Document.Status.FAILED
        # The surfaced reason is sanitized (#47): it must NOT leak internal config like the embedding
        # dimension setting or the model name — that goes to the server log only.
        assert doc.error and "TENANTIQ_EMBEDDING_DIM" not in doc.error
        assert Chunk.objects.filter(document=doc).count() == 0


def test_ingestion_fails_permanently_on_soft_time_limit(monkeypatch):
    # A soft time-limit hit means the work outran its budget on the shared worker: fail permanently
    # with a safe message and NO retry amplification (#47) — run_ingestion returns without raising,
    # so Celery's autoretry_for never fires.
    from celery.exceptions import SoftTimeLimitExceeded

    a = _tenant("acme")
    doc = _doc(a, body=b"some text to ingest")

    def _timeout(*args, **kwargs):
        raise SoftTimeLimitExceeded()

    monkeypatch.setattr("app.ingestion.extract_text", _timeout)

    run_ingestion(doc.id, a.id)  # permanent -> recorded, does not raise

    with tenant_context(a):
        doc.refresh_from_db()
        assert doc.status == Document.Status.FAILED
        assert doc.attempts == 1  # not retried
        assert "too long" in doc.error.lower()  # the safe timeout message, not a raw traceback
        assert Chunk.objects.filter(document=doc).count() == 0


def test_mark_ingestion_failed_sanitizes_the_surfaced_reason(caplog):
    # A terminal transient failure must surface a *safe* message to the tenant — the raw exception
    # (which can carry hostnames, DSNs, internal paths) goes only to the server log (#47).
    from app.ingestion import mark_ingestion_failed

    a = _tenant("acme")
    doc = _doc(a, body=b"whatever")
    with tenant_context(a):
        doc.status = Document.Status.PROCESSING
        doc.save(update_fields=["status"])

    raw = (
        "OSError: connect to postgres://svc:s3cr3t@db.internal:5432 failed at /opt/app/worker.py:88"
    )
    with caplog.at_level("ERROR"):
        mark_ingestion_failed(doc.id, a.id, RuntimeError(raw))

    with tenant_context(a):
        doc.refresh_from_db()
        assert doc.status == Document.Status.FAILED
        assert doc.error == "The document could not be processed. Please try again later."
        # None of the raw internals leak into the tenant-facing reason...
        for secret in ("postgres://", "s3cr3t", "db.internal", "/opt/app", "OSError"):
            assert secret not in doc.error
    # ...but the raw exception IS logged server-side for operators.
    assert any("s3cr3t" in record.getMessage() for record in caplog.records)


def test_mark_ingestion_failed_never_overwrites_a_ready_document():
    # A late or duplicate failing task must not stomp a document that already succeeded — otherwise
    # a concurrent retry could flip a READY document to FAILED.
    from app.ingestion import mark_ingestion_failed

    a = _tenant("acme")
    doc = _doc(a, body=b"whatever")
    with tenant_context(a):
        doc.status = Document.Status.READY
        doc.save(update_fields=["status"])

    mark_ingestion_failed(doc.id, a.id, RuntimeError("stale failure from a duplicate task"))

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


# --- PII redaction on ingest (#16) ----------------------------------------------------------------


def test_ingestion_redacts_pii_before_storing_chunks():
    # Recognizable PII must never land in a stored chunk (and so never in the vector index or an
    # answer). Redaction runs on the extracted text before chunking.
    a = _tenant("acme")
    body = ("Contact Jane at jane.doe@example.com or call 415-555-0148. " * 40).encode()
    doc = _doc(a, body=body)

    run_ingestion(doc.id, a.id)

    with tenant_context(a):
        doc.refresh_from_db()
        assert doc.status == Document.Status.READY
        joined = " ".join(Chunk.objects.filter(document=doc).values_list("text", flat=True))

    assert "jane.doe@example.com" not in joined
    assert "415-555-0148" not in joined
    assert "[REDACTED_EMAIL]" in joined
    assert "[REDACTED_PHONE]" in joined


def test_redacted_chunk_text_is_still_a_faithful_slice():
    # Redaction runs before chunking, so #45 fidelity holds against the *redacted* source: each
    # chunk's stored text is exactly its char span of redact_pii(source). This is the real guard —
    # it would fail if redaction moved after chunking or offsets indexed the un-redacted text.
    from app.guardrails import redact_pii

    a = _tenant("acme")
    raw = "Email a@b.com then write several more words to fill the chunk. " * 30
    doc = _doc(a, body=raw.encode())
    redacted_source = redact_pii(raw)

    run_ingestion(doc.id, a.id)

    with tenant_context(a):
        chunks = list(Chunk.objects.filter(document=doc).order_by("index"))
        assert chunks
        for chunk in chunks:
            assert chunk.text == redacted_source[chunk.start_offset : chunk.end_offset]
        assert "a@b.com" not in " ".join(c.text for c in chunks)  # redaction actually happened


def test_pii_redaction_can_be_disabled_via_settings(settings):
    # An escape hatch for evaluation baselines (#21) that need the raw extracted text. Off by config.
    settings.TENANTIQ_REDACT_PII = False
    a = _tenant("acme")
    body = ("Reach me at raw.pii@example.com any time you like. " * 40).encode()
    doc = _doc(a, body=body)

    run_ingestion(doc.id, a.id)

    with tenant_context(a):
        joined = " ".join(Chunk.objects.filter(document=doc).values_list("text", flat=True))

    assert "raw.pii@example.com" in joined  # redaction disabled → raw text stored
    assert "[REDACTED_EMAIL]" not in joined


def test_ingestion_redacts_pii_split_across_a_page_join_newline():
    # pypdf joins pages with "\n"; an SSN straddling a page break (review #16) must not survive into
    # a stored chunk. Redaction runs on the whole extracted text before chunking, so it is caught.
    a = _tenant("acme")
    body = (("filler words to pad the document. " * 20) + "SSN 123-45-\n6789 tail.").encode()
    doc = _doc(a, body=body)

    run_ingestion(doc.id, a.id)

    with tenant_context(a):
        joined = " ".join(Chunk.objects.filter(document=doc).values_list("text", flat=True))
    assert "123-45-6789" not in joined
    assert "[REDACTED_SSN]" in joined
