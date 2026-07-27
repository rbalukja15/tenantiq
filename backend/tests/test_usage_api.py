"""HTTP-level TDD for cost & token accounting (#17).

Two things to prove at the boundary:

- **The acceptance criterion:** cost per tenant is queryable for a time range —
  ``GET /api/usage?start=&end=`` returns the caller's totals, scoped to its own tenant, and a second
  tenant never sees that spend.
- **The wiring:** a served ``POST /api/query`` actually records a usage row (with non-zero tokens),
  even though the SSE body completes *after* the request transaction commits and the tenant
  contextvar is cleared — the case a naive recorder gets wrong.

The query-path cases are Postgres-gated because the query endpoint runs pgvector retrieval.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone as dtz
from decimal import Decimal

import pytest
from django.db import connection
from django.utils import timezone
from rest_framework.test import APIClient

from app.auth.tenancy import tenant_for_issuer
from app.auth.verifier import TenantTokenVerifier
from app.models import Chunk, Document, Tenant, UsageRecord
from app.tenant_context import tenant_context
from app.usage import record_query_usage, usage_summary
from tests.conftest import TEST_CLIENT_ID, TEST_ISSUER

pytestmark = pytest.mark.django_db

GLOBEX_ISSUER = "https://keycloak.test/realms/globex"
GLOBEX_CLIENT = "tenantiq-globex"

requires_postgres = pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="the query endpoint runs pgvector retrieval before returning",
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


@pytest.fixture
def priced(settings):
    settings.TENANTIQ_LLM_PRICE_INPUT_PER_MTOK = "3.00"
    settings.TENANTIQ_LLM_PRICE_OUTPUT_PER_MTOK = "15.00"
    # No per-model overrides, so whichever model serves is billed at the global rate. (The shipped
    # defaults price the fake/local models at zero — correct in production, but it would make these
    # cost assertions vacuous.)
    settings.TENANTIQ_LLM_PRICES = {}


def bearer(token: str) -> dict:
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


def _acme(mint_token) -> dict:
    return bearer(mint_token(sub="alice"))


def _globex(mint_token) -> dict:
    return bearer(mint_token(sub="bob", issuer=GLOBEX_ISSUER, audience=GLOBEX_CLIENT))


def _seed(tenant: Tenant, texts: list[str]) -> None:
    """Seed retrievable chunks — embeddings included, or vector search finds nothing to ground on."""
    from app.embeddings import get_embedder

    embedder = get_embedder()
    with tenant_context(tenant):
        doc = Document.objects.create(title="doc", status=Document.Status.READY)
        for i, text in enumerate(texts):
            Chunk.objects.create(
                document=doc,
                index=i,
                text=text,
                char_count=len(text),
                start_offset=0,
                end_offset=len(text),
                embedding=embedder.embed_query(text),
                embedding_model=embedder.model,
            )


# --- the aggregate endpoint -----------------------------------------------------------------------


def test_requires_authentication(api, tenants):
    assert api.get("/api/usage").status_code == 401


def test_usage_endpoint_reports_the_callers_totals(api, tenants, mint_token, priced):
    acme, _ = tenants
    record_query_usage(acme, model="m", input_tokens=1000, output_tokens=500)
    record_query_usage(acme, model="m", input_tokens=2000, output_tokens=1000)

    resp = api.get("/api/usage", **_acme(mint_token))

    assert resp.status_code == 200
    body = resp.json()
    assert body["requests"] == 2
    assert body["input_tokens"] == 3000
    assert body["output_tokens"] == 1500
    assert Decimal(body["estimated_cost_usd"]) == Decimal("0.031500")


def test_usage_endpoint_never_reports_another_tenants_spend(api, tenants, mint_token, priced):
    # The isolation invariant, at the reporting boundary: acme's spend is invisible to globex.
    acme, globex = tenants
    record_query_usage(acme, model="m", input_tokens=5000, output_tokens=5000)
    record_query_usage(globex, model="m", input_tokens=7, output_tokens=7)

    body = api.get("/api/usage", **_globex(mint_token)).json()

    assert body["requests"] == 1
    assert body["input_tokens"] == 7  # not 5007


def test_usage_is_queryable_for_an_explicit_time_range(api, tenants, mint_token, priced):
    # Acceptance (#17): "Cost per tenant is queryable for a time range."
    acme, _ = tenants
    now = timezone.now()
    recent = record_query_usage(acme, model="m", input_tokens=100, output_tokens=10)
    old = record_query_usage(acme, model="m", input_tokens=9999, output_tokens=9999)
    UsageRecord.all_objects.filter(pk=old.pk).update(created_at=now - timedelta(days=90))
    UsageRecord.all_objects.filter(pk=recent.pk).update(created_at=now - timedelta(hours=1))

    start = (now - timedelta(days=1)).isoformat()
    end = now.isoformat()
    body = api.get(f"/api/usage?start={start}&end={end}", **_acme(mint_token)).json()

    assert body["requests"] == 1
    assert body["input_tokens"] == 100  # the 90-day-old row is outside the window


def test_a_bare_calendar_date_range_is_accepted(api, tenants, mint_token, priced):
    # Operators reach for dates, not timestamps: ?start=2026-07-01&end=2026-07-31 must work.
    acme, _ = tenants
    row = record_query_usage(acme, model="m", input_tokens=100, output_tokens=10)
    UsageRecord.all_objects.filter(pk=row.pk).update(
        created_at=timezone.make_aware(datetime(2026, 7, 15, 12, 0), dtz.utc)
    )
    body = api.get("/api/usage?start=2026-07-01&end=2026-07-31", **_acme(mint_token)).json()
    assert body["requests"] == 1


def test_an_unencoded_iso_offset_is_tolerated(api, tenants, mint_token, priced):
    # In a query string '+' decodes to a space, so a client passing datetime.isoformat() straight
    # through sends '...12:00:00 00:00'. That is valid-looking input and must not 400.
    acme, _ = tenants
    record_query_usage(acme, model="m", input_tokens=100, output_tokens=10)
    now = timezone.now()  # after the row, so it falls inside the window
    raw = f"/api/usage?start={(now - timedelta(days=1)).isoformat()}&end={now.isoformat()}"
    assert "+00:00" in raw  # the offset really is unencoded in this request
    resp = api.get(raw, **_acme(mint_token))
    assert resp.status_code == 200
    assert resp.json()["requests"] == 1


def test_malformed_range_is_a_400_not_a_500(api, tenants, mint_token):
    resp = api.get("/api/usage?start=not-a-date", **_acme(mint_token))
    assert resp.status_code == 400


def test_start_after_end_is_rejected(api, tenants, mint_token):
    now = timezone.now()
    start = now.isoformat()
    end = (now - timedelta(days=5)).isoformat()
    assert api.get(f"/api/usage?start={start}&end={end}", **_acme(mint_token)).status_code == 400


def test_no_usage_reports_zeros(api, tenants, mint_token, priced):
    body = api.get("/api/usage", **_acme(mint_token)).json()
    assert body["requests"] == 0
    assert body["input_tokens"] == 0
    assert Decimal(body["estimated_cost_usd"]) == Decimal("0")


def test_usage_endpoint_is_rate_limited_on_the_read_budget(api, tenants, mint_token, settings):
    rf = dict(settings.REST_FRAMEWORK)
    rf["DEFAULT_THROTTLE_RATES"] = {**rf["DEFAULT_THROTTLE_RATES"], "read": "1/min"}
    settings.REST_FRAMEWORK = rf
    assert api.get("/api/usage", **_acme(mint_token)).status_code == 200
    assert api.get("/api/usage", **_acme(mint_token)).status_code == 429


# --- wiring: a served query records usage ---------------------------------------------------------


@requires_postgres
def test_a_served_query_records_usage_for_the_tenant(api, tenants, mint_token, priced):
    # The row must be written even though the SSE body completes after the request transaction has
    # committed and the middleware cleared the tenant contextvar.
    acme, _ = tenants
    _seed(acme, ["Payment terms are net thirty days."])

    resp = api.post(
        "/api/query",
        {"question": "what are the payment terms?"},
        format="json",
        **_acme(mint_token),
    )
    assert resp.status_code == 200
    b"".join(resp.streaming_content)  # drain the stream so the generator's tail runs

    with tenant_context(acme):
        row = UsageRecord.objects.get()
    assert row.kind == UsageRecord.Kind.QUERY
    assert row.input_tokens > 0  # the grounded prompt was charged
    assert row.output_tokens > 0  # the streamed answer was charged
    assert row.estimated_cost_usd > Decimal("0")


@requires_postgres
def test_recorded_usage_surfaces_through_the_usage_endpoint(api, tenants, mint_token, priced):
    # End to end: query -> recorded -> reportable. This is what "knowing what a tenant costs" means.
    acme, _ = tenants
    _seed(acme, ["Net thirty days."])

    resp = api.post("/api/query", {"question": "terms?"}, format="json", **_acme(mint_token))
    b"".join(resp.streaming_content)

    body = api.get("/api/usage", **_acme(mint_token)).json()
    assert body["requests"] == 1
    assert Decimal(body["estimated_cost_usd"]) > Decimal("0")


@requires_postgres
def test_a_query_with_no_context_is_not_charged(api, tenants, mint_token, priced):
    # No context means the model is never called (#15), so there is nothing to charge. Recording a
    # zero-cost row would inflate the request count with spend that never happened.
    acme, _ = tenants
    resp = api.post("/api/query", {"question": "anything?"}, format="json", **_acme(mint_token))
    assert resp.status_code == 200
    b"".join(resp.streaming_content)

    with tenant_context(acme):
        assert UsageRecord.objects.count() == 0


@requires_postgres
def test_a_rejected_query_records_no_usage(api, tenants, mint_token, priced):
    # A 400 never reaches the model, so it must not be charged.
    acme, _ = tenants
    assert (
        api.post("/api/query", {"question": "  "}, format="json", **_acme(mint_token)).status_code
        == 400
    )
    with tenant_context(acme):
        assert UsageRecord.objects.count() == 0


@requires_postgres
def test_accounting_failure_does_not_break_an_already_delivered_answer(
    api, tenants, mint_token, priced, monkeypatch
):
    # Accounting runs at the tail of the response body, after every token has been sent. If the write
    # fails (DB down, misconfigured price), the client must still get its complete answer — losing a
    # usage row is bad, corrupting a delivered response is worse.
    import app.views as views_mod

    def _boom(*args, **kwargs):
        raise RuntimeError("usage table unavailable")

    monkeypatch.setattr(views_mod, "record_query_usage", _boom)
    acme, _ = tenants
    _seed(acme, ["Net thirty days."])

    resp = api.post("/api/query", {"question": "terms?"}, format="json", **_acme(mint_token))
    assert resp.status_code == 200
    body = b"".join(resp.streaming_content)  # must not raise
    assert b"event: citations" in body  # the stream completed normally, terminal frame included

    with tenant_context(acme):
        assert UsageRecord.objects.count() == 0  # the row was lost, as expected


def test_a_bare_end_date_includes_that_whole_day(api, tenants, mint_token, priced):
    # ?end=2026-07-31 means "through the 31st". Resolving it to midnight would silently drop the last
    # day of every month report.
    acme, _ = tenants
    late = record_query_usage(acme, model="m", input_tokens=100, output_tokens=10)
    next_day = record_query_usage(acme, model="m", input_tokens=555, output_tokens=5)
    UsageRecord.all_objects.filter(pk=late.pk).update(
        created_at=timezone.make_aware(datetime(2026, 7, 31, 23, 59), dtz.utc)
    )
    UsageRecord.all_objects.filter(pk=next_day.pk).update(
        created_at=timezone.make_aware(datetime(2026, 8, 1, 0, 1), dtz.utc)
    )

    body = api.get("/api/usage?start=2026-07-01&end=2026-07-31", **_acme(mint_token)).json()

    assert body["requests"] == 1  # the 23:59 row on the end date counts...
    assert body["input_tokens"] == 100  # ...and the next day's row does not


def test_a_future_start_with_no_end_is_rejected(api, tenants, mint_token, priced):
    # Without an explicit end the window ends "now", so a future start is an inverted range — a 400,
    # not a 200 reporting a misleading "no spend".
    assert api.get("/api/usage?start=2099-01-01", **_acme(mint_token)).status_code == 400


def test_usage_summary_is_a_single_aggregate_query(api, tenants, mint_token, priced):
    # requests/tokens/cost must come from one statement, so the count can't reflect a different
    # snapshot than the sums.
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    acme, _ = tenants
    record_query_usage(acme, model="m", input_tokens=100, output_tokens=10)
    with tenant_context(acme), CaptureQueriesContext(connection) as captured:
        usage_summary()
    selects = [q for q in captured if "app_usagerecord" in q["sql"].lower()]
    assert len(selects) == 1, [q["sql"] for q in selects]


# --- what is (and isn't) charged -------------------------------------------------------------------


class _FailsImmediatelyLLM:
    """A backend that dies before emitting anything — a rotated key, a 529, Ollama down."""

    model = "claude-test"

    def stream(self, system_prompt, user_prompt):  # noqa: ARG002
        raise RuntimeError("provider unavailable")
        yield ""  # pragma: no cover - makes this a generator

    def generate(self, system_prompt, user_prompt):  # noqa: ARG002
        raise AssertionError("unused")


class _FailsAfterOneTokenLLM:
    """Emits real output, then fails — tokens were genuinely spent."""

    model = "claude-test"

    def stream(self, system_prompt, user_prompt):  # noqa: ARG002
        yield "Partial answer that really was generated"
        raise RuntimeError("provider died mid-stream")

    def generate(self, system_prompt, user_prompt):  # noqa: ARG002
        raise AssertionError("unused")


@requires_postgres
def test_a_generation_that_produced_nothing_is_not_charged(
    api, tenants, mint_token, priced, settings
):
    # A provider outage must not manufacture spend: with no tokens produced there is nothing to bill,
    # so a client retry loop during an outage cannot run up a bill for answers it never got.
    settings.TENANTIQ_LLM_FACTORY = lambda: _FailsImmediatelyLLM()
    acme, _ = tenants
    _seed(acme, ["Net thirty days."])

    resp = api.post("/api/query", {"question": "terms?"}, format="json", **_acme(mint_token))
    body = b"".join(resp.streaming_content)
    assert b"event: error" in body  # generation really did fail

    with tenant_context(acme):
        assert UsageRecord.objects.count() == 0


@requires_postgres
def test_a_generation_that_failed_after_emitting_tokens_is_charged_for_them(
    api, tenants, mint_token, priced, settings
):
    # The other half: tokens that were produced were paid for, so they are recorded even though the
    # answer ended in an error.
    settings.TENANTIQ_LLM_FACTORY = lambda: _FailsAfterOneTokenLLM()
    acme, _ = tenants
    _seed(acme, ["Net thirty days."])

    resp = api.post("/api/query", {"question": "terms?"}, format="json", **_acme(mint_token))
    b"".join(resp.streaming_content)

    with tenant_context(acme):
        row = UsageRecord.objects.get()
    assert row.output_tokens > 0
    assert row.input_tokens > 0


def test_a_client_that_disconnects_midstream_is_still_charged(tenants, priced):
    # This is the whole reason accounting lives in a `finally`: when the client hangs up, the response
    # generator is closed mid-flight (GeneratorExit) and the tokens already produced must still be
    # recorded. Driven at the generator level — going through the test client would fire
    # request_finished and close the DB connection, which is a harness artifact, not the behaviour.
    from app.rag import AssembledContext, Source, build_grounded_prompt
    from app.views import QueryView

    class _ChattyLLM:
        model = "claude-test"

        def stream(self, system_prompt, user_prompt):  # noqa: ARG002
            yield "First part of the answer. "
            yield "Second part the client never reads."

    acme, _ = tenants
    source = Source(
        number=1,
        chunk_id=1,
        document_id=1,
        document_title="Doc",
        chunk_index=0,
        start_offset=0,
        end_offset=16,
        similarity=0.9,
        text="Net thirty days.",
    )
    system, user = build_grounded_prompt("terms?", (source,))
    context = AssembledContext(
        question="terms?", sources=(source,), system_prompt=system, user_prompt=user
    )

    stream = QueryView._stream_and_account(context, acme, _ChattyLLM())
    next(stream)  # one frame delivered...
    stream.close()  # ...then the client disconnects

    with tenant_context(acme):
        row = UsageRecord.objects.get()
    assert row.output_tokens > 0  # charged for what was produced before the hang-up
    assert row.input_tokens > 0


@requires_postgres
def test_usage_records_the_model_that_actually_served_the_answer(
    api, tenants, mint_token, settings
):
    # With no Anthropic key the local fallback serves the answer. Recording the configured Anthropic
    # model name (and its price) would report spend on a provider that served nothing.
    class _LocalLLM:
        model = "llama3.1"

        def stream(self, system_prompt, user_prompt):  # noqa: ARG002
            yield "Locally generated answer."

        def generate(self, system_prompt, user_prompt):  # noqa: ARG002
            raise AssertionError("unused")

    settings.TENANTIQ_LLM_FACTORY = lambda: _LocalLLM()
    settings.TENANTIQ_LLM_MODEL = "claude-opus-4-8"
    settings.TENANTIQ_LLM_PRICE_INPUT_PER_MTOK = "5.00"
    settings.TENANTIQ_LLM_PRICE_OUTPUT_PER_MTOK = "25.00"
    settings.TENANTIQ_LLM_PRICES = {"llama3.1": {"input": "0", "output": "0"}}
    acme, _ = tenants
    _seed(acme, ["Net thirty days."])

    resp = api.post("/api/query", {"question": "terms?"}, format="json", **_acme(mint_token))
    b"".join(resp.streaming_content)

    with tenant_context(acme):
        row = UsageRecord.objects.get()
    assert row.model_name == "llama3.1"  # not the configured Anthropic model
    assert row.estimated_cost_usd == Decimal("0.000000")  # priced as the local model


@requires_postgres
def test_interleaved_streams_charge_each_tenant_its_own_usage(
    api, tenants, mint_token, priced, settings
):
    # Isolation applies to accounting too. Two tenants' answers stream concurrently and their frames
    # interleave; each generator must charge its *own* tenant. Rows are read back inside each tenant's
    # context — under forced RLS that is the only correct way to read them.
    acme, globex = tenants
    _seed(acme, ["Acme payment terms are net thirty days."])
    _seed(globex, ["Globex payment terms are net sixty days."])

    a = api.post("/api/query", {"question": "payment terms?"}, format="json", **_acme(mint_token))
    b = api.post("/api/query", {"question": "payment terms?"}, format="json", **_globex(mint_token))
    left, right = iter(a.streaming_content), iter(b.streaming_content)
    alive = {"a": True, "b": True}
    while any(alive.values()):  # pull one frame from each in turn
        for name, stream in (("a", left), ("b", right)):
            if not alive[name]:
                continue
            try:
                next(stream)
            except StopIteration:
                alive[name] = False

    for tenant in (acme, globex):
        with tenant_context(tenant):
            row = UsageRecord.objects.get()  # exactly one, and it is this tenant's
            assert row.tenant_id == tenant.id
            assert row.output_tokens > 0
