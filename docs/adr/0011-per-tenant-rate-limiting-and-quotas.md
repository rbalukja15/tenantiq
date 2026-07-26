# ADR-0011 — Per-tenant rate limiting & quotas

- **Status:** Accepted
- **Date:** 2026-07-24

## Context

Until now there was no rate limiting or quota anywhere. An authenticated client could issue unlimited
uploads and — once grounded generation shipped (#15) — drive **unbounded LLM spend** through
`POST /api/query`. #25 (M6) will make the API publicly reachable. Some protection against capacity
abuse must exist *before* the query endpoint is deployed anywhere public (#49).

The project's defining invariant is tenant isolation ("isolation is sacred", CLAUDE.md). Capacity is
part of that invariant: one tenant must not be able to degrade another by exhausting a shared limit.
So the interesting question is not "do we rate-limit" but "what is the *unit* of limiting, and how do
burst and volume differ."

Three design forks had to be resolved.

**What is the limiting unit — user, IP, or tenant?** Per-*user* would let a tenant with many users
run up unbounded aggregate spend (each user under the cap, the tenant far over). Per-*IP* is an edge
concern (a WAF/proxy property) and is meaningless for authenticated server-to-server traffic behind
NAT. Per-*tenant* is the only unit that matches the isolation model and actually bounds a customer's
spend.

**How to bound burst vs volume?** A per-minute rate limits *burst* (a hammering loop) but says
nothing about sustained daily cost; a daily/monthly cap limits *volume* but not a thundering burst.
These are different controls with different windows.

**Where does throttle state live?** DRF throttling counts requests in Django's cache. With more than
one worker process, a per-process in-memory cache means each worker enforces the limit independently
(N workers ⇒ N× the intended rate). Correct multi-worker throttling needs a *shared* cache.

## Decision

**Throttling is keyed on the tenant, never the user or IP.** `app.throttling` provides throttles
whose cache key is `request.tenant.id`, so a tenant's whole workforce shares one budget and no tenant
can consume another's allowance. Throttles run after authentication and permissions (DRF `initial()`
order), so `request.tenant` is always present when they execute.

**Two families of control, both per tenant:**

- **Burst — sliding-window rate throttles.** `TenantQueryRateThrottle` / `TenantUploadRateThrottle` /
  `TenantReadRateThrottle` subclass DRF's `SimpleRateThrottle`, re-keyed on the tenant. Three
  **separate scopes** with independent budgets: `query` (LLM-backed, expensive) < `upload` (enqueues
  worker load) < `read` (cheap). Rates are configuration — `REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]`,
  env-overridable (`TENANTIQ_THROTTLE_{QUERY,UPLOAD,READ}`), default `30`/`20`/`120` per minute. The
  base class reads the rate *fresh* from `api_settings` (DRF binds `THROTTLE_RATES` at import, which
  would otherwise make the rate un-reconfigurable at runtime and in tests).
- **Volume — fixed calendar-window quotas.** `TenantQueryDailyQuotaThrottle` /
  `TenantQueryMonthlyQuotaThrottle` count a tenant's total query requests in the current calendar
  day / month and deny past a configured limit, resetting at the window boundary.
  `TENANTIQ_QUERY_DAILY_QUOTA` (default `1000`) and `TENANTIQ_QUERY_MONTHLY_QUOTA` (default `0`);
  `0` means unlimited — the hook is present but disabled. This is the "quota hooks (simple counters)"
  half of #49; precise per-token / per-dollar accounting is #17.

**Quota gate vs. consume.** DRF's `check_throttles` runs *every* throttle on a view with no
short-circuit, so a request already rejected (429) by the burst-rate throttle would still execute the
quota throttles. If the quota counted inside `allow_request`, those zero-work rejected requests would
burn quota — a client retrying on 429 could drain its own daily quota and lock itself out for the day.
So the quota separates the two: `allow_request` only *gates* (compares the counter to the limit), and
a separate `record` *consumes* it; the query view calls `record` only once the request has cleared
every throttle and is actually being served. A request rejected by any throttle therefore never
charges quota, and a bad-request (400) does not either.

A denied request yields **429 with `Retry-After`**: DRF's exception handler sets the header from the
throttle's `wait()`, which returns seconds-until-the-window-frees for both families (DRF takes the max
across all denying throttles).

**Throttle state lives in a shared cache.** The default cache is Redis in production (reusing
`REDIS_URL`, or `CACHE_URL`), so all workers count against one bucket. Under pytest — and on a dev box
with no cache URL — it falls back to a local in-memory cache (single process, still correct there; the
suite stays hermetic, and an autouse fixture clears it between tests so counts can't leak).

**Rejected / deferred:**

- **Per-user or per-IP limiting** — rejected as the primary unit for the reasons above. Per-IP edge
  throttling (a WAF concern) can still be layered on at deploy (#25); it complements, not replaces,
  per-tenant limits.
- **DRF's `ScopedRateThrottle` / `UserRateThrottle`** — they key on user/IP; subclassing
  `SimpleRateThrottle` with a tenant key was the smallest change that gets per-tenant semantics.
- **A sliding-log daily quota** (a `1000/day` `SimpleRateThrottle`) — it stores up to N timestamps
  per tenant and has no meaningful "resets at midnight". A fixed calendar-window counter is cheaper
  and gives an honest reset time for `Retry-After`.
- **Throttling the unauthenticated path at the app layer** — DRF rejects an unauthenticated request
  with 401 at the permission layer *before* throttles run, so an app-level anon throttle would never
  fire. Bounding unauthenticated request cost (the "each costs a DB lookup" concern in #49) belongs at
  the edge/WAF and lands with the public deploy (#25).

## Consequences

- **Easier.** LLM spend and worker load are bounded per tenant before any public exposure; capacity
  abuse by one tenant cannot touch another (isolation now covers capacity, proved by a test). Limits
  are pure configuration — an operator retunes rates/quotas via environment, no code change. The
  acceptance criterion (a tenant hammering `/api/query` is throttled by its own budget while a second
  tenant is unaffected) runs hermetically against the real endpoint.
- **Harder / accepted costs.**
  - *Shared-cache dependency, fail-open.* Correct multi-worker throttling depends on a reachable
    shared cache (Redis). Django's built-in `RedisCache` does **not** swallow backend errors, so both
    throttle families catch a cache-unavailable error explicitly and **fail open** (allow the request)
    rather than 500 the API — a throttle must never be a hard dependency for serving traffic. The cost
    of failing open is that limits (including query spend) are not enforced during a cache outage,
    bounded by the outage's duration; we judge availability the right trade there. A dev box without
    Redis gets per-process (not global) limits.
  - *Coarse quota counters.* The volume quota is a check-then-record counter; under concurrent
    requests at the boundary it can admit one or two over the limit. Acceptable for a volume cap
    ("simple counters", #49); precise accounting is #17.
  - *`Retry-After` is advisory.* A misbehaving client can ignore it and keep getting 429s; the point
    is to shed load and signal well-behaved clients, not to enforce client backoff.
  - *Defaults are generous.* The per-minute rates and daily quota are starting points; real limits are
    a per-deployment tuning exercise informed by #17's cost data.
- **Sequencing.** This is a prerequisite for the public deploy (#25) and complements the ingestion
  work bounds (#47): #47 bounds a *single* document's cost inside the worker; #49 bounds *request
  volume* at the API edge. Together they close both halves of capacity abuse. #17 will layer precise
  cost/token accounting on top of these request-count quotas.
