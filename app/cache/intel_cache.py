"""Intel cache — PostgreSQL-backed TTL cache.

Used for: company intelligence cards (7-day TTL),
contact enrichment results (14-day TTL), market context (30-day TTL).

Simple and reliable — no Redis dependency. Cache table lives in the
same PostgreSQL database as everything else.
"""
import json, logging
from datetime import datetime, timezone, timedelta

from sqlalchemy import text

from app.database import SessionLocal

log = logging.getLogger("avail.cache")


def get_cached(cache_key: str) -> dict | None:
    """Retrieve cached data if not expired. Returns None on miss."""
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
        log.debug(f"Cache read error for {cache_key}: {e}")
    return None


def set_cached(cache_key: str, data: dict, ttl_days: int = 7) -> None:
    """Store data in cache with TTL."""
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
        log.warning(f"Cache write error for {cache_key}: {e}")


def invalidate(cache_key: str) -> None:
    """Delete a specific cache entry."""
    try:
        with SessionLocal() as db:
            db.execute(
                text("DELETE FROM intel_cache WHERE cache_key = :key"),
                {"key": cache_key},
            )
            db.commit()
    except Exception as e:
        log.debug(f"Cache invalidate error for {cache_key}: {e}")


def cleanup_expired() -> int:
    """Remove all expired cache entries. Returns count deleted.

    Called periodically by the scheduler (e.g., daily).
    """
    try:
        with SessionLocal() as db:
            result = db.execute(
                text("DELETE FROM intel_cache WHERE expires_at < NOW()")
            )
            db.commit()
            count = result.rowcount
            if count:
                log.info(f"Cache cleanup: removed {count} expired entries")
            return count
    except Exception as e:
        log.warning(f"Cache cleanup error: {e}")
        return 0
