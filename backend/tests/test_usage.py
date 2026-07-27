"""TDD for per-tenant cost & token accounting (#17) — the accounting core.

Cost accounting has to be trustworthy, so these pin the properties that make it so:

- **Money is Decimal, never float.** A float would accumulate representation error across thousands
  of rows and make a bill unreconcilable.
- **Pricing is configuration**, per model, per million tokens — a price change is a settings change.
- **Records are tenant-owned**, so a usage row inherits both isolation layers and one tenant can
  never read (or aggregate over) another's spend.
- **The summary is a real time-range aggregate**: rows outside the window are excluded.

The HTTP surface and the /api/query wiring live in ``test_usage_api.py``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone as dtz
from decimal import Decimal

import pytest
from django.utils import timezone

from app.models import Tenant, UsageRecord
from app.tenant_context import tenant_context
from app.usage import estimate_tokens, estimated_cost_usd, record_query_usage, usage_summary

pytestmark = pytest.mark.django_db


@pytest.fixture
def two_tenants(db):
    acme = Tenant.objects.create(
        slug="acme", name="Acme", oidc_issuer="https://kc.test/acme", oidc_client_id="a"
    )
    globex = Tenant.objects.create(
        slug="globex", name="Globex", oidc_issuer="https://kc.test/globex", oidc_client_id="g"
    )
    return acme, globex


# --- pricing (Decimal, configuration-driven) ------------------------------------------------------


def test_cost_is_computed_from_configured_per_million_token_prices(settings):
    settings.TENANTIQ_LLM_PRICE_INPUT_PER_MTOK = "3.00"
    settings.TENANTIQ_LLM_PRICE_OUTPUT_PER_MTOK = "15.00"
    # 1M input @ $3 + 1M output @ $15 = $18 exactly.
    assert estimated_cost_usd(1_000_000, 1_000_000) == Decimal("18.000000")


def test_cost_is_a_decimal_not_a_float(settings):
    settings.TENANTIQ_LLM_PRICE_INPUT_PER_MTOK = "3.00"
    settings.TENANTIQ_LLM_PRICE_OUTPUT_PER_MTOK = "15.00"
    cost = estimated_cost_usd(1000, 500)
    assert isinstance(cost, Decimal)  # money must never be a float
    # 1000/1e6*3 + 500/1e6*15 = 0.003 + 0.0075 = 0.0105
    assert cost == Decimal("0.010500")


def test_price_change_is_configuration_not_code(settings):
    settings.TENANTIQ_LLM_PRICE_INPUT_PER_MTOK = "1.00"
    settings.TENANTIQ_LLM_PRICE_OUTPUT_PER_MTOK = "1.00"
    cheap = estimated_cost_usd(1_000_000, 0)
    settings.TENANTIQ_LLM_PRICE_INPUT_PER_MTOK = "10.00"
    expensive = estimated_cost_usd(1_000_000, 0)
    assert cheap == Decimal("1.000000") and expensive == Decimal("10.000000")


def test_zero_tokens_cost_nothing(settings):
    settings.TENANTIQ_LLM_PRICE_INPUT_PER_MTOK = "3.00"
    settings.TENANTIQ_LLM_PRICE_OUTPUT_PER_MTOK = "15.00"
    assert estimated_cost_usd(0, 0) == Decimal("0.000000")


def test_token_estimate_scales_with_text_length():
    assert estimate_tokens("") == 0
    short, long = estimate_tokens("a" * 40), estimate_tokens("a" * 400)
    assert 0 < short < long


# --- recording ------------------------------------------------------------------------------------


def test_record_query_usage_stores_a_tenant_owned_row(two_tenants, settings):
    settings.TENANTIQ_LLM_PRICE_INPUT_PER_MTOK = "3.00"
    settings.TENANTIQ_LLM_PRICE_OUTPUT_PER_MTOK = "15.00"
    acme, _ = two_tenants

    record_query_usage(acme, model="claude-test", input_tokens=1000, output_tokens=500)

    with tenant_context(acme):
        row = UsageRecord.objects.get()
    assert row.tenant_id == acme.id
    assert row.kind == UsageRecord.Kind.QUERY
    assert (row.input_tokens, row.output_tokens) == (1000, 500)
    assert row.estimated_cost_usd == Decimal("0.010500")
    assert row.model_name == "claude-test"


def test_recording_works_without_an_ambient_tenant_context(two_tenants):
    # The SSE body streams *after* the request transaction commits and after the middleware clears the
    # tenant contextvar, so recording must establish tenant context itself rather than assume one.
    acme, _ = two_tenants
    record_query_usage(acme, model="m", input_tokens=10, output_tokens=1)
    with tenant_context(acme):
        assert UsageRecord.objects.count() == 1


def test_one_tenants_usage_is_invisible_to_another(two_tenants):
    acme, globex = two_tenants
    record_query_usage(acme, model="m", input_tokens=100, output_tokens=10)

    with tenant_context(globex):
        assert UsageRecord.objects.count() == 0  # never sees acme's spend
    with tenant_context(acme):
        assert UsageRecord.objects.count() == 1


# --- summary (time-range aggregate) ---------------------------------------------------------------


def _at(record: UsageRecord, when: datetime) -> None:
    """Backdate a row (created_at is auto_now_add, so it needs an explicit update)."""
    UsageRecord.all_objects.filter(pk=record.pk).update(created_at=when)


def test_summary_aggregates_tokens_and_cost_for_the_tenant(two_tenants, settings):
    settings.TENANTIQ_LLM_PRICE_INPUT_PER_MTOK = "3.00"
    settings.TENANTIQ_LLM_PRICE_OUTPUT_PER_MTOK = "15.00"
    acme, _ = two_tenants
    record_query_usage(acme, model="m", input_tokens=1000, output_tokens=500)
    record_query_usage(acme, model="m", input_tokens=2000, output_tokens=1000)

    with tenant_context(acme):
        summary = usage_summary()

    assert summary["requests"] == 2
    assert summary["input_tokens"] == 3000
    assert summary["output_tokens"] == 1500
    assert summary["estimated_cost_usd"] == Decimal("0.031500")  # 0.0105 + 0.0210


def test_summary_excludes_rows_outside_the_requested_range(two_tenants):
    acme, _ = two_tenants
    now = datetime(2026, 7, 20, 12, 0, tzinfo=dtz.utc)
    inside = record_query_usage(acme, model="m", input_tokens=100, output_tokens=10)
    outside = record_query_usage(acme, model="m", input_tokens=999, output_tokens=999)
    _at(inside, now)
    _at(outside, now - timedelta(days=10))

    with tenant_context(acme):
        summary = usage_summary(start=now - timedelta(days=1), end=now + timedelta(days=1))

    assert summary["requests"] == 1
    assert summary["input_tokens"] == 100  # the out-of-range row is excluded


def test_summary_of_no_usage_is_zero_not_null(two_tenants):
    # An empty aggregate must be reportable zeros, not None — a caller shouldn't special-case it.
    acme, _ = two_tenants
    with tenant_context(acme):
        summary = usage_summary()
    assert summary["requests"] == 0
    assert summary["input_tokens"] == 0
    assert summary["output_tokens"] == 0
    assert summary["estimated_cost_usd"] == Decimal("0")


def test_summary_never_includes_another_tenants_spend(two_tenants):
    acme, globex = two_tenants
    record_query_usage(acme, model="m", input_tokens=5000, output_tokens=5000)
    record_query_usage(globex, model="m", input_tokens=7, output_tokens=7)

    with tenant_context(globex):
        summary = usage_summary()

    assert summary["requests"] == 1
    assert summary["input_tokens"] == 7  # acme's 5000 is invisible


# --- per-model pricing ----------------------------------------------------------------------------


def test_a_model_with_a_configured_price_overrides_the_global_pair(settings):
    settings.TENANTIQ_LLM_PRICE_INPUT_PER_MTOK = "5.00"
    settings.TENANTIQ_LLM_PRICE_OUTPUT_PER_MTOK = "25.00"
    settings.TENANTIQ_LLM_PRICES = {"llama3.1": {"input": "0", "output": "0"}}
    # A locally served answer costs nothing per token; billing it at Anthropic rates would report
    # spend that never happened.
    assert estimated_cost_usd(1_000_000, 1_000_000, "llama3.1") == Decimal("0.000000")
    # An unlisted model still falls back to the global pair.
    assert estimated_cost_usd(1_000_000, 0, "claude-opus-4-8") == Decimal("5.000000")


def test_recorded_cost_uses_the_price_of_the_model_that_served(two_tenants, settings):
    settings.TENANTIQ_LLM_PRICE_INPUT_PER_MTOK = "5.00"
    settings.TENANTIQ_LLM_PRICE_OUTPUT_PER_MTOK = "25.00"
    settings.TENANTIQ_LLM_PRICES = {"llama3.1": {"input": "0", "output": "0"}}
    acme, _ = two_tenants
    row = record_query_usage(acme, model="llama3.1", input_tokens=100_000, output_tokens=100_000)
    assert row.model_name == "llama3.1"
    assert row.estimated_cost_usd == Decimal("0.000000")


# --- default window -------------------------------------------------------------------------------


def test_the_default_window_excludes_rows_older_than_thirty_days(two_tenants):
    acme, _ = two_tenants
    now = timezone.now()
    recent = record_query_usage(acme, model="m", input_tokens=100, output_tokens=10)
    stale = record_query_usage(acme, model="m", input_tokens=9999, output_tokens=9999)
    _at(recent, now - timedelta(days=1))
    _at(stale, now - timedelta(days=31))  # outside the 30-day default

    with tenant_context(acme):
        summary = usage_summary()

    assert summary["requests"] == 1
    assert summary["input_tokens"] == 100
