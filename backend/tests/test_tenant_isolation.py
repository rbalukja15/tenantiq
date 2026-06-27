"""Issue #9 — the cross-tenant isolation proof.

Seeds two tenants and asserts, adversarially and at every layer, that tenant A can never read or
query tenant B's data: at the API edge (both directions, and with forged client input), through the
ORM, and — crucially — even when the application scope is bypassed, where Postgres RLS alone must
still block the leak. This is the automated trust signal for M1's core promise (ADR-0002).
"""

from __future__ import annotations

import pytest
from django.db import connection
from rest_framework.test import APIClient

from app.auth.tenancy import tenant_for_issuer
from app.auth.verifier import TenantTokenVerifier
from app.models import Document, Tenant
from app.tenant_context import tenant_context
from tests.conftest import TEST_CLIENT_ID, TEST_ISSUER

pytestmark = pytest.mark.django_db

B_ISSUER = "https://keycloak.test/realms/globex"
B_CLIENT = "tenantiq-globex"

requires_postgres = pytest.mark.skipif(
    connection.vendor != "postgresql", reason="row-level security is a Postgres-only backstop"
)


@pytest.fixture
def configured_auth(settings, rsa_keys):
    _, public_pem = rsa_keys
    settings.TENANTIQ_TOKEN_VERIFIER_FACTORY = lambda: TenantTokenVerifier(
        key_resolver=lambda token, tenant: public_pem,
        tenant_lookup=tenant_for_issuer,
    )


@pytest.fixture
def two_tenants(db):
    a = Tenant.objects.create(
        slug="acme", name="Acme", oidc_issuer=TEST_ISSUER, oidc_client_id=TEST_CLIENT_ID
    )
    b = Tenant.objects.create(
        slug="globex", name="Globex", oidc_issuer=B_ISSUER, oidc_client_id=B_CLIENT
    )
    with tenant_context(a):
        a_doc = Document.objects.create(title="acme-secret")
    with tenant_context(b):
        b_doc = Document.objects.create(title="globex-secret")
    return (a, a_doc), (b, b_doc)


@pytest.fixture
def api(configured_auth) -> APIClient:
    return APIClient()


def bearer(token: str) -> dict:
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


def titles(resp) -> list[str]:
    return [doc["title"] for doc in resp.json()]


# --- API edge: A and B each see only their own, in both directions --------------------------------


def test_api_tenant_a_sees_only_its_own_documents(api, two_tenants, mint_token):
    resp = api.get("/api/documents", **bearer(mint_token(sub="alice")))
    assert resp.status_code == 200
    assert titles(resp) == ["acme-secret"]


def test_api_tenant_b_sees_only_its_own_documents(api, two_tenants, mint_token):
    token = mint_token(sub="bob", issuer=B_ISSUER, audience=B_CLIENT)
    resp = api.get("/api/documents", **bearer(token))
    assert resp.status_code == 200
    assert titles(resp) == ["globex-secret"]


def test_api_client_input_cannot_widen_scope(api, two_tenants, mint_token):
    # The tenant is taken from the verified `iss`; a forged ?tenant_id naming B must be ignored.
    (_, _), (b, _) = two_tenants
    resp = api.get(f"/api/documents?tenant_id={b.id}", **bearer(mint_token(sub="alice")))
    assert resp.status_code == 200
    assert titles(resp) == ["acme-secret"]


def test_unauthenticated_request_is_blocked(api, two_tenants):
    assert api.get("/api/documents").status_code == 401


# --- ORM: A cannot fetch B's row even by its exact id ---------------------------------------------


def test_orm_cannot_fetch_another_tenants_row(two_tenants):
    (a, _), (_, b_doc) = two_tenants
    with tenant_context(a):
        assert not Document.objects.filter(id=b_doc.id).exists()
        with pytest.raises(Document.DoesNotExist):
            Document.objects.get(id=b_doc.id)


# --- The backstop: isolation holds even with the application filter removed ------------------------


@requires_postgres
def test_rls_backstops_a_bypassed_application_filter(two_tenants):
    """Even if a developer forgets to scope — uses the unscoped manager or raw SQL — the database
    refuses to surface another tenant's row. Proves Layer 2 stands alone."""
    (a, _), (_, b_doc) = two_tenants
    with tenant_context(a):  # GUC = acme
        # Unscoped ORM manager: no Layer-1 tenant filter at all.
        assert not Document.all_objects.filter(id=b_doc.id).exists()
        assert set(Document.all_objects.values_list("title", flat=True)) == {"acme-secret"}
        # Raw SQL: the ORM bypassed entirely.
        with connection.cursor() as cur:
            cur.execute("SELECT title FROM app_document")
            assert [row[0] for row in cur.fetchall()] == ["acme-secret"]
