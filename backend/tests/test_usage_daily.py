"""The usage dashboard's data: the caller's spend, day by day (#27, ADR-0025).

``GET /api/usage/daily`` is what the dashboard's charts draw. What has to hold:

- **Every day of the range is there**, zeros included and in order, and a row counts on the UTC day
  it was written.
- **The totals are the sum of the days**, with the money exact.
- **The range is checked before anything is read**: calendar dates only, never inverted, never
  starting in the future, at most a year. An end in the future is moved back to today.
- **One tenant never sees another's numbers**: at the API, and with the application's filter taken
  away, where row-level security alone has to keep the sums to one tenant.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone as dtz
from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from app.auth.tenancy import tenant_for_issuer
from app.auth.verifier import TenantTokenVerifier
from app.models import Tenant, UsageRecord
from app.tenant_context import NoActiveTenant, tenant_context
from app.usage import _daily_rows, record_query_usage, usage_by_day, utc_today
from tests.conftest import TEST_CLIENT_ID, TEST_ISSUER

pytestmark = pytest.mark.django_db

GLOBEX_ISSUER = "https://keycloak.test/realms/globex"
GLOBEX_CLIENT = "tenantiq-globex"

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


@pytest.fixture(autouse=True)
def priced(settings):
    # The global pair for every model, so a cost is never zero by accident of the model's name (the
    # shipped table prices the fake and local models at nothing).
    settings.TENANTIQ_LLM_PRICE_INPUT_PER_MTOK = "3.00"
    settings.TENANTIQ_LLM_PRICE_OUTPUT_PER_MTOK = "15.00"
    settings.TENANTIQ_LLM_PRICES = {}


def _acme(mint_token) -> dict:
    return {"HTTP_AUTHORIZATION": f"Bearer {mint_token(sub='alice')}"}


def _globex(mint_token) -> dict:
    token = mint_token(sub="bob", issuer=GLOBEX_ISSUER, audience=GLOBEX_CLIENT)
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


def _spent(tenant: Tenant, when: datetime, input_tokens: int, output_tokens: int) -> None:
    """Record a query's usage as if it was written at ``when``.

    The backdating runs inside the tenant's own context because on Postgres row-level security
    governs an update as much as a read: outside it, the update would match no row at all.
    """
    row = record_query_usage(
        tenant, model="m", input_tokens=input_tokens, output_tokens=output_tokens
    )
    with tenant_context(tenant):
        UsageRecord.objects.filter(pk=row.pk).update(created_at=when)


def _utc(day: date, hour: int = 12, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=dtz.utc)


def _daily(api, headers, query: str = "") -> dict:
    response = api.get(f"/api/usage/daily{query}", **headers)
    assert response.status_code == 200, response.content
    return response.json()


# --- the days -------------------------------------------------------------------------------------


def test_requires_authentication(api, tenants):
    assert api.get("/api/usage/daily").status_code == 401


def test_every_day_of_the_range_is_reported_zeros_included(api, tenants, mint_token):
    # A chart of the range has to be able to draw a day with no queries: left out, the line would
    # run straight from one busy day to the next as if the days between had been busy too.
    acme, _ = tenants
    _spent(acme, _utc(date(2026, 7, 3)), 1000, 500)
    _spent(acme, _utc(date(2026, 7, 5)), 2000, 1000)

    body = _daily(api, _acme(mint_token), "?start=2026-07-01&end=2026-07-07")

    assert [day["date"] for day in body["days"]] == [f"2026-07-0{n}" for n in range(1, 8)]
    by_date = {day["date"]: day for day in body["days"]}
    assert by_date["2026-07-03"] == {
        "date": "2026-07-03",
        "requests": 1,
        "input_tokens": 1000,
        "output_tokens": 500,
        "estimated_cost_usd": "0.010500",  # 1000 x $3/M + 500 x $15/M
    }
    assert by_date["2026-07-04"] == {
        "date": "2026-07-04",
        "requests": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "estimated_cost_usd": "0.000000",
    }
    assert by_date["2026-07-05"]["requests"] == 1
    assert body["start"] == "2026-07-01"
    assert body["end"] == "2026-07-07"
    assert body["timezone"] == "UTC"
    assert body["currency"] == "USD"


def test_a_row_counts_on_the_utc_day_it_was_written(api, tenants, mint_token):
    # Two minutes apart, across midnight UTC: two days, not one, whatever the server's clock says.
    acme, _ = tenants
    _spent(acme, _utc(date(2026, 7, 31), 23, 59), 100, 10)
    _spent(acme, _utc(date(2026, 8, 1), 0, 1), 555, 5)

    body = _daily(api, _acme(mint_token), "?start=2026-07-31&end=2026-08-01")

    assert [(day["date"], day["input_tokens"]) for day in body["days"]] == [
        ("2026-07-31", 100),
        ("2026-08-01", 555),
    ]


def test_the_range_includes_its_first_and_last_day_whole_and_nothing_either_side(
    api, tenants, mint_token
):
    acme, _ = tenants
    _spent(acme, datetime(2026, 7, 9, 23, 59, 59, 999999, tzinfo=dtz.utc), 1, 0)  # the day before
    _spent(acme, datetime(2026, 7, 10, 0, 0, tzinfo=dtz.utc), 10, 0)  # the first instant of it
    _spent(acme, datetime(2026, 7, 12, 23, 59, 59, 999999, tzinfo=dtz.utc), 100, 0)  # its last
    _spent(acme, datetime(2026, 7, 13, 0, 0, tzinfo=dtz.utc), 1000, 0)  # the day after

    body = _daily(api, _acme(mint_token), "?start=2026-07-10&end=2026-07-12")

    assert [day["input_tokens"] for day in body["days"]] == [10, 0, 100]
    assert body["totals"]["input_tokens"] == 110


def test_the_totals_are_the_sum_of_the_days(api, tenants, mint_token):
    acme, _ = tenants
    _spent(acme, _utc(date(2026, 7, 1), 9), 1000, 500)
    _spent(acme, _utc(date(2026, 7, 1), 17), 2000, 1000)
    _spent(acme, _utc(date(2026, 7, 2)), 333, 777)

    body = _daily(api, _acme(mint_token), "?start=2026-07-01&end=2026-07-02")

    assert body["totals"] == {
        "requests": 3,
        "input_tokens": 3333,
        "output_tokens": 2277,
        # 0.031500 on the 1st, plus 333 x $3/M + 777 x $15/M = 0.012654 on the 2nd: exact, as money
        # has to be, which a float sum of the two would not promise.
        "estimated_cost_usd": "0.044154",
    }
    days = body["days"]
    assert sum(Decimal(day["estimated_cost_usd"]) for day in days) == Decimal("0.044154")
    assert sum(day["requests"] for day in days) == 3


# --- the range ------------------------------------------------------------------------------------


def test_the_default_is_the_thirty_days_ending_today(api, tenants, mint_token):
    acme, _ = tenants
    record_query_usage(acme, model="m", input_tokens=100, output_tokens=10)  # written today
    today = utc_today()

    body = _daily(api, _acme(mint_token))

    assert len(body["days"]) == 30
    assert body["start"] == (today - timedelta(days=29)).isoformat()
    assert body["end"] == today.isoformat()
    assert body["days"][-1]["requests"] == 1


def test_an_end_alone_reports_the_thirty_days_ending_there(api, tenants, mint_token):
    body = _daily(api, _acme(mint_token), "?end=2026-07-31")

    assert (body["start"], body["end"], len(body["days"])) == ("2026-07-02", "2026-07-31", 30)


def test_a_start_alone_reports_through_today(api, tenants, mint_token):
    today = utc_today()
    start = today - timedelta(days=6)

    body = _daily(api, _acme(mint_token), f"?start={start.isoformat()}")

    assert (body["start"], body["end"], len(body["days"])) == (
        start.isoformat(),
        today.isoformat(),
        7,
    )


def test_an_end_in_the_future_is_moved_back_to_today(api, tenants, mint_token):
    # "This month" on the 22nd. The days after today have had no usage yet, and drawing them as
    # zeros would report on days that have not happened, so the range stops today and says so.
    today = utc_today()
    start = today - timedelta(days=3)
    end = today + timedelta(days=10)

    body = _daily(api, _acme(mint_token), f"?start={start.isoformat()}&end={end.isoformat()}")

    assert body["end"] == today.isoformat()
    assert body["days"][-1]["date"] == today.isoformat()
    assert len(body["days"]) == 4


def test_a_year_is_the_longest_range(api, tenants, mint_token):
    body = _daily(
        api, _acme(mint_token), "?start=2024-01-01&end=2024-12-31"
    )  # 366 days: a leap year

    assert len(body["days"]) == 366


@pytest.mark.parametrize(
    "query",
    [
        "?start=not-a-date",
        "?end=31/07/2026",
        "?start=2026-02-30",  # well formed, but no such day
        "?start=20260701",  # a basic-format ISO date, which fromisoformat would otherwise take
        "?start=2026-W27-1",  # an ISO week date, likewise
        "?start=2026-07-01T00:00:00Z",  # a timestamp: the breakdown is by day, so it takes days
        "?start=2026-07-10&end=2026-07-09",  # inverted
        "?start=2099-01-01",  # in the future, so with no end it is after today
        "?start=2099-01-01&end=2099-12-31",  # in the future, whatever the end
        "?start=2024-01-01&end=2025-01-01",  # 367 days
    ],
)
def test_a_bad_range_is_a_400_never_a_500(api, tenants, mint_token, query):
    response = api.get(f"/api/usage/daily{query}", **_acme(mint_token))

    assert response.status_code == 400
    assert response.json()["detail"]


def test_a_bad_range_reads_nothing(api, tenants, mint_token):
    # Rejected before the database is asked, so an unbounded range costs one parse, not a scan.
    headers = _acme(mint_token)
    with CaptureQueriesContext(connection) as captured:
        api.get("/api/usage/daily?start=1900-01-01&end=2026-01-01", **headers)
    assert not [q for q in captured if "app_usagerecord" in q["sql"].lower()]


def test_the_breakdown_is_one_statement(tenants):
    # Every day comes from the same snapshot, so a day cannot disagree with its neighbours or with
    # the totals, which are summed from those same days rather than queried again.
    acme, _ = tenants
    _spent(acme, _utc(date(2026, 7, 1)), 100, 10)
    _spent(acme, _utc(date(2026, 7, 2)), 100, 10)
    with tenant_context(acme), CaptureQueriesContext(connection) as captured:
        usage_by_day(first=date(2026, 7, 1), last=date(2026, 7, 31))
    selects = [q for q in captured if "app_usagerecord" in q["sql"].lower()]
    assert len(selects) == 1, [q["sql"] for q in selects]


def test_the_breakdown_is_rate_limited_on_the_read_budget(api, tenants, mint_token, settings):
    rf = dict(settings.REST_FRAMEWORK)
    rf["DEFAULT_THROTTLE_RATES"] = {**rf["DEFAULT_THROTTLE_RATES"], "read": "1/min"}
    settings.REST_FRAMEWORK = rf
    assert api.get("/api/usage/daily", **_acme(mint_token)).status_code == 200
    assert api.get("/api/usage/daily", **_acme(mint_token)).status_code == 429


# --- isolation ------------------------------------------------------------------------------------


def test_another_tenants_spend_never_appears_on_any_day(api, tenants, mint_token):
    # The isolation invariant at the dashboard's boundary: acme's busy days are globex's empty ones.
    acme, globex = tenants
    _spent(acme, _utc(date(2026, 7, 1)), 5000, 5000)
    _spent(acme, _utc(date(2026, 7, 2)), 5000, 5000)
    _spent(globex, _utc(date(2026, 7, 2)), 7, 7)

    body = _daily(api, _globex(mint_token), "?start=2026-07-01&end=2026-07-03")

    assert [(day["requests"], day["input_tokens"]) for day in body["days"]] == [
        (0, 0),
        (1, 7),  # globex's own row, and nothing of acme's on the same day
        (0, 0),
    ]
    assert body["totals"]["requests"] == 1
    assert body["totals"]["input_tokens"] == 7


def test_the_breakdown_refuses_to_run_without_a_tenant(tenants):
    # "Forgot to scope" is a loud error, never a sum across every tenant.
    with pytest.raises(NoActiveTenant):
        usage_by_day(first=date(2026, 7, 1), last=date(2026, 7, 31))


@requires_postgres
def test_row_level_security_alone_keeps_the_days_to_one_tenant(tenants):
    """The grouped statement handed the unscoped manager, so the ORM's tenant filter is gone: the
    database still sums only the active tenant's rows. Proves Layer 2 holds for this path alone.
    """
    acme, globex = tenants
    _spent(acme, _utc(date(2026, 7, 1)), 5000, 5000)
    _spent(globex, _utc(date(2026, 7, 1)), 7, 7)
    start, end = _utc(date(2026, 7, 1), 0), _utc(date(2026, 7, 2), 0)

    with tenant_context(acme):
        seen_by_acme = _daily_rows(UsageRecord.all_objects.all(), start=start, end=end)
    with tenant_context(globex):
        seen_by_globex = _daily_rows(UsageRecord.all_objects.all(), start=start, end=end)

    assert seen_by_acme[date(2026, 7, 1)]["input_tokens"] == 5000  # there to leak, had RLS let it
    assert seen_by_globex == {
        date(2026, 7, 1): {
            "requests": 1,
            "input_tokens": 7,
            "output_tokens": 7,
            "estimated_cost_usd": Decimal("0.000126"),
        }
    }
