"""Document ingestion: parse an uploaded file, chunk it, and persist tenant-scoped chunks (#11).

The work lives in :func:`run_ingestion`, a plain function so it can be tested synchronously; the
Celery task in ``app.tasks`` is a thin wrapper. The task has no request, so — per ADR-0002 — it
sets the tenant explicitly via the tenant id passed at enqueue time.

A ``ParseError`` (unreadable / empty file) is a *permanent* failure → the document is marked
FAILED. Any other exception propagates so Celery can retry a *transient* failure.

Observability (#13): the attempt is recorded in its *own* transaction before the risky work, so a
transient failure — which rolls back the work (``tenant_context`` is atomic on Postgres) — still
leaves an accurate ``attempts`` count and a visible PROCESSING status. Terminal transient failures
are recorded by the task's ``on_failure`` via :func:`mark_ingestion_failed`.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.utils import timezone

from app.chunking import chunk_text
from app.embeddings import EmbeddingDimensionError, embed_in_batches, get_embedder
from app.models import Chunk, Document, Tenant
from app.parsing import ParseError, extract_text
from app.tenant_context import tenant_context

logger = logging.getLogger(__name__)

# Cap the surfaced reason so a stray traceback or huge parser message can't bloat the row.
MAX_ERROR_LENGTH = 2000


def run_ingestion(document_id: int, tenant_id) -> None:
    tenant = Tenant.objects.get(id=tenant_id)  # Tenant is not tenant-owned; readable un-scoped

    # Phase 1 — record the attempt in its own transaction. Entering PROCESSING counts as an
    # attempt and clears any stale reason, so ``attempts`` reflects retries and ``error`` only ever
    # describes the latest outcome. Committing this *before* the risky work means a transient
    # failure below (which rolls the work back) can't erase the fact that we tried.
    with tenant_context(tenant):
        doc = Document.objects.get(id=document_id)
        doc.status = Document.Status.PROCESSING
        doc.attempts += 1
        doc.error = ""
        doc.save(update_fields=["status", "attempts", "error", "updated_at"])

    # Phase 2 — the risky work, atomically (all chunks + READY, or nothing). A ParseError is a
    # *permanent* failure (record it, no retry); any other error propagates so the task retries a
    # *transient* failure, rolling back any partial chunk writes with it.
    with tenant_context(tenant):
        doc = Document.objects.get(id=document_id)
        try:
            with doc.file.open("rb") as handle:
                text = extract_text(handle, doc.content_type)
            pieces = chunk_text(
                text,
                target_tokens=settings.TENANTIQ_CHUNK_TARGET_TOKENS,
                overlap_tokens=settings.TENANTIQ_CHUNK_OVERLAP_TOKENS,
            )
            if not pieces:
                raise ParseError("No extractable text.")

            embedder = get_embedder()
            # embed_in_batches validates the backend's response (#46): a wrong dimension raises
            # EmbeddingDimensionError (caught here as permanent), a short count raises
            # EmbeddingCountError, which — like any transient backend error — propagates to retry.
            vectors = embed_in_batches(
                embedder,
                [piece["text"] for piece in pieces],
                settings.TENANTIQ_EMBED_BATCH_SIZE,
            )
        except (ParseError, EmbeddingDimensionError) as exc:
            # Permanent failures: an unreadable/empty file, or a static mis-configuration (a model
            # whose vectors don't match TENANTIQ_EMBEDDING_DIM). Record the reason and stop — a
            # retry would only hit the same error, so we don't burn the task's backoff on it.
            logger.warning(
                "Ingestion failed for document %s (tenant %s): %s", document_id, tenant_id, exc
            )
            doc.status = Document.Status.FAILED
            doc.error = str(exc)[:MAX_ERROR_LENGTH]
            doc.save(update_fields=["status", "error", "updated_at"])
            return

        # Re-ingestion (a retry) must be idempotent: drop any chunks from a prior run before writing
        # the new set, so the unique (document, index) constraint can't trip. This runs in phase 2's
        # transaction, so the delete + re-create are atomic — a failure restores the old chunks.
        Chunk.objects.filter(document=doc).delete()
        Chunk.objects.bulk_create(
            [
                Chunk(
                    tenant=doc.tenant,  # bulk_create skips save(); set the tenant explicitly
                    document=doc,
                    index=piece["index"],
                    text=piece["text"],
                    char_count=piece["char_count"],
                    token_estimate=piece["token_estimate"],
                    embedding=vector,
                    embedding_model=embedder.model,
                )
                # strict=True is a belt-and-suspenders invariant backing embed_in_batches' count
                # check: one vector per chunk, never a silent zip truncation onto the shorter list.
                for piece, vector in zip(pieces, vectors, strict=True)
            ]
        )
        doc.status = Document.Status.READY
        doc.save(update_fields=["status", "updated_at"])


def mark_ingestion_failed(document_id: int, tenant_id, error: str) -> None:
    """Record a *terminal* ingestion failure so it is observable (issue #13).

    Called from the task's ``on_failure`` hook once retries are exhausted, so a transient outage
    that never recovers becomes a FAILED document with a reason rather than one wedged in
    PROCESSING forever. Runs inside the tenant's context because ``Document`` is tenant-owned.

    A conditional update (never touching a READY document) keeps a late or duplicate failing task
    from stomping a document that has since succeeded; it also no-ops harmlessly if the row is gone.
    """
    tenant = Tenant.objects.get(id=tenant_id)
    with tenant_context(tenant):
        Document.objects.filter(id=document_id).exclude(status=Document.Status.READY).update(
            status=Document.Status.FAILED,
            error=(error or "Ingestion failed.")[:MAX_ERROR_LENGTH],
            updated_at=timezone.now(),
        )
