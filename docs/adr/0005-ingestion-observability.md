# ADR-0005 — Ingestion observability & retry model

- **Status:** Accepted
- **Date:** 2026-07-02

## Context

Ingestion runs asynchronously on Celery (ADR-0003, #11) and embeds chunks before a document is
`READY` (ADR-0004, #12). Async work fails out of sight, so issue #13 requires that ingestion state
be **observable** and that a failed ingestion be **retryable**. Forces at play:

- **Transient vs. permanent failures differ.** An unreadable file (`ParseError`) will never
  succeed; an embedding-backend outage (Ollama unreachable) is temporary. The pipeline already
  encodes this — permanent → `FAILED`, transient → propagate so Celery retries — but a transient
  failure that *exhausts* its retries had nowhere to land: the document stayed stuck in `PROCESSING`
  forever, with no record of why.
- **A status is not a reason.** "Failed" without a message isn't observable enough to act on.
- **`tenant_context` is transactional on Postgres (ADR-0002).** It opens `transaction.atomic()` to
  scope the RLS `SET LOCAL`. So any work done inside it — including a "we are now attempting this"
  write — rolls back when the attempt raises. A naïve attempt counter would be erased by the very
  failures it exists to make visible.
- **Tenant isolation is sacred (ADR-0002).** Retry is a new write path; it must be tenant-scoped and
  proven not to leak.

## Decision

**Track the attempt durably, mark terminal failures via the task's `on_failure`, and expose a
tenant-scoped retry endpoint.**

- **Status vocabulary is unchanged:** `pending / processing / ready / failed`. Issue #13 lists the
  states as `queued / processing / done / failed`; these are the same machine (`pending` ≡ queued,
  `ready` ≡ done). Renaming would churn the #10/#11 migration, serializer, and tests for no semantic
  gain, so the mapping is documented rather than applied.
- **Three observability fields on `Document`** (migration `0009`, plain `AddField`s — `app_document`
  already carries RLS from `0003`, and RLS is row-level, so new columns are covered without a new
  policy):
  - `error` — the surfaced reason for a `FAILED` document (empty otherwise), capped at 2000 chars so
    a stray traceback can't bloat the row.
  - `attempts` — how many times ingestion has run, bumped on each `PROCESSING` transition, so retries
    are visible.
  - `updated_at` (`auto_now`) — last state change, so a document wedged in `PROCESSING` is detectable
    by age.
- **Two-phase `run_ingestion`.** Phase 1 records the attempt (`PROCESSING`, `attempts += 1`, clear
  `error`) in **its own** `tenant_context` transaction, which commits before the risky work. Phase 2
  does the parse → chunk → embed → persist → `READY` work in a second `tenant_context`, atomically.
  A phase-2 transient failure rolls back only the work, leaving an accurate `attempts` count and a
  visible in-progress status; a `ParseError` is recorded as a permanent `FAILED` and returns without
  raising (no retry). Chunk writes + `READY` remain all-or-nothing.
- **Terminal-failure marking lives in the task, not the function.** A transient error propagates out
  of `run_ingestion` so Celery retries it (`autoretry_for`, `retry_backoff`, `max_retries=3`). When
  those retries are exhausted, `IngestTask.on_failure` calls `mark_ingestion_failed(...)` to record
  `FAILED` + the reason. `on_failure` fires only on terminal failure, never on an intermediate retry,
  so a document is never left silently stuck.
- **Retry endpoint.** `POST /api/documents/<id>/retry` looks the document up through the
  tenant-scoped `Document.objects` manager (another tenant's id resolves to **404**, never a
  cross-tenant action), rejects anything not `FAILED` with **409**, and re-enqueues ingestion in
  `transaction.on_commit`. The serializer exposes `status`/`error`/`attempts`/`updated_at`
  read-only, so the full state travels with every document.
- **Retry is idempotent and single-flight.** Re-ingestion clears any prior chunks before writing the
  new set (inside phase 2's transaction), so the unique `(document, index)` constraint can't trip on
  a re-run. The endpoint claims the `FAILED → PENDING` transition with a single conditional
  `UPDATE ... WHERE status = 'failed'`, so two concurrent retries can't both pass a check and enqueue
  duplicate work — only the update that matches wins. And `mark_ingestion_failed` never touches a
  `READY` document, so a late or duplicate failing task can't stomp a success back to `FAILED`.

### Rejected alternatives

- **Rename the states to `queued`/`done`.** Pure churn across an established vocabulary; the mapping
  is documented instead.
- **Do the attempt bookkeeping and the work in one transaction.** Simpler, but on Postgres a
  transient failure would roll the `attempts` increment back with the work — the counter would
  under-count exactly the retried failures it's meant to surface. The two-phase split is the fix.
- **Mark `FAILED` from inside `run_ingestion` on any error.** Would stop Celery from ever retrying a
  transient failure. Terminal marking belongs after the retries, in `on_failure`.
- **A dead-letter queue / unbounded exponential backoff.** Over-engineered for v1. A bounded retry, a
  surfaced reason, and an explicit manual retry cover the need; a DLQ can come later if volume
  warrants it.
- **A `manage.py retry_ingestion` command instead of an endpoint.** The product need is a
  user-triggerable retry, which the API serves; an ops command is deferred (YAGNI).

## Consequences

- `attempts` counts **every** try, including rolled-back transient ones, because phase 1 commits
  before the work — the metric is trustworthy for spotting flaky documents.
- A **hard process kill** (SIGKILL / OOM) between phase 1 and `on_failure` can still leave a document
  in `PROCESSING`, because `on_failure` never runs. `updated_at` makes such stragglers detectable by
  age; a periodic sweeper to reap them is future work, out of scope for #13.
- Ingestion now writes in two transactions rather than one; a successful run is two commits. This is
  the price of a durable attempt record and is acceptable.
- Retry is a new tenant-scoped write path; isolation is proven by a cross-tenant retry test (B cannot
  retry or observe A's document).
- Implemented by #13; consumed by the frontend document view (M4) and the evaluation/ops surfaces
  later.
