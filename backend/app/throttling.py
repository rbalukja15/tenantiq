"""Rate limiting & quotas (#49, ADR-0011; extended by #18).

Two families of throttle for *authenticated* traffic, both keyed on the **tenant** (never the user,
never globally), so tenant isolation — the project's core invariant — extends to capacity: one tenant
hammering an endpoint can exhaust only its *own* budget, never another tenant's.

Plus one throttle for the single **unauthenticated** endpoint (tenant discovery, #18), which has no
tenant to key on and is bucketed per requested slug instead — see
:class:`PublicDiscoveryRateThrottle`. The rule below about throttles never running for anonymous
requests holds for every endpoint *except* that one, which is deliberately ``AllowAny``.

- **Rate throttles** (:class:`TenantQueryRateThrottle`, ``…Upload…``, ``…Read…``) bound *burst*: a
  sliding-window request rate per tenant per scope, so query (LLM-backed, expensive) / uploads /
  reads each carry their own limit. Built on DRF's :class:`SimpleRateThrottle` but re-keyed on the
  tenant and reading the configured rate fresh (so runtime/test reconfiguration is honored).
- **Quota throttles** (:class:`TenantQueryDailyQuotaThrottle`, ``…Monthly…``) bound *volume*: a
  per-tenant counter over a fixed calendar window (day / month). This is the throttling half of the
  quota hooks in #49 — a simple counter, configuration-driven (``0`` = unlimited). Precise
  per-token / per-dollar cost accounting is #17; this only caps request *count*.

**Gate vs. consume.** DRF's ``check_throttles`` runs *every* throttle on a view with no
short-circuit, so if quota counting happened inside ``allow_request`` a request already rejected
(429) by the burst-rate throttle would still burn quota — a client retrying on 429 could drain its
own daily quota with zero-work requests and lock itself out for the day. So the quota separates the
two: :meth:`allow_request` only *gates* (compares the counter to the limit), and :meth:`record`
*consumes* (increments) — and the view calls ``record`` only for a request it actually serves.

**Fail open.** Throttle counters live in the cache (Redis in production). A throttle must never be a
hard dependency for serving traffic: if the cache is unreachable, both families **fail open** (allow
the request) rather than 500 the API — availability over strictness.

Throttles run after authentication and permissions (DRF ``initial()`` order), so an unauthenticated
request is already rejected with 401 before any throttle sees it. When a throttle denies, DRF raises
``Throttled(wait)`` and its exception handler sets ``Retry-After`` from ``wait()`` automatically.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone
from rest_framework.settings import api_settings
from rest_framework.throttling import BaseThrottle, SimpleRateThrottle

logger = logging.getLogger(__name__)

# Exceptions that mean "the cache backend is unreachable" — treated as fail-open, not a 500. redis-py
# raises RedisError (a ConnectionError subclass) on outage; OSError covers socket-level failures. The
# import is guarded so a non-Redis cache backend doesn't force a redis dependency at import.
try:
    from redis.exceptions import RedisError

    _CACHE_UNAVAILABLE: tuple[type[BaseException], ...] = (OSError, RedisError)
except Exception:  # pragma: no cover - redis is a hard dependency, but stay defensive
    _CACHE_UNAVAILABLE = (OSError,)


def _tenant_ident(request) -> str | None:
    """The throttling identity: the tenant id, or ``None`` when there is no tenant (don't throttle)."""
    tenant = getattr(request, "tenant", None)
    return str(tenant.id) if tenant is not None else None


# --- rate throttles (sliding window, per tenant) --------------------------------------------------


class _ConfiguredRateThrottle(SimpleRateThrottle):
    """Behaviour shared by every rate throttle here, independent of what the bucket is keyed on.

    Two differences from the DRF base:

    - :meth:`get_rate` reads the configured rate *fresh* from DRF's settings on each instantiation.
      ``SimpleRateThrottle`` binds ``THROTTLE_RATES`` at import time, which would make the rate
      un-overridable at runtime (and in tests). Reading ``api_settings`` fresh keeps the rate true
      configuration.
    - :meth:`allow_request` fails open on a cache outage, so a Redis blip degrades to "no throttling"
      instead of 500-ing every request.
    """

    def get_rate(self) -> str | None:
        return api_settings.DEFAULT_THROTTLE_RATES.get(self.scope)

    def allow_request(self, request, view) -> bool:
        try:
            return super().allow_request(request, view)
        except _CACHE_UNAVAILABLE:  # cache down -> fail open, don't take the API down
            logger.warning("throttle cache unavailable; failing open for scope=%s", self.scope)
            return True


class _TenantScopedRateThrottle(_ConfiguredRateThrottle):
    """A rate throttle bucketed by tenant + scope rather than by user or IP.

    :meth:`get_cache_key` keys on ``request.tenant.id``, so the bucket is the tenant, not the
    individual user — a tenant's whole workforce shares (and is bounded by) one budget, and no
    tenant can reach into another's bucket.
    """

    def get_cache_key(self, request, view) -> str | None:
        ident = _tenant_ident(request)
        if ident is None:
            return None  # no tenant -> not rate-limited here (the 401 already happened)
        return self.cache_format % {"scope": self.scope, "ident": ident}


class TenantQueryRateThrottle(_TenantScopedRateThrottle):
    """Burst limit for the LLM-backed query endpoint — the expensive path."""

    scope = "query"


class TenantUploadRateThrottle(_TenantScopedRateThrottle):
    """Burst limit for document uploads / re-ingestion triggers (each enqueues worker load)."""

    scope = "upload"


class TenantReadRateThrottle(_TenantScopedRateThrottle):
    """Burst limit for cheap read endpoints (listing documents, identity)."""

    scope = "read"


# --- public (pre-authentication) rate throttle ----------------------------------------------------


class PublicDiscoveryRateThrottle(_ConfiguredRateThrottle):
    """Burst limit for the unauthenticated tenant-discovery endpoint (#18, ADR-0013 §2).

    Discovery is the one route a caller reaches *before* it has a token, so there is no tenant to key
    on — and the tenant-scoped throttles above deliberately return a ``None`` cache key in that case,
    which DRF treats as "do not throttle". Reusing one of them here would have left the project's
    only public endpoint completely unbounded.

    **The bucket is the requested slug, not the client.** Keying on the client IP looks like the
    obvious choice and is wrong here: under the BFF (ADR-0013 §1) the browser never calls this
    endpoint — the Next server does, on the browser's behalf — so Django sees a single
    ``REMOTE_ADDR`` for *every* discovery request in production. An IP key would therefore be one
    global bucket, and a single anonymous flood would deny login to every tenant at once. Keying on
    the slug keeps the blast radius to the tenant actually under attack, which is the same isolation
    promise the rest of the system makes: one tenant's traffic can never degrade another's.

    Two honest limits. An attacker who *rotates* slugs spreads across buckets and is not bounded by
    this at all — accepted, because ADR-0013 already accepts slug enumeration and this is a single
    indexed row lookup returning public values. And a determined flood against one slug denies that
    tenant's logins; per-client limiting is not expressible in Django under a BFF and belongs at the
    edge if it is ever wanted.

    The slug is hashed to bound the **key size**, not to obfuscate it: the view does not length-limit
    the raw query parameter, and the ident is interpolated straight into the cache key — which lands
    in the same Redis that serves as the Celery broker. Hashing keeps an oversized parameter from
    turning anonymous traffic into broker memory pressure.
    """

    scope = "discovery"

    def get_cache_key(self, request, view) -> str | None:
        slug = (request.query_params.get("slug") or "").strip().lower()
        ident = hashlib.sha256(slug.encode()).hexdigest()[:32]
        return self.cache_format % {"scope": self.scope, "ident": ident}


# --- quota throttles (fixed calendar window, per tenant) ------------------------------------------


class _TenantFixedWindowQuotaThrottle(BaseThrottle):
    """A per-tenant volume cap over a fixed calendar window (``day`` or ``month``).

    Unlike the sliding-window rate throttles, this counts a tenant's *served* requests in the current
    calendar day/month and denies once the configured limit is reached, resetting at the window
    boundary. The limit is configuration (:meth:`get_limit`); ``0`` (or negative) means unlimited —
    the hook is present but disabled.

    Gate/consume are separate (see the module docstring): :meth:`allow_request` only compares the
    counter to the limit; :meth:`record` increments it, and the view calls ``record`` only for a
    request it actually serves — so a request rejected by another throttle never burns quota. The
    counter is a cache value with a TTL to the window's end. It is deliberately coarse ("simple
    counters", per #49): a check-then-record across concurrent requests can admit one or two over the
    limit. That is acceptable for a volume cap; precise accounting is #17. On a cache outage both
    methods fail open (allow / skip).
    """

    period: str  # "day" or "month"
    scope: str  # names the cache bucket, e.g. "query_daily"

    def get_limit(self) -> int:
        raise NotImplementedError(".get_limit() must be overridden")

    def _window(self, now: datetime) -> tuple[str, datetime]:
        """Return (stamp, reset): the current window's cache stamp and when it rolls over."""
        if self.period == "day":
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            reset = start + timedelta(days=1)
            return now.strftime("%Y-%m-%d"), reset
        # month: roll to the 1st of next month at midnight
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        reset = (start + timedelta(days=32)).replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )
        return now.strftime("%Y-%m"), reset

    def _key(self, ident: str, stamp: str) -> str:
        return f"quota_{self.scope}_{ident}_{stamp}"

    def allow_request(self, request, view) -> bool:
        """Gate only — does the tenant have quota left? Never mutates the counter."""
        limit = self.get_limit()
        if limit <= 0:
            return True  # unlimited / disabled hook
        ident = _tenant_ident(request)
        if ident is None:
            return True
        now = timezone.now()
        stamp, reset = self._window(now)
        try:
            count = cache.get(self._key(ident, stamp), 0)
        except _CACHE_UNAVAILABLE:  # cache down -> fail open
            logger.warning("quota cache unavailable; failing open for scope=%s", self.scope)
            return True
        if count >= limit:
            self._wait = max(1, int((reset - now).total_seconds()))
            return False
        return True

    def record(self, request) -> None:
        """Consume one unit — call this only for a request that is actually served."""
        limit = self.get_limit()
        if limit <= 0:
            return  # quota disabled -> nothing to count
        ident = _tenant_ident(request)
        if ident is None:
            return
        now = timezone.now()
        stamp, reset = self._window(now)
        key = self._key(ident, stamp)
        ttl = int((reset - now).total_seconds()) + 60  # a little past the boundary
        try:
            # add() seeds the counter with the window TTL only if absent; incr() is atomic on shared
            # caches. A window-boundary expiry between the two is reseeded.
            cache.add(key, 0, ttl)
            try:
                cache.incr(key)
            except ValueError:
                cache.set(key, 1, ttl)
        except _CACHE_UNAVAILABLE:  # cache down -> fail open (don't charge)
            logger.warning("quota cache unavailable; not recording for scope=%s", self.scope)

    def wait(self) -> float | None:
        return getattr(self, "_wait", None)


class TenantQueryDailyQuotaThrottle(_TenantFixedWindowQuotaThrottle):
    """Per-tenant daily cap on query requests. ``TENANTIQ_QUERY_DAILY_QUOTA`` (0 = unlimited)."""

    period = "day"
    scope = "query_daily"

    def get_limit(self) -> int:
        return int(getattr(settings, "TENANTIQ_QUERY_DAILY_QUOTA", 0) or 0)


class TenantQueryMonthlyQuotaThrottle(_TenantFixedWindowQuotaThrottle):
    """Per-tenant monthly cap on query requests. ``TENANTIQ_QUERY_MONTHLY_QUOTA`` (0 = unlimited)."""

    period = "month"
    scope = "query_monthly"

    def get_limit(self) -> int:
        return int(getattr(settings, "TENANTIQ_QUERY_MONTHLY_QUOTA", 0) or 0)


#: The query quota throttles, in the order the view records them. Kept here so the view charges the
#: same set it is gated by (a new quota only has to be added in one place).
QUERY_QUOTA_THROTTLES: tuple[type[_TenantFixedWindowQuotaThrottle], ...] = (
    TenantQueryDailyQuotaThrottle,
    TenantQueryMonthlyQuotaThrottle,
)
