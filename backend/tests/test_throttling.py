"""TDD for per-tenant throttling & quotas (#49) — the mechanism, exercised hermetically.

These call the throttle classes directly (no DB, no HTTP): throttling must be keyed on the *tenant*,
so one tenant exhausting its budget can never consume another tenant's allowance, and the
daily/monthly quota is a configurable per-tenant volume cap (0 = unlimited hook). The HTTP wiring and
the acceptance criterion on the real ``/api/query`` endpoint live in ``test_ratelimit_api.py``.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from django.core.cache import cache

from app.throttling import (
    TenantQueryDailyQuotaThrottle,
    TenantQueryMonthlyQuotaThrottle,
    TenantQueryRateThrottle,
    TenantReadRateThrottle,
)


def _request(tenant_id: uuid.UUID | None = None) -> SimpleNamespace:
    """A minimal stand-in for a DRF request: throttles only read ``request.tenant.id``."""
    return SimpleNamespace(tenant=SimpleNamespace(id=tenant_id or uuid.uuid4()))


def _set_rates(settings, **rates: str) -> None:
    rf = dict(settings.REST_FRAMEWORK)
    rf["DEFAULT_THROTTLE_RATES"] = {**rf.get("DEFAULT_THROTTLE_RATES", {}), **rates}
    settings.REST_FRAMEWORK = rf


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


# --- rate throttles (sliding window, per tenant) --------------------------------------------------


def test_rate_throttle_is_keyed_per_tenant_not_globally(settings):
    # Two tenants in one process must have independent buckets: exhausting A leaves B untouched.
    _set_rates(settings, read="2/min")
    a, b = _request(), _request()

    assert TenantReadRateThrottle().allow_request(a, None) is True
    assert TenantReadRateThrottle().allow_request(a, None) is True
    assert TenantReadRateThrottle().allow_request(a, None) is False  # A exhausted at 2/min

    # B is a different tenant -> its own full allowance, untouched by A's exhaustion.
    assert TenantReadRateThrottle().allow_request(b, None) is True
    assert TenantReadRateThrottle().allow_request(b, None) is True
    assert TenantReadRateThrottle().allow_request(b, None) is False


def test_rate_scopes_are_independent_budgets(settings):
    # The same tenant hitting 'query' must not consume its 'read' budget: separate scopes.
    _set_rates(settings, query="1/min", read="1/min")
    a = _request()

    assert TenantQueryRateThrottle().allow_request(a, None) is True
    assert TenantQueryRateThrottle().allow_request(a, None) is False  # query exhausted
    # read is a different scope -> still fully available for the same tenant.
    assert TenantReadRateThrottle().allow_request(a, None) is True


def test_rate_throttle_does_not_apply_without_a_tenant(settings):
    # Defensive: an unauthenticated request (no tenant) is not rate-limited here — the permission
    # layer already rejected it with 401 before throttles run.
    _set_rates(settings, read="1/min")
    req = SimpleNamespace()  # no .tenant attribute
    assert TenantReadRateThrottle().allow_request(req, None) is True
    assert TenantReadRateThrottle().allow_request(req, None) is True


def test_rate_limit_is_configuration_not_code(settings):
    # Same code, different configured rate -> different behavior. Proves 'limits are configuration'.
    _set_rates(settings, read="1/min")
    a = _request()
    assert TenantReadRateThrottle().allow_request(a, None) is True
    assert TenantReadRateThrottle().allow_request(a, None) is False


# --- daily / monthly quotas (fixed calendar window, per tenant) -----------------------------------
#
# The quota separates gate (allow_request, read-only) from consume (record). A request is only served
# — and only charged — if it clears the gate; a request another throttle rejects never records. These
# tests mirror that flow with _admit().


def _admit(throttle_cls, request) -> bool:
    """Mimic the real flow: gate, then consume only if admitted. Returns whether the request is served."""
    if not throttle_cls().allow_request(request, None):
        return False
    throttle_cls().record(request)
    return True


def test_daily_quota_admits_up_to_the_limit_then_denies_and_isolates_tenants(settings):
    settings.TENANTIQ_QUERY_DAILY_QUOTA = 2
    a, b = _request(), _request()

    assert [_admit(TenantQueryDailyQuotaThrottle, a) for _ in range(3)] == [True, True, False]
    # A different tenant has its own daily budget, untouched by A hitting its cap.
    assert _admit(TenantQueryDailyQuotaThrottle, b) is True


def test_monthly_quota_admits_up_to_the_limit_then_denies(settings):
    settings.TENANTIQ_QUERY_MONTHLY_QUOTA = 3
    a = _request()
    assert [_admit(TenantQueryMonthlyQuotaThrottle, a) for _ in range(4)] == [
        True,
        True,
        True,
        False,
    ]


def test_quota_is_unlimited_when_the_limit_is_zero(settings):
    # 0 is the 'unlimited / disabled hook' sentinel — the quota never denies and never records.
    settings.TENANTIQ_QUERY_DAILY_QUOTA = 0
    a = _request()
    assert all(_admit(TenantQueryDailyQuotaThrottle, a) for _ in range(50))


def test_gate_does_not_consume_so_a_rejected_sibling_request_costs_no_quota(settings):
    # allow_request must be pure: calling it (without record) never draws down the quota. This is what
    # lets a request rejected by another throttle avoid burning quota.
    settings.TENANTIQ_QUERY_DAILY_QUOTA = 1
    a = _request()
    for _ in range(10):
        assert TenantQueryDailyQuotaThrottle().allow_request(a, None) is True  # gate, no consume
    # The one real (served) request still gets its full allowance.
    assert _admit(TenantQueryDailyQuotaThrottle, a) is True
    assert _admit(TenantQueryDailyQuotaThrottle, a) is False


def test_denied_quota_reports_a_positive_retry_after_wait(settings):
    settings.TENANTIQ_QUERY_DAILY_QUOTA = 1
    a = _request()
    assert _admit(TenantQueryDailyQuotaThrottle, a) is True
    denied = TenantQueryDailyQuotaThrottle()
    assert denied.allow_request(a, None) is False
    wait = denied.wait()
    assert wait is not None and wait > 0


# --- quota calendar-window math + reset -----------------------------------------------------------


def test_daily_window_stamp_and_reset_at_midnight_utc():
    from datetime import datetime, timezone as dtz

    stamp, reset = TenantQueryDailyQuotaThrottle()._window(
        datetime(2026, 7, 24, 23, 59, tzinfo=dtz.utc)
    )
    assert stamp == "2026-07-24"
    assert reset == datetime(2026, 7, 25, 0, 0, tzinfo=dtz.utc)


def test_monthly_window_rolls_over_december_to_january():
    from datetime import datetime, timezone as dtz

    stamp, reset = TenantQueryMonthlyQuotaThrottle()._window(
        datetime(2026, 12, 15, 12, 0, tzinfo=dtz.utc)
    )
    assert stamp == "2026-12"
    assert reset == datetime(2027, 1, 1, 0, 0, tzinfo=dtz.utc)


def test_daily_quota_counter_resets_when_the_day_rolls_over(settings, monkeypatch):
    from datetime import datetime, timezone as dtz

    import app.throttling as throttling_mod

    settings.TENANTIQ_QUERY_DAILY_QUOTA = 1
    a = _request()
    clock = {"now": datetime(2026, 7, 24, 10, 0, tzinfo=dtz.utc)}
    monkeypatch.setattr(throttling_mod.timezone, "now", lambda: clock["now"])

    assert _admit(TenantQueryDailyQuotaThrottle, a) is True
    assert _admit(TenantQueryDailyQuotaThrottle, a) is False  # today's single unit is spent
    clock["now"] = datetime(2026, 7, 25, 0, 1, tzinfo=dtz.utc)  # next day
    assert _admit(TenantQueryDailyQuotaThrottle, a) is True  # a fresh window
