"""Celery tasks for async ingestion (#11).

Thin wrapper over :func:`app.ingestion.run_ingestion`. ``ParseError`` is handled inside as a
permanent failure (document → FAILED); any other exception propagates and is retried with backoff
as a transient failure.
"""

from __future__ import annotations

from celery import shared_task

from app.ingestion import run_ingestion


@shared_task(
    bind=True,
    ignore_result=True,  # the result is never read; don't touch the result backend
    autoretry_for=(Exception,),
    retry_backoff=True,
    max_retries=3,
)
def ingest_document(self, document_id, tenant_id) -> None:
    run_ingestion(document_id, tenant_id)
