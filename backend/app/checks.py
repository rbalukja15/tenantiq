"""System checks for settings that are only wrong in relation to each other (#55, ADR-0023).

The settings module reads each value on its own. These checks run with `manage.py check`, and with
`migrate`, which is the first thing a release runs, so a combination that would damage live work
stops the release before any pod changes.
"""

from __future__ import annotations

from django.conf import settings
from django.core.checks import Error, register


@register()
def check_the_sweep_cannot_reap_running_work(app_configs, **kwargs) -> list[Error]:
    """The stuck-document sweep may only fail documents no attempt can still be working on.

    An attempt refreshes ``updated_at`` when it starts and the hard time limit ends it, but a
    retry also waits for its backoff and then for a free worker while the document stays PROCESSING.
    Twice the limit leaves room for one whole attempt and at least that long again in the queue.
    """
    errors = []
    reap_after = settings.TENANTIQ_INGEST_REAP_AFTER_SECONDS
    time_limit = settings.TENANTIQ_INGEST_TIME_LIMIT
    if reap_after < 2 * time_limit:
        errors.append(
            Error(
                f"TENANTIQ_INGEST_REAP_AFTER_SECONDS is {reap_after}, less than twice "
                f"TENANTIQ_INGEST_TIME_LIMIT ({time_limit}).",
                hint=(
                    "The sweep would fail documents whose ingestion may still be running or about to "
                    f"retry. Set it to at least {2 * time_limit}; the default is 1800."
                ),
                id="tenantiq.E001",
            )
        )
    interval = settings.TENANTIQ_INGEST_SWEEP_INTERVAL_SECONDS
    if interval <= 0:
        errors.append(
            Error(
                f"TENANTIQ_INGEST_SWEEP_INTERVAL_SECONDS is {interval}; it must be a positive number "
                "of seconds.",
                hint="Celery beat sends the stuck-document sweep this often. The default is 300.",
                id="tenantiq.E002",
            )
        )
    return errors
