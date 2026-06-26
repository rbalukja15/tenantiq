"""Request-scoped "current tenant" — the shared state behind both isolation layers (ADR-0002).

Layer 1 (ORM): the current tenant id lives in a :class:`~contextvars.ContextVar`; the
``TenantScopedManager`` reads it and *raises* :class:`NoActiveTenant` when it is unset, so a
forgotten scope is a loud error rather than a silent all-tenant read.

Layer 2 (Postgres RLS): the same id is pushed into the ``app.current_tenant`` session variable
with ``SET LOCAL`` (via ``set_config(_, _, true)``), so the database itself filters every row even
if the ORM is bypassed. ``SET LOCAL`` resets at transaction end, so a pooled connection can never
carry one request's tenant into the next.

The contextvar half is engine-agnostic; the GUC half is a no-op off Postgres (e.g. SQLite tests).
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Iterator
from contextvars import ContextVar

from django.db import connection, transaction

_current_tenant: ContextVar[uuid.UUID | None] = ContextVar("current_tenant", default=None)

#: Postgres session variable read by the RLS policy (see the 0003 migration).
GUC = "app.current_tenant"


class NoActiveTenant(RuntimeError):
    """Raised when a tenant-scoped model is accessed with no tenant in context."""


def get_current_tenant_id() -> uuid.UUID | None:
    return _current_tenant.get()


def set_current_tenant(tenant_id: uuid.UUID | None) -> None:
    _current_tenant.set(tenant_id)


def clear_current_tenant() -> None:
    _current_tenant.set(None)


def sync_db_tenant(tenant_id: uuid.UUID) -> None:
    """Push ``tenant_id`` into the Postgres GUC with ``SET LOCAL``. No-op off Postgres.

    Must run inside a transaction so the setting is local to it — ``ATOMIC_REQUESTS`` provides
    that for requests, :func:`tenant_context` for everything else.
    """
    if connection.vendor != "postgresql":
        return
    with connection.cursor() as cursor:
        cursor.execute("SELECT set_config(%s, %s, true)", [GUC, str(tenant_id)])


def activate_tenant(tenant) -> None:
    """Activate ``tenant`` for the current request in both layers (called by the authenticator)."""
    set_current_tenant(tenant.id)
    sync_db_tenant(tenant.id)


@contextlib.contextmanager
def tenant_context(tenant) -> Iterator[None]:
    """Activate ``tenant`` for a non-request code path (tests, Celery tasks, the shell), restoring
    the previous tenant on exit.

    On Postgres it opens a transaction so the ``SET LOCAL`` GUC is scoped to the block; off
    Postgres it only manages the contextvar.
    """
    token = _current_tenant.set(tenant.id)
    try:
        if connection.vendor == "postgresql":
            with transaction.atomic():
                sync_db_tenant(tenant.id)
                yield
        else:
            yield
    finally:
        _current_tenant.reset(token)
