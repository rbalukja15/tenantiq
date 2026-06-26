"""TDD for the tenant-scoped documents endpoint + middleware lifecycle (Layer 1 at the API edge).

Proves issue #8's acceptance criterion at the HTTP boundary: GET /api/documents returns only the
caller's tenant's rows, an unauthenticated request is 401, and the request middleware clears the
tenant contextvar afterwards so it cannot leak to the next request on a reused worker thread.
"""

from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from app.auth.tenancy import tenant_for_issuer
from app.auth.verifier import TenantTokenVerifier
from app.models import Document, Tenant
from app.tenant_context import get_current_tenant_id, tenant_context
from tests.conftest import TEST_CLIENT_ID, TEST_ISSUER

pytestmark = pytest.mark.django_db

GLOBEX_ISSUER = "https://keycloak.test/realms/globex"
GLOBEX_CLIENT = "tenantiq-globex"


@pytest.fixture
def configured_auth(settings, rsa_keys):
    _, public_pem = rsa_keys
    settings.TENANTIQ_TOKEN_VERIFIER_FACTORY = lambda: TenantTokenVerifier(
        key_resolver=lambda token, tenant: public_pem,
        tenant_lookup=tenant_for_issuer,
    )


@pytest.fixture
def tenants(db):
    acme = Tenant.objects.create(
        slug="acme", name="Acme", oidc_issuer=TEST_ISSUER, oidc_client_id=TEST_CLIENT_ID
    )
    globex = Tenant.objects.create(
        slug="globex", name="Globex", oidc_issuer=GLOBEX_ISSUER, oidc_client_id=GLOBEX_CLIENT
    )
    with tenant_context(acme):
        Document.objects.create(title="acme-doc")
    with tenant_context(globex):
        Document.objects.create(title="globex-doc")
    return acme, globex


@pytest.fixture
def api(configured_auth) -> APIClient:
    return APIClient()


def bearer(token: str) -> dict:
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


def test_lists_only_callers_tenant_documents(api, tenants, mint_token):
    resp = api.get("/api/documents", **bearer(mint_token(sub="alice")))
    assert resp.status_code == 200
    assert [d["title"] for d in resp.json()] == ["acme-doc"]


def test_other_tenant_sees_only_its_own_documents(api, tenants, mint_token):
    resp = api.get(
        "/api/documents",
        **bearer(mint_token(sub="bob", issuer=GLOBEX_ISSUER, audience=GLOBEX_CLIENT)),
    )
    assert resp.status_code == 200
    assert [d["title"] for d in resp.json()] == ["globex-doc"]


def test_requires_authentication(api, tenants):
    assert api.get("/api/documents").status_code == 401


def test_contextvar_cleared_after_request(api, tenants, mint_token):
    api.get("/api/documents", **bearer(mint_token(sub="alice")))
    assert get_current_tenant_id() is None
