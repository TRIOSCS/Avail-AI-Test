"""cache/decorators.py — Endpoint caching decorator.

Wraps get_cached/set_cached from intel_cache.py. Caches the return value
of an endpoint function by building a key from specified parameters.

Usage:
    @cached_endpoint(prefix="perf_vendors", ttl_hours=4, key_params=["sort_by", "order", "limit", "offset"])
    def list_vendor_scorecards(sort_by, order, limit, offset, ...):
        ...
"""

import asyncio
import functools
import hashlib

from loguru import logger

from app.utils import json_helpers as json

from .intel_cache import get_cached, set_cached

# Kwargs never folded into the cache key (request-scoped, non-serializable).
_KEY_EXCLUDED = frozenset({"db", "user", "request"})


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
        # Closure helpers are intentionally left unannotated (like the original inline
        # wrapper body): the cache stores JSON, so `set_cached` accepts dict/list at
        # runtime even though its param is typed `dict`.
        def _build_cache_key(kwargs):
            # Build cache key from specified params
            if key_params is not None:
                key_dict = {k: kwargs.get(k) for k in key_params}
            else:
                key_dict = {k: v for k, v in kwargs.items() if k not in _KEY_EXCLUDED}

            # Include user.id in key for per-user caching
            user = kwargs.get("user")
            if user and hasattr(user, "id"):
                key_dict["_uid"] = user.id

            # Deterministic key: sort dict and hash
            key_str = json.dumps(key_dict, sort_keys=True, default=str)
            key_hash = hashlib.md5(key_str.encode(), usedforsecurity=False).hexdigest()[:12]
            return f"{prefix}:{key_hash}"

        def _read_cache(cache_key):
            """Return the cached value, or None on a miss/read error."""
            try:
                cached = get_cached(cache_key)
                if cached is not None:
                    logger.debug("Cache HIT: {}", cache_key)
                    return cached
            except Exception as e:
                logger.warning("Cache read failed for {}: {}", cache_key, e)
            return None

        def _store_result(cache_key, result):
            # Don't cache error responses
            if isinstance(result, dict) and "error" in result:
                return
            # Only cache dict/list results (not Response/StreamingResponse objects)
            if isinstance(result, (dict, list)):
                try:
                    set_cached(cache_key, result, ttl_days=ttl_days)
                except Exception as e:
                    logger.warning("Cache write failed for {}: {}", cache_key, e)

        # Async endpoints must get an async wrapper: a sync wrapper would return an
        # unawaited coroutine on a miss and a bare value on a hit — inconsistent with
        # FastAPI's async contract and never actually caching. Streaming targets
        # (StreamingResponse / async generators) still compose: they are awaited then
        # skipped by `_store_result`'s dict/list gate, so they pass through uncached.
        if asyncio.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args, **kwargs):
                cache_key = _build_cache_key(kwargs)
                cached = _read_cache(cache_key)
                if cached is not None:
                    return cached
                logger.debug("Cache MISS: {}", cache_key)
                result = await func(*args, **kwargs)
                _store_result(cache_key, result)
                return result

            async_wrapper.cache_prefix = prefix  # type: ignore[attr-defined]  # dynamic attr read by invalidation helpers
            return async_wrapper

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            cache_key = _build_cache_key(kwargs)
            cached = _read_cache(cache_key)
            if cached is not None:
                return cached
            logger.debug("Cache MISS: {}", cache_key)
            result = func(*args, **kwargs)
            _store_result(cache_key, result)
            return result

        # Expose cache prefix for invalidation
        wrapper.cache_prefix = prefix  # type: ignore[attr-defined]  # dynamic attr read by invalidation helpers
        return wrapper

    return decorator


def invalidate_prefix(prefix: str) -> None:
    """Invalidate all cache entries matching a prefix.

    Note: Redis supports pattern deletion, PostgreSQL fallback uses LIKE.
    """
    from .intel_cache import _REDIS_PREFIX, _get_redis

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
            logger.warning("Redis prefix invalidation error for {}: {}", prefix, e)

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
        logger.warning("PG prefix invalidation error for {}: {}", prefix, e)
