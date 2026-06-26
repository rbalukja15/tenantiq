"""TDD for Layer 1 enforcement: TenantScopedManager + TenantOwnedModel (app.models).

Proves the ADR-0002 contract at the ORM level: the default manager filters by the active tenant
and *raises* when none is set (never returns all rows), save() binds a row to the active tenant,
and a row may not be written for a different tenant than the active one. Engine-agnostic.
"""

from __future__ import annotations

import pytest

from app.models import Document, Tenant
from app.tenant_context import NoActiveTenant, tenant_context

pytestmark = pytest.mark.django_db


def make_tenant(slug: str) -> Tenant:
    return Tenant.objects.create(
        slug=slug,
        name=slug.title(),
        oidc_issuer=f"https://keycloak.test/realms/{slug}",
        oidc_client_id=slug,
    )


def test_objects_with_no_active_tenant_raises():
    with pytest.raises(NoActiveTenant):
        list(Document.objects.all())


def test_all_objects_with_no_active_tenant_does_not_raise():
    # The system/base manager must not raise (Django internals + FK reverse lookups use it).
    assert Document.all_objects.count() == 0


def test_objects_returns_only_the_active_tenants_rows():
    a, b = make_tenant("acme"), make_tenant("globex")
    with tenant_context(a):
        Document.objects.create(title="a-1")
        Document.objects.create(title="a-2")
    with tenant_context(b):
        Document.objects.create(title="b-1")

    with tenant_context(a):
        assert set(Document.objects.values_list("title", flat=True)) == {"a-1", "a-2"}
    with tenant_context(b):
        assert set(Document.objects.values_list("title", flat=True)) == {"b-1"}


def test_save_autofills_tenant_from_context():
    a = make_tenant("acme")
    with tenant_context(a):
        doc = Document(title="x")
        doc.save()
        assert doc.tenant_id == a.id


def test_save_with_mismatched_tenant_raises():
    a, b = make_tenant("acme"), make_tenant("globex")
    with tenant_context(a):
        with pytest.raises(NoActiveTenant):
            Document(title="x", tenant=b).save()
