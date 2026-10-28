"""Per-tenant cost & token accounting (#17, ADR-0012).

Production AI means knowing what each tenant costs. This module owns three things:

- **Pricing.** :func:`estimated_cost_usd` turns token counts into money using configured
  per-million-token prices. Everything is :class:`~decimal.Decimal` — cost is money, and float
  representation error would accumulate across thousands of rows into an unreconcilable total.
- **Recording.** :func:`record_query_usage` writes one :class:`~app.models.UsageRecord` for a served
  query. It establishes tenant context itself: the streaming answer (#48) finishes *after* the
  request transaction has committed and after the middleware cleared the tenant contextvar, so a
  recorder that assumed an ambient tenant would either write nothing or raise.
- **Reporting.** :func:`usage_summary` aggregates the *current tenant's* rows over a time range, and
  :func:`usage_by_day` breaks a range down by day for the usage dashboard (#27). Both read through
  the tenant-scoped manager, so a tenant can only ever aggregate its own spend.

Token counts are **estimates**: the streaming path yields text deltas and exposes no provider usage
numbers, so tokens are estimated from the prompt and the answer with the same chars-per-token
heuristic used for chunking (ADR-0003). The estimate is deliberately conservative and honest about
what it is — see ADR-0012 for the limits and the path to exact provider counts.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone as dt_timezone
from decimal import Decimal

from django.conf import settings
from django.db.models import Count, QuerySet, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone

from app.chunking import estimate_tokens as _estimate_tokens
from app.models import Tenant, UsageRecord
from app.tenant_context import tenant_context

#: Cost is quantized to micro-dollars — sub-cent precision, since one cheap request can cost far less
#: than a cent, while keeping totals exact under addition.
_CENTS = Decimal("0.000001")
_PER_MILLION = Decimal(1_000_000)

#: How far back :func:`usage_summary` looks when no explicit range is given.
DEFAULT_WINDOW = timedelta(days=30)

#: The days :func:`usage_by_day` covers when no range is given: today and the 29 before it.
DEFAULT_DAYS = 30

#: The longest range :func:`usage_by_day` reports: a year, leap day included. Every day is an entry
#: in the response and a point on the dashboard's charts, so this bounds both.
MAX_DAYS = 366


def estimate_tokens(text: str) -> int:
    """Approximate the token count of ``text`` (shared heuristic with chunking, ADR-0003)."""
    return _estimate_tokens(text)


def _decimal(value: object) -> Decimal:
    """Coerce a configured price to Decimal (accepts str/int/Decimal; blank/None means zero)."""
    return Decimal(str(value)) if value not in (None, "") else Decimal(0)


def _prices_for(model: str) -> tuple[Decimal, Decimal]:
    """The (input, output) per-million-token prices for ``model``.

    ``TENANTIQ_LLM_PRICES`` maps a model name to its prices, so a deployment that serves more than one
    model prices each correctly — notably the local Ollama fallback, which costs nothing per token and
    must not be billed at Anthropic rates. A model with no entry falls back to the global
    ``TENANTIQ_LLM_PRICE_{INPUT,OUTPUT}_PER_MTOK`` pair.
    """
    table = getattr(settings, "TENANTIQ_LLM_PRICES", {}) or {}
    entry = table.get(model)
    if entry is not None:
        return _decimal(entry.get("input")), _decimal(entry.get("output"))
    return (
        _decimal(getattr(settings, "TENANTIQ_LLM_PRICE_INPUT_PER_MTOK", 0)),
        _decimal(getattr(settings, "TENANTIQ_LLM_PRICE_OUTPUT_PER_MTOK", 0)),
    )


def estimated_cost_usd(input_tokens: int, output_tokens: int, model: str = "") -> Decimal:
    """Estimated USD cost of a request, from the per-million-token prices configured for ``model``.

    Pricing is configuration, so a provider price change is a settings change. Input and output are
    priced separately because output tokens are normally the dearer side.
    """
    price_in, price_out = _prices_for(model)
    cost = (Decimal(input_tokens) / _PER_MILLION) * price_in + (
        Decimal(output_tokens) / _PER_MILLION
    ) * price_out
    return cost.quantize(_CENTS)


def record_query_usage(
    tenant: Tenant,
    *,
    model: str,
    input_tokens: int,
    output_tokens: int,
) -> UsageRecord:
    """Record one query's token usage and estimated cost for ``tenant``.

    Establishes tenant context explicitly (see the module docstring): the caller is typically the tail
    of a streamed response, where no ambient tenant remains. The cost is computed and stored at write
    time, so the price then in effect is baked into the row and a later price change cannot silently
    rewrite history.
    """
    cost = estimated_cost_usd(input_tokens, output_tokens, model)
    with tenant_context(tenant):
        return UsageRecord.objects.create(
            tenant=tenant,
            kind=UsageRecord.Kind.QUERY,
            model_name=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_cost_usd=cost,
        )


def usage_summary(
    *, start: datetime | None = None, end: datetime | None = None
) -> dict[str, object]:
    """Aggregate the **current tenant's** usage over ``[start, end]`` (default: the last 30 days).

    Reads through ``UsageRecord.objects`` — the tenant-scoped manager — so the totals can only ever
    cover the active tenant, and an unscoped call raises rather than summing across tenants. Empty
    ranges report zeros (never ``None``), so a caller never has to special-case "no usage yet".
    """
    end = end or timezone.now()
    start = start or (end - DEFAULT_WINDOW)
    # One statement for all four numbers, so the count can never come from a different snapshot than
    # the sums (and it is one round trip, not two).
    totals = UsageRecord.objects.filter(created_at__gte=start, created_at__lte=end).aggregate(
        requests=Count("id"),
        input_tokens=Sum("input_tokens"),
        output_tokens=Sum("output_tokens"),
        estimated_cost_usd=Sum("estimated_cost_usd"),
    )
    return {
        "start": start,
        "end": end,
        "requests": totals["requests"] or 0,
        "input_tokens": totals["input_tokens"] or 0,
        "output_tokens": totals["output_tokens"] or 0,
        "estimated_cost_usd": totals["estimated_cost_usd"] or Decimal("0"),
    }


def utc_today() -> date:
    """Today's date in UTC, the calendar :func:`usage_by_day` counts days in."""
    return timezone.now().astimezone(dt_timezone.utc).date()


def usage_by_day(*, first: date, last: date) -> dict[str, object]:
    """The **current tenant's** usage for each UTC day from ``first`` to ``last`` inclusive (#27).

    Every day of the range has an entry, zeros included, so a chart draws the range as it was: a day
    with no answers cost nothing, and leaving it out would draw a line straight across it. Days are
    UTC calendar days, because a stored row has an instant and no reader's timezone.

    One grouped statement produces every day, and the totals are the sum of those days, so they can
    never disagree with the days they summarise. Like :func:`usage_summary` it reads through the
    tenant-scoped manager, so it raises rather than summing across tenants when no tenant is active.
    """
    if last < first:
        raise ValueError("'first' must not be after 'last'.")
    start = datetime.combine(first, time.min, tzinfo=dt_timezone.utc)
    end = datetime.combine(last + timedelta(days=1), time.min, tzinfo=dt_timezone.utc)
    by_day = _daily_rows(UsageRecord.objects.all(), start=start, end=end)

    zero: dict[str, object] = {
        "requests": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        # At the column's six places, like every summed day, so each entry prints the same way.
        "estimated_cost_usd": Decimal(0).quantize(_CENTS),
    }
    days = [
        {"date": day, **by_day.get(day, zero)}
        for day in (first + timedelta(days=n) for n in range((last - first).days + 1))
    ]
    totals = {
        key: sum((d[key] for d in days), start=start_value) for key, start_value in zero.items()
    }
    return {"first": first, "last": last, "days": days, "totals": totals}


def _daily_rows(
    records: QuerySet[UsageRecord], *, start: datetime, end: datetime
) -> dict[date, dict[str, object]]:
    """Sum ``records`` per UTC day in ``[start, end)``, keyed by day. Days with no rows are absent.

    Takes the queryset rather than reading ``UsageRecord.objects`` itself so a test can hand it the
    unscoped manager and show that row-level security alone still keeps the sums to one tenant.
    """
    grouped = (
        records.filter(created_at__gte=start, created_at__lt=end)
        .annotate(day=TruncDate("created_at", tzinfo=dt_timezone.utc))
        .values("day")
        .annotate(
            requests=Count("id"),
            input_tokens=Sum("input_tokens"),
            output_tokens=Sum("output_tokens"),
            estimated_cost_usd=Sum("estimated_cost_usd"),
        )
    )
    by_day: dict[date, dict[str, object]] = {}
    for row in grouped:
        day = row.pop("day")
        # Never None: a day is grouped only when it has rows, and the cost column is NOT NULL.
        by_day[day] = {**row, "estimated_cost_usd": row["estimated_cost_usd"].quantize(_CENTS)}
    return by_day
