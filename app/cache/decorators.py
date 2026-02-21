"""
cache/decorators.py — Endpoint caching decorator

Wraps get_cached/set_cached from intel_cache.py. Caches the return value
of an endpoint function by building a key from specified parameters.

Usage:
    @cached_endpoint(prefix="perf_vendors", ttl_hours=4, key_params=["sort_by", "order", "limit", "offset"])
    def list_vendor_scorecards(sort_by, order, limit, offset, ...):
        ...
"""

import functools
import hashlib
import json
import logging

from .intel_cache import get_cached, invalidate, set_cached

log = logging.getLogger("avail.cache")


def cached_endpoint(prefix: str, ttl_hours: float = 4, key_params: list[str] | None = None):
    """Decorator that caches an endpoint's return value.

    Args:
        prefix: Cache key prefix (e.g. "perf_vendors")
        ttl_hours: Time-to-live in hours (converted to fractional days for set_cached)
        key_params: List of kwarg names to include in the cache key.
                    If None, all kwargs are used (excluding db, user, request).
    """
    # Convert hours to days for set_cached (which takes ttl_days)
    ttl_days = ttl_hours / 24

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            # Build cache key from specified params
            excluded = {"db", "user", "request"}
            if key_params is not None:
                key_dict = {k: kwargs.get(k) for k in key_params}
            else:
                key_dict = {k: v for k, v in kwargs.items() if k not in excluded}

            # Deterministic key: sort dict and hash
            key_str = json.dumps(key_dict, sort_keys=True, default=str)
            key_hash = hashlib.md5(key_str.encode(), usedforsecurity=False).hexdigest()[:12]
            cache_key = f"{prefix}:{key_hash}"

            # Try cache hit
            cached = get_cached(cache_key)
            if cached is not None:
                log.debug("Cache HIT: %s", cache_key)
                return cached

            # Cache miss — call the real function
            log.debug("Cache MISS: %s", cache_key)
            result = func(*args, **kwargs)

            # Only cache dict/list results (not Response objects)
            if isinstance(result, (dict, list)):
                set_cached(cache_key, result, ttl_days=max(1, int(ttl_days)) if ttl_days >= 1 else 1)

            return result

        # Expose cache prefix for invalidation
        wrapper.cache_prefix = prefix
        return wrapper

    return decorator


def invalidate_prefix(prefix: str) -> None:
    """Invalidate all cache entries matching a prefix.

    Note: Redis supports pattern deletion, PostgreSQL fallback uses LIKE.
    """
    from .intel_cache import _get_redis, _REDIS_PREFIX

    # Redis: scan and delete by pattern
    r = _get_redis()
    if r:
        try:
            cursor = 0
            pattern = f"{_REDIS_PREFIX}{prefix}:*"
            while True:
                cursor, keys = r.scan(cursor=cursor, match=pattern, count=100)
                if keys:
                    r.delete(*keys)
                if cursor == 0:
                    break
        except Exception as e:
            log.debug("Redis prefix invalidation error for %s: %s", prefix, e)

    # PostgreSQL: delete by LIKE pattern
    try:
        from sqlalchemy import text

        from app.database import SessionLocal

        with SessionLocal() as db:
            db.execute(
                text("DELETE FROM intel_cache WHERE cache_key LIKE :pattern"),
                {"pattern": f"{prefix}:%"},
            )
            db.commit()
    except Exception as e:
        log.debug("PG prefix invalidation error for %s: %s", prefix, e)
