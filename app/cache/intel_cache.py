"""Intel cache — Redis primary with PostgreSQL fallback.

Used for: company intelligence cards (7-day TTL),
contact enrichment results (14-day TTL), market context (30-day TTL).

Redis is preferred for speed. Falls back to PostgreSQL if Redis is
unavailable (e.g., during development without Docker).
"""

import json
import logging
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from app.database import SessionLocal

log = logging.getLogger("avail.cache")

# Lazy-initialized Redis client
_redis_client = None
_redis_init_attempted = False
_REDIS_PREFIX = "intel:"


def _get_redis():
    """Lazy-init Redis connection. Returns client or None if unavailable."""
    global _redis_client, _redis_init_attempted

    if _redis_init_attempted:
        return _redis_client

    _redis_init_attempted = True

    if os.environ.get("TESTING"):
        return None

    try:
        from app.config import settings
        if settings.cache_backend == "postgres":
            log.info("Cache backend set to postgres — skipping Redis")
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
        log.info("Redis cache connected: %s", settings.redis_url)
    except Exception as e:
        log.warning("Redis unavailable, falling back to PostgreSQL cache: %s", e)
        _redis_client = None

    return _redis_client


def get_cached(cache_key: str) -> dict | None:
    """Retrieve cached data if not expired. Returns None on miss."""
    # Try Redis first
    r = _get_redis()
    if r:
        try:
            data = r.get(f"{_REDIS_PREFIX}{cache_key}")
            if data:
                return json.loads(data)
            return None
        except Exception as e:
            log.debug("Redis read error for %s: %s", cache_key, e)

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
        log.debug("Cache read error for %s: %s", cache_key, e)
    return None


def set_cached(cache_key: str, data: dict, ttl_days: int = 7) -> None:
    """Store data in cache with TTL."""
    ttl_seconds = ttl_days * 86400

    # Try Redis first
    r = _get_redis()
    if r:
        try:
            r.setex(f"{_REDIS_PREFIX}{cache_key}", ttl_seconds, json.dumps(data))
            return  # Success — skip PG write
        except Exception as e:
            log.debug("Redis write error for %s: %s", cache_key, e)

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
        log.warning("Cache write error for %s: %s", cache_key, e)


def invalidate(cache_key: str) -> None:
    """Delete a specific cache entry."""
    # Try Redis
    r = _get_redis()
    if r:
        try:
            r.delete(f"{_REDIS_PREFIX}{cache_key}")
        except Exception as e:
            log.debug("Redis invalidate error for %s: %s", cache_key, e)

    # Also clean PostgreSQL (may have stale entry)
    try:
        with SessionLocal() as db:
            db.execute(
                text("DELETE FROM intel_cache WHERE cache_key = :key"),
                {"key": cache_key},
            )
            db.commit()
    except Exception as e:
        log.debug("Cache invalidate error for %s: %s", cache_key, e)


def cleanup_expired() -> int:
    """Remove all expired cache entries. Returns count deleted.

    Called periodically by the scheduler (e.g., daily).
    """
    count = 0

    # PostgreSQL cleanup
    try:
        with SessionLocal() as db:
            result = db.execute(
                text("DELETE FROM intel_cache WHERE expires_at < NOW()")
            )
            db.commit()
            count = result.rowcount
            if count:
                log.info("Cache cleanup: removed %d expired entries from PostgreSQL", count)
    except Exception as e:
        log.warning("Cache cleanup error: %s", e)

    # Redis handles expiration automatically via TTL — no cleanup needed
    return count
