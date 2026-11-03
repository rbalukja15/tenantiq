"""Celery application for async ingestion (M2).

Broker/result config comes from Django settings (the ``CELERY_*`` keys); tasks live in
``app.tasks`` and are auto-discovered. Tests/CI set ``task_always_eager`` so no live broker is
needed.

Logging (#26, ADR-0024): the worker and beat log through Django's ``LOGGING``, not Celery's own
handlers, and a task runs inside the log fields of the request that queued it, so the request id an
upload was logged with is on every line its ingestion logs. The receivers import ``app.log_context``
when they run rather than here, because this module is imported while Django is still loading its
settings.
"""

from __future__ import annotations

import os
from typing import Any

from celery import Celery
from celery.signals import before_task_publish, setup_logging, task_postrun, task_prerun

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("tenantiq")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()


@setup_logging.connect
def _log_through_django(**kwargs: Any) -> None:
    """Apply Django's ``LOGGING`` in place of Celery's own setup.

    Any receiver on this signal stops Celery from replacing the root logger's handlers with its own
    plain-text format and from redirecting stdout into the log. Applying the same dictionary the API
    applies keeps every process on one format.
    """
    from logging.config import dictConfig

    from django.conf import settings

    dictConfig(settings.LOGGING)


@before_task_publish.connect
def _carry_log_context(headers: dict[str, Any] | None = None, **kwargs: Any) -> None:
    from app.log_context import carry_into_headers

    if headers is not None:
        carry_into_headers(headers)


@task_prerun.connect
def _enter_task_log_context(task: Any = None, **kwargs: Any) -> None:
    from app.log_context import enter_task

    if task is not None:
        enter_task(task)


@task_postrun.connect
def _exit_task_log_context(task: Any = None, **kwargs: Any) -> None:
    from app.log_context import exit_task

    if task is not None:
        exit_task(task)
