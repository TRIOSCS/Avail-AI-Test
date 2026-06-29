"""Shared rate limiter with Redis storage and in-memory fallback.

Two limiters live here, both Redis-backed with an in-memory fallback so enforcement
survives a Redis outage (limits just stop being shared across workers in that mode):

- ``limiter`` — the slowapi IP-based HTTP limiter (storage resolved at import time).
- ``check_rate_limit`` — a per-(user, bucket) fixed-window counter for application-level
  outreach throttles (CDM click-to-call / click-to-contact / call-outcome). It rides the
  shared Redis substrate (``app.cache.intel_cache``) via an atomic ``INCR``, so a per-user
  limit holds across every worker process and across restarts within the window.
"""

import time
from threading import Lock

from loguru import logger
from slowapi import Limiter
from slowapi.util import get_remote_address

from .cache.intel_cache import _get_redis
from .config import settings


def _resolve_storage() -> str | None:
    """Try Redis for distributed rate limiting; fall back to in-memory."""
    if settings.cache_backend != "redis" or not settings.redis_url:
        return None
    try:
        import redis as redis_lib

        r = redis_lib.from_url(settings.redis_url, socket_connect_timeout=2)
        r.ping()
        logger.info("Rate limiter using Redis storage")
        return settings.redis_url
    except Exception:
        logger.warning(
            "Redis unavailable — rate limiter using in-memory storage (limits won't be shared across workers)"
        )
        return None


limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[settings.rate_limit_default],
    enabled=settings.rate_limit_enabled,
    storage_uri=_resolve_storage(),
)


# ── Per-(user, bucket) outreach rate limiter ───────────────────────────
# Fixed-window counter shared across processes via the Redis substrate. The window is
# encoded in the key (``…:{window_index}``) so the counter resets automatically each
# window without any pruning; the key is also given a TTL as a belt-and-braces cleanup.
# Degrades to a per-process in-memory counter when Redis is unavailable — same posture
# as the slowapi ``limiter`` above (enforcement continues, just not shared across
# workers). Fixed window, not sliding: this is the shape the counter substrate supports
# atomically (mirrors ``intel_cache.incr_count``).

_OUTREACH_RL_PREFIX = "rl:outreach:"

# In-memory fallback store: ``{user_id}:{bucket}`` -> (window_index, count).
# Used only when Redis is down; reset by ``reset_rate_limit_state`` (tests).
_fallback_lock = Lock()
_fallback_counts: dict[str, tuple[int, int]] = {}


def _now() -> float:
    """Current wall-clock time — a seam so tests can advance the window."""
    return time.time()


def _check_in_memory(key_base: str, window_index: int, limit: int) -> bool:
    """Per-process fixed-window fallback used when Redis is unavailable."""
    with _fallback_lock:
        prev = _fallback_counts.get(key_base)
        count = prev[1] + 1 if prev is not None and prev[0] == window_index else 1
        _fallback_counts[key_base] = (window_index, count)
    return count <= limit


def check_rate_limit(user_id: int, bucket: str, limit: int, window_seconds: int = 60) -> bool:
    """Return True if the caller is within the rate limit, False if it is exceeded.

    Atomic fixed-window counter keyed per ``(user_id, bucket, time-window)``. The counter
    lives in the shared Redis substrate (``app.cache.intel_cache``), so the per-user limit
    is enforced across all worker processes and across restarts within the window. When
    Redis is unavailable the check degrades to a per-process in-memory counter (mirrors the
    slowapi ``limiter`` fallback): enforcement continues, but the limit stops being shared.

    Semantics: allows up to ``limit`` calls per ``window_seconds`` window, blocks the next.
    """
    window_index = int(_now() // window_seconds)
    key_base = f"{user_id}:{bucket}"

    redis = _get_redis()
    if redis is not None:
        redis_key = f"{_OUTREACH_RL_PREFIX}{key_base}:{window_index}"
        try:
            count = int(redis.incr(redis_key))
            # Bound the key's lifetime (2× window so the tail of a window still expires
            # cleanly). Window correctness comes from the key, not the TTL.
            redis.expire(redis_key, window_seconds * 2)
            return count <= limit
        except Exception as e:
            logger.warning(
                "Outreach rate limiter: Redis error ({}) — falling back to in-memory counter",
                e,
            )

    return _check_in_memory(key_base, window_index, limit)


def reset_rate_limit_state() -> None:
    """Clear the in-memory fallback counter.

    Test-support hook.
    """
    with _fallback_lock:
        _fallback_counts.clear()
