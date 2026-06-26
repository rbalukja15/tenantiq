"""TDD for the request-scoped current-tenant contextvar (app.tenant_context).

This is Layer 1's foundation (ADR-0002): the current tenant lives in a contextvar; tenant-scoped
queries read it and *raise* when it is unset. The Postgres GUC half (SET LOCAL) is a no-op off
Postgres, so these tests are engine-agnostic and run on SQLite.
"""

from __future__ import annotations

import uuid

import pytest

from app.models import Tenant
from app.tenant_context import (
    clear_current_tenant,
    get_current_tenant_id,
    set_current_tenant,
    tenant_context,
)


def test_no_tenant_in_context_by_default():
    assert get_current_tenant_id() is None


def test_set_then_get_returns_the_tenant_id():
    tid = uuid.uuid4()
    set_current_tenant(tid)
    try:
        assert get_current_tenant_id() == tid
    finally:
        clear_current_tenant()


def test_clear_resets_to_none():
    set_current_tenant(uuid.uuid4())
    clear_current_tenant()
    assert get_current_tenant_id() is None


@pytest.mark.django_db
def test_tenant_context_sets_inside_and_restores_after():
    tenant = Tenant.objects.create(
        slug="acme",
        name="Acme",
        oidc_issuer="https://keycloak.test/realms/acme",
        oidc_client_id="acme",
    )
    assert get_current_tenant_id() is None
    with tenant_context(tenant):
        assert get_current_tenant_id() == tenant.id
    assert get_current_tenant_id() is None


@pytest.mark.django_db
def test_tenant_context_nested_restores_outer_tenant():
    a = Tenant.objects.create(
        slug="a", name="A", oidc_issuer="https://keycloak.test/realms/a", oidc_client_id="a"
    )
    b = Tenant.objects.create(
        slug="b", name="B", oidc_issuer="https://keycloak.test/realms/b", oidc_client_id="b"
    )
    with tenant_context(a):
        assert get_current_tenant_id() == a.id
        with tenant_context(b):
            assert get_current_tenant_id() == b.id
        assert get_current_tenant_id() == a.id
    assert get_current_tenant_id() is None
