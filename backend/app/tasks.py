"""Celery tasks for async ingestion (#11, observability #13).

Thin wrapper over :func:`app.ingestion.run_ingestion`. ``ParseError`` is handled inside as a
permanent failure (document → FAILED); any other exception propagates and is retried with backoff
as a transient failure. When those retries are exhausted, :class:`IngestTask.on_failure` records
the terminal failure on the document so it never sits silently stuck in PROCESSING.
"""

from __future__ import annotations

from celery import Task, shared_task
from celery.exceptions import SoftTimeLimitExceeded
from django.conf import settings

from app.ingestion import mark_ingestion_failed, run_ingestion


class IngestTask(Task):
    """Base task that surfaces a *terminal* ingestion failure on the document.

    ``on_failure`` fires only once the task has given up (retries exhausted or a non-retryable
    error) — never on an intermediate retry — so a transient outage that eventually fails for good
    becomes an observable FAILED document with a reason instead of one wedged in PROCESSING. It
    passes the exception (not its raw text) so the reason is sanitized before it reaches the tenant.
    """

    def on_failure(self, exc, task_id, args, kwargs, einfo) -> None:
        document_id, tenant_id = args[0], args[1]
        mark_ingestion_failed(document_id, tenant_id, exc)


@shared_task(
    base=IngestTask,
    bind=True,
    ignore_result=True,  # the result is never read; don't touch the result backend
    autoretry_for=(Exception,),
    retry_backoff=True,
    max_retries=3,
    # Bound the work so a crafted upload can't monopolize the shared worker (#47). The soft limit
    # raises SoftTimeLimitExceeded *inside* the task (handled below as permanent); the hard limit
    # SIGKILLs a task that ignores the soft raise.
    soft_time_limit=settings.TENANTIQ_INGEST_SOFT_TIME_LIMIT,
    time_limit=settings.TENANTIQ_INGEST_TIME_LIMIT,
)
def ingest_document(self, document_id, tenant_id) -> None:
    try:
        run_ingestion(document_id, tenant_id)
    except SoftTimeLimitExceeded as exc:
        # A soft-limit hit is a *permanent* failure — retrying (autoretry_for) would just burn the
        # worker again and amplify the cost 4x. Record it terminally instead of re-raising, so the
        # task does not retry. (run_ingestion also handles a soft-limit hit inside its own work; this
        # covers a hit in the thin window outside that block.)
        mark_ingestion_failed(document_id, tenant_id, exc)
