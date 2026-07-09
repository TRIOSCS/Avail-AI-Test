"""Lazy Redis probe with automatic re-probe + downgrade observability.

Both the search-result cache (``app.search_service._get_search_redis``) and the intel
cache (``app.cache.intel_cache._get_redis``) lazily connect to Redis and fall back to a
degraded path (no cache / PostgreSQL) when it is unavailable. Historically that fallback
was *sticky*: the first failed connect disabled Redis for the entire process lifetime,
with no recovery and no signal — a Redis blip during startup meant every request ran
cache-less until the app was restarted, invisibly.

``RedisProbe`` wraps the connect in a re-probe loop. While degraded it retries the real
Redis at most once per ``REPROBE_INTERVAL_S`` and recovers automatically when Redis
returns, emitting a Prometheus gauge/counter (``redis_degraded`` /
``redis_downgrade_total``) plus structured logs so the degraded state is observable.

The probe is strictly best-effort: ``get()`` never raises (a probe or metric failure is
swallowed), so it is safe to call on the request hot path.

Called by: app.cache.intel_cache._get_redis, app.search_service._get_search_redis
Depends on: time (monotonic clock), loguru, app.prometheus_metrics (lazy, optional)
"""

from __future__ import annotations

import time
from collections.abc import Callable

from loguru import logger

# While degraded, re-attempt the real Redis at most this often. A probe carries a socket
# connect timeout, so we don't want to pay it on every cache call — but 30s is frequent
# enough that recovery is near-automatic. Referenced (not captured) so tests can patch it.
REPROBE_INTERVAL_S = 30.0


class RedisProbe:
    """Lazy Redis client that re-probes after a failure and reports its degraded state.

    ``connect`` is a zero-arg callable that returns a live client, or ``None`` to mean
    "intentionally disabled" (e.g. TESTING, or a non-Redis cache backend), or *raises* to
    mean "transiently unavailable — retry later". A returned ``None`` permanently disables
    the probe (no retries, no metric); a raised exception triggers the re-probe loop.
    """

    def __init__(self, subsystem: str, connect: Callable[[], object]):
        self._subsystem = subsystem
        self._connect = connect
        self._client: object | None = None
        self._disabled = False  # connect() returned None → permanently off (no re-probe)
        self._degraded = False  # a probe failed and we have not yet recovered
        self._last_probe = 0.0  # monotonic time of the last connect attempt (0 = never)

    def get(self) -> object | None:
        """Return a live Redis client, or ``None`` when degraded/disabled.

        Never raises. On the degraded path it re-probes at most once per
        ``REPROBE_INTERVAL_S`` and recovers transparently when Redis returns.
        """
        if self._disabled:
            return None
        if self._client is not None:
            return self._client

        now = time.monotonic()
        if self._last_probe and (now - self._last_probe) < REPROBE_INTERVAL_S:
            return None  # inside the backoff window — stay degraded without hammering
        self._last_probe = now

        try:
            client = self._connect()
        except Exception as e:  # transient — schedule a re-probe, stay degraded
            self._record_downgrade(e)
            self._client = None
            return None

        if client is None:  # intentionally disabled — stop probing entirely
            self._disabled = True
            return None

        self._client = client
        if self._degraded:
            self._record_recovery()
        return client

    def _record_downgrade(self, exc: Exception) -> None:
        """Mark the subsystem degraded and emit metric/log (best-effort).

        The counter increments only on the healthy->degraded transition (so it counts
        outage *events*, not re-probe attempts); the gauge tracks current state. The
        transition logs at WARNING; subsequent failed re-probes log at DEBUG so a long
        outage does not spam the log every 30s.
        """
        was_degraded = self._degraded
        self._degraded = True
        try:
            from app.prometheus_metrics import REDIS_DEGRADED, REDIS_DOWNGRADE_TOTAL

            REDIS_DEGRADED.labels(subsystem=self._subsystem).set(1)
            if not was_degraded:
                REDIS_DOWNGRADE_TOTAL.labels(subsystem=self._subsystem).inc()
        except Exception:  # metrics are optional — never let them break the probe
            pass
        log = logger.warning if not was_degraded else logger.debug
        log("Redis degraded for {} (fallback active): {}", self._subsystem, exc)

    def _record_recovery(self) -> None:
        """Clear the degraded state and emit metric/log (best-effort)."""
        self._degraded = False
        try:
            from app.prometheus_metrics import REDIS_DEGRADED

            REDIS_DEGRADED.labels(subsystem=self._subsystem).set(0)
        except Exception:
            pass
        logger.info("Redis recovered for {} — cache re-enabled", self._subsystem)

    def reset(self) -> None:
        """Forget the cached client + probe state.

        Test-support hook.
        """
        self._client = None
        self._disabled = False
        self._degraded = False
        self._last_probe = 0.0
