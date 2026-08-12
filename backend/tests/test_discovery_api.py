"""The public tenant-discovery endpoint (#18, ADR-0013 §2).

Resolves the login flow's chicken-and-egg: per-tenant OIDC config lives only in the database and
``/api/me`` needs a token, so a browser arriving at the login page has no way to learn *which*
realm and client to authenticate against. ``GET /api/tenants/discovery?slug=`` hands it exactly
that, and nothing else.

This endpoint is the project's **only unauthenticated surface**, so the tests below pin its shape
hard: the response body is an exact key set (not a superset), unknown and inactive tenants are
indistinguishable, and no other tenant's configuration is ever reachable through it.
"""

from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from app.models import Tenant
from tests.conftest import TEST_CLIENT_ID, TEST_ISSUER

pytestmark = pytest.mark.django_db

URL = "/api/tenants/discovery"

GLOBEX_ISSUER = "https://keycloak.test/realms/globex"
GLOBEX_CLIENT = "tenantiq-globex"


@pytest.fixture
def api() -> APIClient:
    return APIClient()


@pytest.fixture
def tenants(db):
    acme = Tenant.objects.create(
        slug="acme", name="Acme", oidc_issuer=TEST_ISSUER, oidc_client_id=TEST_CLIENT_ID
    )
    globex = Tenant.objects.create(
        slug="globex", name="Globex", oidc_issuer=GLOBEX_ISSUER, oidc_client_id=GLOBEX_CLIENT
    )
    return acme, globex


def _set_rates(settings, **rates: str) -> None:
    """Override just the per-scope rates, preserving the rest of REST_FRAMEWORK."""
    rf = dict(settings.REST_FRAMEWORK)
    rf["DEFAULT_THROTTLE_RATES"] = {**rf.get("DEFAULT_THROTTLE_RATES", {}), **rates}
    settings.REST_FRAMEWORK = rf


# --- the happy path -------------------------------------------------------------------------------


def test_returns_the_tenants_issuer_and_client_id(api, tenants):
    response = api.get(URL, {"slug": "acme"})

    assert response.status_code == 200
    assert response.json() == {"issuer": TEST_ISSUER, "client_id": TEST_CLIENT_ID}


def test_response_carries_nothing_but_issuer_and_client_id(api, tenants):
    # An exact key set, not a subset check: this is a public endpoint, so a field added to Tenant
    # later (a billing plan, an internal display name) must not silently start being published.
    acme, _ = tenants
    # A display name that shares no substring with the issuer or client id, so finding it in the
    # body can only mean the name itself leaked.
    Tenant.objects.filter(pk=acme.pk).update(name="Zephyr Holdings (internal)")

    body = api.get(URL, {"slug": "acme"}).json()

    assert set(body) == {"issuer", "client_id"}
    assert "Zephyr" not in str(body)  # the display name is not published
    assert str(acme.id) not in str(body)  # nor the internal tenant id


def test_needs_no_authentication(api, tenants):
    # The whole point: the browser has no token yet. No Authorization header, still 200.
    response = api.get(URL, {"slug": "acme"})

    assert response.status_code == 200
    assert "WWW-Authenticate" not in response


def test_a_garbage_bearer_token_is_ignored_rather_than_rejected(api, tenants):
    # The endpoint runs with authentication disabled, so a stale/expired token in the browser can
    # never turn discovery into a 401 and strand a user who is simply trying to log in again.
    response = api.get(URL, {"slug": "acme"}, HTTP_AUTHORIZATION="Bearer not-a-real-token")

    assert response.status_code == 200


# --- misses are uniform ---------------------------------------------------------------------------


def test_unknown_slug_is_404(api, tenants):
    assert api.get(URL, {"slug": "does-not-exist"}).status_code == 404


def test_inactive_tenant_is_indistinguishable_from_a_missing_one(api, tenants):
    # A deactivated tenant must not be discoverable, and must not be *distinguishable* from one that
    # never existed — otherwise the endpoint reports which customers churned.
    acme, _ = tenants
    Tenant.objects.filter(pk=acme.pk).update(is_active=False)

    inactive = api.get(URL, {"slug": "acme"})
    unknown = api.get(URL, {"slug": "never-existed"})

    assert inactive.status_code == unknown.status_code == 404
    assert inactive.json() == unknown.json()


@pytest.mark.parametrize("params", [{}, {"slug": ""}, {"slug": "   "}])
def test_a_missing_or_blank_slug_is_a_uniform_404(api, tenants, params):
    # Deliberately 404, not 400: a distinct status for "no slug given" is one more bit an enumerator
    # can use to fingerprint the endpoint, and there is no caller for whom 400 is more actionable.
    response = api.get(URL, params)

    assert response.status_code == 404
    assert response.json() == api.get(URL, {"slug": "never-existed"}).json()


# --- isolation ------------------------------------------------------------------------------------


def test_one_tenants_slug_never_returns_another_tenants_configuration(api, tenants):
    # The isolation proof for this path (CLAUDE.md): discovery is keyed strictly by slug, so acme's
    # slug can only ever yield acme's realm — never globex's, whatever else exists in the table.
    acme_body = api.get(URL, {"slug": "acme"}).json()
    globex_body = api.get(URL, {"slug": "globex"}).json()

    assert acme_body == {"issuer": TEST_ISSUER, "client_id": TEST_CLIENT_ID}
    assert globex_body == {"issuer": GLOBEX_ISSUER, "client_id": GLOBEX_CLIENT}
    assert GLOBEX_ISSUER not in str(acme_body)
    assert TEST_ISSUER not in str(globex_body)


def test_discovery_needs_no_active_tenant_context(api, tenants):
    # Tenant is not a TenantOwnedModel, so the tenant-scoped manager (which *raises* with no active
    # tenant) is not in play here. Proven by the request succeeding with the contextvar cleared —
    # the autouse fixture guarantees that state.
    from app.tenant_context import get_current_tenant_id

    assert get_current_tenant_id() is None
    assert api.get(URL, {"slug": "acme"}).status_code == 200


# --- rate limiting --------------------------------------------------------------------------------


def test_discovery_is_rate_limited_even_though_there_is_no_tenant(api, tenants, settings):
    # The per-tenant throttles key on request.tenant, which is absent here — so this endpoint needs
    # a budget of its own, or the project's only public route would be entirely unbounded.
    _set_rates(settings, discovery="3/min")

    codes = [api.get(URL, {"slug": "acme"}).status_code for _ in range(4)]

    assert codes == [200, 200, 200, 429]


def test_flooding_one_slug_does_not_deny_login_to_another_tenant(api, tenants, settings):
    # The bucket must be the *slug*, not the caller. Under the BFF the browser never reaches this
    # endpoint — the Next server does, on its behalf — so in production every discovery request
    # arrives from one address. Keying on the client would make this a single global bucket, and one
    # anonymous flood would deny login to every tenant at once. Keying on the slug keeps the damage
    # to the tenant actually under attack, which is the isolation promise the rest of the system
    # makes. This is the test that tells those two implementations apart.
    _set_rates(settings, discovery="1/min")

    assert api.get(URL, {"slug": "acme"}).status_code == 200
    assert api.get(URL, {"slug": "acme"}).status_code == 429  # acme's own budget is spent

    assert api.get(URL, {"slug": "globex"}).status_code == 200  # globex can still log in


def test_the_discovery_scope_is_actually_configured(api):
    # A throttle whose scope has no configured rate gets rate=None, which DRF silently treats as
    # "unlimited" — the endpoint would look throttled in code and be wide open in production, with
    # every other test still green. This asserts the wiring itself.
    from app.throttling import PublicDiscoveryRateThrottle

    assert PublicDiscoveryRateThrottle().rate is not None


def test_an_oversized_slug_does_not_produce_an_oversized_cache_key():
    # The ident is interpolated straight into the cache key, and that cache is the same Redis that
    # serves as the Celery broker. An unbounded slug would let anonymous traffic write multi-KB keys
    # into the broker, so the slug is hashed to a fixed width before it gets there.
    from types import SimpleNamespace

    from app.throttling import PublicDiscoveryRateThrottle

    huge = SimpleNamespace(query_params={"slug": "x" * 5000})

    key = PublicDiscoveryRateThrottle().get_cache_key(huge, None)

    assert len(key) < 100


def test_exhausting_discovery_leaves_a_tenants_read_budget_intact(
    api, tenants, settings, mint_token, rsa_keys
):
    # Discovery is anonymous and client-keyed; it must not draw down any tenant's authenticated
    # 'read' budget, or an unauthenticated stranger could rate-limit a paying tenant off the API.
    from app.auth.tenancy import tenant_for_issuer
    from app.auth.verifier import TenantTokenVerifier

    _, public_pem = rsa_keys
    settings.TENANTIQ_TOKEN_VERIFIER_FACTORY = lambda: TenantTokenVerifier(
        key_resolver=lambda token, tenant: public_pem,
        tenant_lookup=tenant_for_issuer,
    )
    _set_rates(settings, discovery="1/min", read="2/min")

    assert api.get(URL, {"slug": "acme"}).status_code == 200
    assert api.get(URL, {"slug": "acme"}).status_code == 429  # anonymous budget exhausted

    # The tenant's authenticated read budget is untouched by the stranger's traffic.
    auth = {"HTTP_AUTHORIZATION": f"Bearer {mint_token(sub='alice')}"}
    assert api.get("/api/me", **auth).status_code == 200
    assert api.get("/api/me", **auth).status_code == 200
