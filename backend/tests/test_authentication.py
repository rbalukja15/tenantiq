"""TDD for the DRF authenticator + /api/me (app.auth.authentication, app.auth.tenancy, app.views).

Hermetic: the verifier is overridden to trust the local test key, while still doing real DB
tenant resolution by issuer. Proves the acceptance criterion: a valid token associates the
caller with exactly one tenant; bad tokens are 401.
"""

from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from app.auth.tenancy import tenant_for_issuer
from app.auth.verifier import TenantTokenVerifier
from app.models import Tenant, TenantMembership, User
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
def tenant_acme():
    return Tenant.objects.create(
        slug="acme", name="Acme Inc", oidc_issuer=TEST_ISSUER, oidc_client_id=TEST_CLIENT_ID
    )


@pytest.fixture
def api(configured_auth) -> APIClient:
    return APIClient()


def bearer(token: str) -> dict:
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


def test_valid_token_authenticates_and_returns_tenant(api, tenant_acme, mint_token):
    resp = api.get("/api/me", **bearer(mint_token(sub="alice", email="alice@acme.test")))
    assert resp.status_code == 200
    body = resp.json()
    assert body["tenant"]["slug"] == "acme"
    assert body["email"] == "alice@acme.test"
    assert User.objects.count() == 1
    assert TenantMembership.objects.filter(tenant=tenant_acme).count() == 1


def test_missing_authorization_returns_401(api, tenant_acme):
    assert api.get("/api/me").status_code == 401


def test_malformed_authorization_returns_401(api, tenant_acme):
    assert api.get("/api/me", HTTP_AUTHORIZATION="Bearer garbage").status_code == 401


def test_expired_token_returns_401(api, tenant_acme, mint_token):
    assert api.get("/api/me", **bearer(mint_token(expires_in=-120))).status_code == 401


def test_unknown_tenant_returns_401(api, mint_token):
    resp = api.get("/api/me", **bearer(mint_token(issuer="https://keycloak.test/realms/ghost")))
    assert resp.status_code == 401


def test_deactivated_tenant_token_is_rejected(api, mint_token):
    # Offboarding: once a tenant is deactivated (is_active=False), a still-valid IdP token for it must
    # no longer authenticate — the issuer resolves to no active tenant (app.auth.tenancy), so 401.
    Tenant.objects.create(
        slug="gone",
        name="Gone",
        oidc_issuer=GLOBEX_ISSUER,
        oidc_client_id=GLOBEX_CLIENT,
        is_active=False,
    )
    token = mint_token(issuer=GLOBEX_ISSUER, audience=GLOBEX_CLIENT, sub="bob")
    assert api.get("/api/me", **bearer(token)).status_code == 401


def test_membership_created_once(api, tenant_acme, mint_token):
    token = mint_token(sub="bob")
    api.get("/api/me", **bearer(token))
    api.get("/api/me", **bearer(token))
    assert User.objects.filter(oidc_sub="bob").count() == 1
    assert TenantMembership.objects.count() == 1


def test_same_sub_two_issuers_are_distinct_users(api, tenant_acme, mint_token):
    Tenant.objects.create(
        slug="globex", name="Globex", oidc_issuer=GLOBEX_ISSUER, oidc_client_id=GLOBEX_CLIENT
    )
    api.get("/api/me", **bearer(mint_token(sub="same")))
    api.get(
        "/api/me", **bearer(mint_token(sub="same", issuer=GLOBEX_ISSUER, audience=GLOBEX_CLIENT))
    )
    assert User.objects.filter(oidc_sub="same").count() == 2
    assert TenantMembership.objects.count() == 2


def test_me_does_not_leak_client_id(api, tenant_acme, mint_token):
    resp = api.get("/api/me", **bearer(mint_token()))
    assert TEST_CLIENT_ID not in resp.content.decode()
