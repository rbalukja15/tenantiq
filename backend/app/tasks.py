"""Celery tasks for async ingestion (#11, observability #13).

Thin wrapper over :func:`app.ingestion.run_ingestion`. ``ParseError`` is handled inside as a
permanent failure (document → FAILED); any other exception propagates and is retried with backoff
as a transient failure. When those retries are exhausted, :class:`IngestTask.on_failure` records
the terminal failure on the document so it never sits silently stuck in PROCESSING.
"""

from __future__ import annotations

from celery import Task, shared_task

from app.ingestion import mark_ingestion_failed, run_ingestion


class IngestTask(Task):
    """Base task that surfaces a *terminal* ingestion failure on the document.

    ``on_failure`` fires only once the task has given up (retries exhausted or a non-retryable
    error) — never on an intermediate retry — so a transient outage that eventually fails for good
    becomes an observable FAILED document with a reason instead of one wedged in PROCESSING.
    """

    def on_failure(self, exc, task_id, args, kwargs, einfo) -> None:
        document_id, tenant_id = args[0], args[1]
        mark_ingestion_failed(document_id, tenant_id, str(exc))


@shared_task(
    base=IngestTask,
    bind=True,
    ignore_result=True,  # the result is never read; don't touch the result backend
    autoretry_for=(Exception,),
    retry_backoff=True,
    max_retries=3,
)
def ingest_document(self, document_id, tenant_id) -> None:
    run_ingestion(document_id, tenant_id)
