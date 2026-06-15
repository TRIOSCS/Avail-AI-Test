"""Intel cache — Redis primary with PostgreSQL fallback.

Used for: company intelligence cards (7-day TTL),
contact enrichment results (14-day TTL), market context (30-day TTL).

Redis is preferred for speed. Falls back to PostgreSQL if Redis is
unavailable (e.g., during development without Docker).
"""

import os
from datetime import datetime, timedelta, timezone
from typing import cast

from loguru import logger
from sqlalchemy import CursorResult, text

from app.database import SessionLocal
from app.utils import json_helpers as json

# Lazy-initialized Redis client
_redis_client = None
_redis_init_attempted = False
_REDIS_PREFIX = "intel:"


def _get_redis():
    """Lazy-init Redis connection.

    Returns client or None if unavailable.
    """
    global _redis_client, _redis_init_attempted

    if _redis_init_attempted:
        return _redis_client

    _redis_init_attempted = True

    if os.environ.get("TESTING"):
        return None

    try:
        from app.config import settings

        if settings.cache_backend == "postgres":
            logger.info("Cache backend set to postgres — skipping Redis")
            return None

        import redis

        _redis_client = redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=3,
            socket_timeout=2,
            retry_on_timeout=True,
        )
        _redis_client.ping()
        logger.info("Redis cache connected: {}", settings.redis_url)
    except Exception as e:
        logger.warning("Redis unavailable, falling back to PostgreSQL cache: {}", e)
        _redis_client = None

    return _redis_client


def get_cached(cache_key: str) -> dict | None:
    """Retrieve cached data if not expired.

    Returns None on miss.
    """
    # Try Redis first
    r = _get_redis()
    if r:
        try:
            data = r.get(f"{_REDIS_PREFIX}{cache_key}")
            if data:
                return json.loads(data)
            return None
        except Exception as e:
            logger.warning("Redis read error for {}: {}", cache_key, e)

    # Fall back to PostgreSQL
    try:
        with SessionLocal() as db:
            row = db.execute(
                text("""
                    SELECT data FROM intel_cache
                    WHERE cache_key = :key AND expires_at > NOW()
                    LIMIT 1
                """),
                {"key": cache_key},
            ).fetchone()

            if row:
                return row[0]  # JSONB column returns as dict
    except Exception as e:
        logger.warning("Cache read error for {}: {}", cache_key, e)
    return None


def set_cached(cache_key: str, data: dict, ttl_days: float = 7) -> None:
    """Store data in cache with TTL."""
    ttl_seconds = int(ttl_days * 86400)

    # Try Redis first
    r = _get_redis()
    if r:
        try:
            r.setex(f"{_REDIS_PREFIX}{cache_key}", ttl_seconds, json.dumps(data))
            return  # Success — skip PG write
        except Exception as e:
            logger.warning("Redis write error for {}: {}", cache_key, e)

    # Fall back to PostgreSQL
    try:
        expires = datetime.now(timezone.utc) + timedelta(days=ttl_days)
        with SessionLocal() as db:
            db.execute(
                text("""
                    INSERT INTO intel_cache (cache_key, data, ttl_days, expires_at, created_at)
                    VALUES (:key, :data, :ttl, :expires, NOW())
                    ON CONFLICT (cache_key) DO UPDATE SET
                        data = :data,
                        ttl_days = :ttl,
                        expires_at = :expires,
                        created_at = NOW()
                """),
                {
                    "key": cache_key,
                    "data": json.dumps(data),
                    "ttl": ttl_days,
                    "expires": expires,
                },
            )
            db.commit()
    except Exception as e:
        logger.warning("Cache write error for {}: {}", cache_key, e)


def get_count(cache_key: str) -> int:
    """Read an integer day-counter (e.g. ``enrichment_worker:web_calls:{date}``).

    Tolerates BOTH value shapes: the plain integer ``incr_count`` writes on Redis, and
    the legacy ``{"count": N}`` dict written by ``set_cached`` (the PG fallback and
    pre-existing rows). Returns 0 on miss/unreadable.
    """
    data = get_cached(cache_key)
    if isinstance(data, dict):
        try:
            return int(data.get("count", 0) or 0)
        except (TypeError, ValueError):
            return 0
    if isinstance(data, (int, float)):
        return int(data)
    return 0


def incr_count(cache_key: str, amount: int = 1, ttl_days: float = 1.0) -> int:
    """Atomically add *amount* to an integer counter; returns the new value.

    Redis path: INCRBY + EXPIRE — atomic across processes, so two concurrent billers
    of the shared daily budget counters (the enrichment worker and the
    backfill_oem_crosswalk drain CLI) never lose each other's updates the way the old
    get_cached → max → set_cached read-modify-write did. Fallback (Redis down, or a
    legacy ``{"count": N}``-shaped value INCRBY rejects): non-atomic read-modify-write
    via ``get_count``/``set_cached`` — single-writer-safe only; in that degraded mode
    cross-process drift is bounded by each biller's in-process floor (callers compose
    the return value with ``max(local + amount, ...)``), and the date-scoped keys
    self-heal at the daily rollover. The returned value is best-effort when BOTH
    backends are down (set_cached swallows failures) — the callers' in-process tallies
    are the durable backstop for the caps.
    """
    r = _get_redis()
    if r:
        try:
            new = int(r.incrby(f"{_REDIS_PREFIX}{cache_key}", amount))
            r.expire(f"{_REDIS_PREFIX}{cache_key}", int(ttl_days * 86400))
            return new
        except Exception as e:
            logger.warning("Redis incr error for {}: {} — falling back to read-modify-write", cache_key, e)
    new = get_count(cache_key) + amount
    set_cached(cache_key, {"count": new}, ttl_days=ttl_days)
    return new


def incr_hash_count(cache_key: str, field: str, amount: int = 1, ttl_days: float = 35.0) -> int:
    """Atomically add *amount* to *field* of the hash at *cache_key*; returns the new
    value.

    The hash shape keeps one Redis key per day for multi-dimensional counters (e.g.
    the F1 ladder-rejection telemetry: key ``ladder:rejections:{date}``, fields
    ``{winner}|{loser}|{corroboration|contradiction}``) instead of a key explosion.
    Redis path: HINCRBY + EXPIRE — atomic across processes, same contract as
    ``incr_count``. Fallback (Redis down): non-atomic read-modify-write of the whole
    hash as a ``get_cached``/``set_cached`` dict — single-writer-safe only, and
    best-effort when BOTH backends are down (set_cached swallows failures). Callers
    that must never break on telemetry (the spec-write path) wrap this call anyway.
    """
    r = _get_redis()
    if r:
        try:
            new = int(r.hincrby(f"{_REDIS_PREFIX}{cache_key}", field, amount))
            r.expire(f"{_REDIS_PREFIX}{cache_key}", int(ttl_days * 86400))
            return new
        except Exception as e:
            logger.warning("Redis hash-incr error for {}: {} — falling back to read-modify-write", cache_key, e)
    data = get_cached(cache_key)
    counts: dict = dict(data) if isinstance(data, dict) else {}
    try:
        new = int(counts.get(field, 0) or 0) + amount
    except (TypeError, ValueError):
        new = amount
    counts[field] = new
    set_cached(cache_key, counts, ttl_days=ttl_days)
    return new


def cleanup_expired() -> int:
    """Remove expired cache entries in batches. Returns count deleted.

    Called periodically by the scheduler (e.g., daily). Deletes in batches of 1000 to
    avoid locking the table.
    """
    count = 0
    BATCH_SIZE = 1000

    # PostgreSQL cleanup — batched to avoid long table locks
    try:
        with SessionLocal() as db:
            while True:
                result = cast(
                    CursorResult,
                    db.execute(
                        text(
                            "DELETE FROM intel_cache WHERE ctid IN "
                            "(SELECT ctid FROM intel_cache WHERE expires_at < NOW() LIMIT :batch)"
                        ),
                        {"batch": BATCH_SIZE},
                    ),
                )
                db.commit()
                count += result.rowcount
                if result.rowcount < BATCH_SIZE:
                    break
            if count:
                logger.info("Cache cleanup: removed {} expired entries from PostgreSQL", count)
    except Exception as e:
        logger.warning("Cache cleanup error: {}", e)

    # Redis handles expiration automatically via TTL — no cleanup needed
    return count
