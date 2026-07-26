"""HTTP-level TDD for per-tenant rate limiting & quotas (#49).

Proves the acceptance criterion at the API boundary: a tenant hammering an endpoint is throttled by
its *own* per-tenant budget and answered 429 with ``Retry-After``, while a **second tenant is
entirely unaffected** — throttling honors the same isolation as data access (CLAUDE.md: "isolation
is sacred"). Query / upload / read carry separate budgets, and the limits are configuration.

The read/upload cases run everywhere. The query-endpoint cases are Postgres-gated because the query
path runs pgvector retrieval before it can return 200.
"""

from __future__ import annotations

import pytest
from django.core.cache.backends.locmem import LocMemCache
from django.db import connection
from rest_framework.test import APIClient

from app.auth.tenancy import tenant_for_issuer
from app.auth.verifier import TenantTokenVerifier
from app.models import Tenant
from tests.conftest import TEST_CLIENT_ID, TEST_ISSUER

pytestmark = pytest.mark.django_db

GLOBEX_ISSUER = "https://keycloak.test/realms/globex"
GLOBEX_CLIENT = "tenantiq-globex"

requires_postgres = pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="the query endpoint runs pgvector retrieval before returning",
)


# --- fixtures / helpers ---------------------------------------------------------------------------


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
    return acme, globex


@pytest.fixture
def api(configured_auth) -> APIClient:
    return APIClient()


def bearer(token: str) -> dict:
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


def _set_rates(settings, **rates: str) -> None:
    """Override just the per-scope rates, preserving the rest of REST_FRAMEWORK (esp. auth)."""
    rf = dict(settings.REST_FRAMEWORK)
    rf["DEFAULT_THROTTLE_RATES"] = {**rf.get("DEFAULT_THROTTLE_RATES", {}), **rates}
    settings.REST_FRAMEWORK = rf


def _acme(mint_token) -> dict:
    return bearer(mint_token(sub="alice"))


def _globex(mint_token) -> dict:
    return bearer(mint_token(sub="bob", issuer=GLOBEX_ISSUER, audience=GLOBEX_CLIENT))


class _BrokenCache(LocMemCache):
    """A cache backend whose every read/write raises, to simulate a Redis outage."""

    def _down(self, *args, **kwargs):
        raise OSError("cache backend unreachable")

    get = add = set = incr = _down


# --- read / upload scopes (run everywhere) --------------------------------------------------------


def test_one_tenant_exhausting_reads_does_not_throttle_another_tenant(
    api, tenants, mint_token, settings
):
    # The headline invariant, at the read endpoint: acme hammers to exhaustion; globex is untouched.
    _set_rates(settings, read="3/min")
    codes = [api.get("/api/documents", **_acme(mint_token)).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]
    assert api.get("/api/documents", **_globex(mint_token)).status_code == 200


def test_throttled_response_carries_retry_after(api, tenants, mint_token, settings):
    _set_rates(settings, read="1/min")
    assert api.get("/api/documents", **_acme(mint_token)).status_code == 200
    resp = api.get("/api/documents", **_acme(mint_token))
    assert resp.status_code == 429
    assert int(resp["Retry-After"]) > 0


def test_read_budget_is_shared_across_read_endpoints(api, tenants, mint_token, settings):
    # 'read' is one per-tenant bucket spanning the read endpoints, not per-URL.
    _set_rates(settings, read="2/min")
    assert api.get("/api/documents", **_acme(mint_token)).status_code == 200
    assert api.get("/api/me", **_acme(mint_token)).status_code == 200
    assert api.get("/api/me", **_acme(mint_token)).status_code == 429  # shared read bucket spent


def test_read_and_upload_are_separate_budgets(api, tenants, mint_token, settings):
    # Exhausting reads must not throttle uploads: different scope, different budget.
    _set_rates(settings, read="1/min", upload="5/min")
    assert api.get("/api/documents", **_acme(mint_token)).status_code == 200
    assert api.get("/api/documents", **_acme(mint_token)).status_code == 429  # reads exhausted
    upload = api.post("/api/documents", {"title": "x"}, **_acme(mint_token))
    assert upload.status_code != 429  # upload budget is untouched (a validation 4xx, never a 429)


def test_rate_is_configuration_a_bigger_limit_admits_more(api, tenants, mint_token, settings):
    # Same code, larger configured rate -> more requests admitted. Limits are configuration.
    _set_rates(settings, read="5/min")
    codes = [api.get("/api/documents", **_acme(mint_token)).status_code for _ in range(5)]
    assert codes == [200, 200, 200, 200, 200]
    assert api.get("/api/documents", **_acme(mint_token)).status_code == 429


def test_retry_endpoint_uses_the_per_tenant_upload_budget(api, tenants, mint_token, settings):
    # The retry endpoint re-enqueues ingestion, so it is bounded by the 'upload' scope. Throttling
    # runs before the document lookup, so the doc need not exist (a miss is 404, never 429).
    _set_rates(settings, upload="2/min")
    assert api.post("/api/documents/1/retry", **_acme(mint_token)).status_code != 429
    assert api.post("/api/documents/1/retry", **_acme(mint_token)).status_code != 429
    assert (
        api.post("/api/documents/1/retry", **_acme(mint_token)).status_code == 429
    )  # budget spent
    # a different tenant's retry is unaffected
    assert api.post("/api/documents/1/retry", **_globex(mint_token)).status_code != 429


def test_throttling_fails_open_when_the_cache_is_unavailable(api, tenants, mint_token, settings):
    # A throttle must never be a hard dependency for serving traffic: if the cache (Redis) is down,
    # both requests still succeed rather than 500 — availability over strictness (ADR-0011).
    settings.CACHES = {
        "default": {"BACKEND": "tests.test_ratelimit_api._BrokenCache", "LOCATION": "x"}
    }
    _set_rates(settings, read="1/min")
    assert api.get("/api/documents", **_acme(mint_token)).status_code == 200
    assert (
        api.get("/api/documents", **_acme(mint_token)).status_code == 200
    )  # would be 429 if cache worked


# --- query scope: the acceptance criterion on the real endpoint (Postgres) ------------------------


@requires_postgres
def test_hammering_the_query_endpoint_throttles_the_tenant_but_not_another(
    api, tenants, mint_token, settings
):
    _set_rates(settings, query="3/min")
    payload = {"question": "what are the payment terms?"}
    codes = [
        api.post("/api/query", payload, format="json", **_acme(mint_token)).status_code
        for _ in range(4)
    ]
    assert codes == [200, 200, 200, 429]
    # Acceptance: a different tenant, hammering nothing, is unaffected by acme's exhaustion.
    assert api.post("/api/query", payload, format="json", **_globex(mint_token)).status_code == 200


@requires_postgres
def test_query_rate_limit_sets_retry_after(api, tenants, mint_token, settings):
    _set_rates(settings, query="1/min")
    payload = {"question": "hi?"}
    assert api.post("/api/query", payload, format="json", **_acme(mint_token)).status_code == 200
    resp = api.post("/api/query", payload, format="json", **_acme(mint_token))
    assert resp.status_code == 429
    assert int(resp["Retry-After"]) > 0


@requires_postgres
def test_query_daily_quota_caps_the_tenant_and_isolates_others(api, tenants, mint_token, settings):
    # Quota is the volume half: cap total daily queries, independent of the burst rate.
    settings.TENANTIQ_QUERY_DAILY_QUOTA = 2
    _set_rates(settings, query="1000/min")  # keep the burst rate out of the way
    payload = {"question": "hi?"}
    codes = [
        api.post("/api/query", payload, format="json", **_acme(mint_token)).status_code
        for _ in range(3)
    ]
    assert codes == [200, 200, 429]  # third exceeds the daily quota
    assert api.post("/api/query", payload, format="json", **_globex(mint_token)).status_code == 200


@requires_postgres
def test_rate_denied_queries_do_not_consume_the_daily_quota(api, tenants, mint_token, settings):
    # A burst that the rate throttle rejects (429, zero LLM work) must NOT draw down the daily quota,
    # or a client that retries on 429 would self-lock-out for the rest of the day after a few answers.
    # Only requests that are actually *served* count against the volume quota.
    from django.core.cache import cache
    from django.utils import timezone

    from app.throttling import TenantQueryDailyQuotaThrottle

    acme, _ = tenants
    _set_rates(settings, query="3/min")
    settings.TENANTIQ_QUERY_DAILY_QUOTA = 100  # generous: never the limiting factor here
    payload = {"question": "hi?"}
    codes = [
        api.post("/api/query", payload, format="json", **_acme(mint_token)).status_code
        for _ in range(8)
    ]
    assert codes[:3] == [200, 200, 200]  # served
    assert codes[3:] == [429, 429, 429, 429, 429]  # rate-rejected, no LLM work

    stamp, _reset = TenantQueryDailyQuotaThrottle()._window(timezone.now())
    key = f"quota_query_daily_{acme.id}_{stamp}"
    assert cache.get(key) == 3  # only the 3 served queries drew down the quota, not all 8 attempts


@requires_postgres
def test_query_monthly_quota_caps_the_tenant_and_isolates_others(
    api, tenants, mint_token, settings
):
    # The monthly cap is wired at the endpoint too, not just unit-tested on the class.
    settings.TENANTIQ_QUERY_MONTHLY_QUOTA = 2
    _set_rates(settings, query="1000/min")  # keep the burst rate out of the way
    payload = {"question": "hi?"}
    codes = [
        api.post("/api/query", payload, format="json", **_acme(mint_token)).status_code
        for _ in range(3)
    ]
    assert codes == [200, 200, 429]  # third exceeds the monthly quota
    assert api.post("/api/query", payload, format="json", **_globex(mint_token)).status_code == 200
