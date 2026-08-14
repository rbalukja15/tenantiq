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


# --- display name vs identity key (#84) --------------------------------------------------------
#
# The shell greeted a signed-in Alice as "c76c642e-…-8e0ad55a57f4.6fa97fcf2c06". The synthesized
# username is *correct* — it is what stops two realms sharing a host from colliding on Django's
# unique `username` — but it is an identity key, and it had leaked into the interface. These pin
# both halves: a person gets a readable name, and that name never becomes the key.


def test_me_reports_the_name_the_idp_uses_for_the_person(api, tenant_acme, mint_token):
    resp = api.get("/api/me", **bearer(mint_token(sub="abc-123", preferred_username="alice")))

    assert resp.json()["display_name"] == "alice"


def test_the_display_name_is_never_the_identity_key(api, tenant_acme, mint_token):
    # The bug, stated directly: whatever else is true, the thing rendered to a person must not be
    # the synthesized <sub>.<issuer-hash>.
    resp = api.get("/api/me", **bearer(mint_token(sub="abc-123", preferred_username="alice")))
    body = resp.json()

    assert body["username"].startswith("abc-123.")  # the key is unchanged...
    assert body["display_name"] != body["username"]  # ...and is not what gets shown


@pytest.mark.parametrize(
    ("claims", "expected"),
    [
        ({"preferred_username": "alice", "name": "Alice A", "email": "a@acme.test"}, "alice"),
        ({"name": "Alice Anderson", "email": "a@acme.test"}, "Alice Anderson"),
        ({"email": "alice@acme.test"}, "alice@acme.test"),
        ({}, ""),
        # Whitespace is not a name. A claim of "   " must fall through rather than render a blank.
        ({"preferred_username": "   ", "name": "Alice Anderson"}, "Alice Anderson"),
    ],
)
def test_display_name_falls_back_through_the_claims(api, tenant_acme, mint_token, claims, expected):
    resp = api.get("/api/me", **bearer(mint_token(sub="who", **claims)))

    assert resp.json()["display_name"] == expected


def test_a_token_with_no_name_claim_does_not_invent_one(api, tenant_acme, mint_token):
    # Empty is the honest answer, and the caller renders "signed in" without a name. Falling back to
    # `username` here is exactly how the original bug would come back.
    resp = api.get("/api/me", **bearer(mint_token(sub="nameless")))
    body = resp.json()

    assert body["display_name"] == ""
    assert body["username"] not in ("", None)


def test_renaming_at_the_idp_updates_the_name_but_not_the_identity(api, tenant_acme, mint_token):
    # preferred_username is mutable at the IdP — which is why it must never be the lookup key.
    api.get("/api/me", **bearer(mint_token(sub="stable", preferred_username="alice")))
    user_id = User.objects.get(oidc_sub="stable").id

    resp = api.get("/api/me", **bearer(mint_token(sub="stable", preferred_username="alice.smith")))

    assert resp.json()["display_name"] == "alice.smith"
    assert User.objects.filter(oidc_sub="stable").count() == 1
    assert User.objects.get(oidc_sub="stable").id == user_id  # same row, renamed


def test_a_later_token_without_the_claim_does_not_erase_a_known_name(api, tenant_acme, mint_token):
    # Client scopes differ per flow, so a refreshed or differently-scoped token can legitimately
    # omit the claim. Blanking the name on that would make the greeting flicker away mid-session.
    api.get("/api/me", **bearer(mint_token(sub="stable", preferred_username="alice")))

    resp = api.get("/api/me", **bearer(mint_token(sub="stable")))

    assert resp.json()["display_name"] == "alice"


def test_two_people_may_share_a_display_name_across_issuers(api, tenant_acme, mint_token):
    # The reason the key is synthesized in the first place. Two realms can both call someone
    # "alice"; they are still two users, and neither login may reach the other's row.
    Tenant.objects.create(
        slug="globex", name="Globex", oidc_issuer=GLOBEX_ISSUER, oidc_client_id=GLOBEX_CLIENT
    )
    api.get("/api/me", **bearer(mint_token(sub="alice", preferred_username="alice")))
    api.get(
        "/api/me",
        **bearer(
            mint_token(
                sub="alice",
                issuer=GLOBEX_ISSUER,
                audience=GLOBEX_CLIENT,
                preferred_username="alice",
            )
        ),
    )

    assert User.objects.filter(display_name="alice").count() == 2
    assert User.objects.values("username").distinct().count() == 2
