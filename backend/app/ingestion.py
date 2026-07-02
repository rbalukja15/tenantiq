"""Document ingestion: parse an uploaded file, chunk it, and persist tenant-scoped chunks (#11).

The work lives in :func:`run_ingestion`, a plain function so it can be tested synchronously; the
Celery task in ``app.tasks`` is a thin wrapper. The task has no request, so — per ADR-0002 — it
sets the tenant explicitly via the tenant id passed at enqueue time.

A ``ParseError`` (unreadable / empty file) is a *permanent* failure → the document is marked
FAILED. Any other exception propagates so Celery can retry a *transient* failure.
"""

from __future__ import annotations

import logging

from django.conf import settings

from app.chunking import chunk_text
from app.embeddings import embed_in_batches, get_embedder
from app.models import Chunk, Document, Tenant
from app.parsing import ParseError, extract_text
from app.tenant_context import tenant_context

logger = logging.getLogger(__name__)


def run_ingestion(document_id: int, tenant_id) -> None:
    tenant = Tenant.objects.get(id=tenant_id)  # Tenant is not tenant-owned; readable un-scoped
    with tenant_context(tenant):
        doc = Document.objects.get(id=document_id)
        doc.status = Document.Status.PROCESSING
        doc.save(update_fields=["status"])
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
        except ParseError:
            logger.warning("Ingestion failed for document %s (tenant %s)", document_id, tenant_id)
            doc.status = Document.Status.FAILED
            doc.save(update_fields=["status"])
            return

        # Embedding happens outside the ParseError guard: a parse failure is permanent (FAILED),
        # but an embedding failure (e.g. Ollama unreachable) is transient — let it propagate so the
        # Celery task retries with backoff rather than marking the document permanently FAILED.
        embedder = get_embedder()
        vectors = embed_in_batches(
            embedder,
            [piece["text"] for piece in pieces],
            settings.TENANTIQ_EMBED_BATCH_SIZE,
        )
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
                for piece, vector in zip(pieces, vectors)
            ]
        )
        doc.status = Document.Status.READY
        doc.save(update_fields=["status"])
