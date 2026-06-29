"""Celery application for async ingestion (M2).

Broker/result config comes from Django settings (the ``CELERY_*`` keys); tasks live in
``app.tasks`` and are auto-discovered. Tests/CI set ``task_always_eager`` so no live broker is
needed.
"""

from __future__ import annotations

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("tenantiq")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
