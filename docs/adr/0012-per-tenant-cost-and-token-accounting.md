# ADR-0012 — Per-tenant cost & token accounting

- **Status:** Accepted
- **Date:** 2026-07-26

## Context

Running AI in production means knowing what each tenant costs (#17). #49 landed *limits* — per-tenant
burst rates and daily/monthly query **counts** — but a request count is a poor proxy for spend: two
queries can differ by an order of magnitude in tokens, and a count can't answer "what did Acme cost
us last month?" Without per-tenant cost there is no basis for pricing, no way to spot a tenant whose
usage is unprofitable, and no data to tune #49's limits with.

Three design forks had to be resolved.

**Where do token counts come from?** The ideal source is the provider's own usage numbers (Anthropic
returns `usage.input_tokens` / `output_tokens`). But the answer path is **streaming** (#48, ADR-0009):
`LLMClient.stream` yields text deltas and exposes no usage payload, and the fake LLM used by the
hermetic suite has no usage concept at all. The options were to (a) change the `LLMClient` protocol to
surface provider usage, threading it through every backend and the fake, or (b) **estimate** tokens
from the text we already have — the assembled prompt and the accumulated answer.

**Where is usage recorded?** Recording must not undo ADR-0009's guarantee that no DB transaction is
held open across the slow model call. And the streamed body finishes *after* `ATOMIC_REQUESTS` has
committed and after the middleware cleared the tenant contextvar, so there is no ambient tenant at the
moment the answer completes.

**How is money represented?** Float or Decimal.

## Decision

**Estimate tokens; record one row per served query.** `app.usage.estimate_tokens` reuses the
chars-per-token heuristic from chunking (ADR-0003) over the assembled prompt (input) and the
accumulated answer text (output). This keeps the `LLMClient` protocol untouched, keeps the suite
hermetic (no network, no key), and works identically for Anthropic, Ollama, and the fake. The cost is
accuracy: these are **estimates**, named as such in the model field (`estimated_cost_usd`) and the API
response. Exact provider counts are a follow-up that can replace the estimate without changing the
schema or the endpoint — see *Consequences*.

**`UsageRecord` is a tenant-owned model.** It carries `kind`, `model_name`, `input_tokens`,
`output_tokens`, `estimated_cost_usd`, `created_at`, with an index on `(tenant, created_at)` for the
reporting access pattern. Being a `TenantOwnedModel` it inherits both isolation layers, and migration
`0012` gives it the same **forced Postgres RLS** policy as documents and chunks — cost data leaks a
tenant's usage volume and behaviour, so it deserves the same backstop as document content. (#50's
meta-guard enumerates tenant-owned models, so a missing RLS migration fails the suite.)

**Recording happens in the response generator's tail, not in `generation.py`.** `app.generation` is
deliberately DB-free (it operates on an already-retrieved context), so the *view* wraps the SSE stream:
it accumulates the streamed text and, in a `finally`, writes one usage row. Consequences of that
placement, all deliberate:

- The write happens **after the last token**, never during the model call — ADR-0009's invariant holds.
  The `test_query_endpoint_holds_no_db_transaction_open_during_generation` acceptance test keeps its
  original strength: it still asserts **zero** queries while frames are being produced (checked after
  every frame, since the accounting write happens only once the generator is exhausted), and adds that
  exactly one statement touches the usage table — one row per request, never per token. Both
  assertions were mutation-tested: a per-token write and a double write each make it fail.
- `record_query_usage` **establishes tenant context itself** (`tenant_context(tenant)`), with the
  tenant captured from the request before streaming began — a recorder that assumed an ambient tenant
  would silently write nothing or raise.
- Using `finally` means a client that disconnects mid-stream, or a model that fails after emitting
  tokens, is still charged for the tokens actually produced. Spend that happened is spend that is
  recorded.
- A **refusal is not charged.** With no retrieved context the model is never called (#15), so there is
  nothing to bill; recording a zero-cost row would inflate the request count with spend that never
  happened. A 400 (bad request) likewise records nothing. Neither is a **generation that produced
  nothing** — a bad key or a provider outage yields an error frame with no tokens, and charging the
  prompt there would let a client's retry loop manufacture spend during an outage. A generation that
  failed *after* emitting tokens **is** charged for them: those tokens were really produced.
- The recorded model is the one the **serving client reports** (`llm.model`), not the configured
  Anthropic name, and prices are per model (`TENANTIQ_LLM_PRICES`). Without this, a deployment with no
  Anthropic key — the documented default, which answers from the local Ollama model — would report
  Anthropic-priced spend for a model that costs nothing per token. Local and fake models ship priced
  at zero; an unlisted model falls back to the global price pair.
- The **price in effect at write time is baked into the row.** Cost is computed and stored on write,
  not recomputed at read time, so a later price change cannot silently rewrite history.

**Money is `Decimal`, never `float`.** The column is `DecimalField(max_digits=12, decimal_places=6)` —
sub-cent precision, because a single cheap request costs far less than a cent, with room for a large
monthly total. Prices are configuration in USD per **million** tokens
(`TENANTIQ_LLM_PRICE_{INPUT,OUTPUT}_PER_MTOK`), read as `Decimal`; input and output are priced
separately because output tokens are normally dearer. The API serializes the cost as a **string**, so a
JSON float can't reintroduce the representation error the Decimal column exists to prevent.

**`GET /api/usage?start=&end=`** returns the caller's `requests`, `input_tokens`, `output_tokens`, and
`estimated_cost_usd` for the window (default: last 30 days) in a **single aggregate query**, so the
count can never come from a different snapshot than the sums. It reads through the tenant-scoped
manager. `start`/`end` accept ISO-8601 timestamps or bare calendar dates; a malformed range is a **400,
never a 500**, and so is an inverted one — validated against the *resolved* window, so a future `start`
with no `end` is rejected rather than silently reporting "no spend". Two parsing details that would
otherwise be quiet data bugs:

- A bare `end` **date is inclusive of that whole day**: `?end=2026-07-31` means "through the 31st".
  Resolving it to midnight would silently drop the final day from every month report. Note this cannot
  be implemented by letting `parse_datetime` fail — since Django 4.1 it delegates to
  `datetime.fromisoformat`, which parses a bare date as midnight — so date-only input is detected
  explicitly by the absence of a time separator.
- In a query string `+` decodes to a space, so an unencoded ISO offset (`...12:00:00+00:00`, exactly
  what `datetime.isoformat()` produces) arrives mangled — the parser retries with the offset restored
  instead of rejecting valid-looking input.

## Consequences

- **Easier.** Per-tenant cost is queryable for any time range, which is the basis for pricing,
  margin analysis, and tuning #49's limits with real data. Because the row records `model_name` and a
  stored cost, totals stay attributable across model and pricing changes. The whole path is testable
  without a network or an API key.
- **Harder / accepted costs.**
  - *Estimates, not invoices.* Token counts come from a chars-per-token heuristic, so a total will
    drift from the provider's bill (tokenizer differences, system overhead, cache reads/writes, and
    any provider-side retries are invisible). It is a cost **signal**, deliberately labelled
    `estimated_*`, not an accounting system of record. Replacing it with exact counts means extending
    `LLMClient` to surface provider usage on the streaming path and writing those numbers into the
    same columns — no schema or API change.
  - *Query path only.* Embedding cost at ingest is real but unmetered here; `kind` exists so it can be
    added without a migration. Ingestion cost is also driven by document size, which #47 already
    bounds.
  - *One extra write per query.* A small INSERT after each served answer. Negligible next to the model
    call, but it is DB work on the hot path; if it ever matters, batching or an async sink is the move.
  - *Accounting failure loses data rather than breaking the answer.* By the time recording runs, every
    token has been sent. A failure there (DB down, misconfigured price) is caught and logged at
    exception level, never raised: truncating or erroring a response the client already received in
    full would be strictly worse than losing one usage row. The trade is explicit — accounting is
    best-effort, and a persistent failure shows up as logged exceptions plus a gap in the data, not as
    failed queries.
  - *Sync ORM inside a streaming generator.* The write happens in the response body's iterator. Under
    WSGI (`runserver` today, gunicorn later) this is fine: Django emits `request_finished` — which
    closes connections — only after the body is exhausted, so the write uses the request's still-open
    connection. If the backend is ever served over **ASGI** (`ASGI_APPLICATION` is configured), sync
    ORM access from the async iteration context would raise `SynchronousOnlyOperation`, so this hook
    would need `sync_to_async` (or an async sink) at that point. Noted for M6's productionization.
  - *No aggregation rollups.* The endpoint aggregates raw rows per call. Fine at this volume with the
    `(tenant, created_at)` index; a high-volume deployment would want periodic rollups or a retention
    policy, since rows accumulate indefinitely.
