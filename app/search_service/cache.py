"""Search package — market-stats helpers + the 15-min Redis search-result cache.

W4.5a split of the 3,604-line app/search_service.py — pure structural move: behavior
unchanged. The facade (__init__) re-exports every former top-level name; test PATCH
targets point at the defining submodule (patching a package attribute cannot intercept a
submodule-local lookup — same rule as app/routers/sightings/).
"""

import hashlib
import json
import os
from datetime import UTC, datetime

import redis
from loguru import logger

from ..cache.redis_probe import RedisProbe


def _median(values: list[float]) -> float | None:
    """Return the median of a list of numbers, or None if empty."""
    if not values:
        return None
    s = sorted(values)
    return s[len(s) // 2]


def compute_market_baseline(rows: list[dict]) -> dict:
    """Compute a read-only franchise-price summary from already-fetched market rows.

    Restricted to rows where ``is_authorized is True`` (franchise / authorized
    distributor). Uses ``_median`` for the upper-median price calculation.

    Args:
        rows: List of market-row dicts (same schema as cached_rows / vendor_card.html).
              Each may carry ``unit_price`` (float|None), ``qty_available`` (int|None),
              and ``is_authorized`` (bool, default False).

    Returns:
        A dict with keys:
          - ``has_authorized``: bool — True if any authorized row exists.
          - ``median_price``:   float|None — median of authorized unit_prices (non-None,
                                >0 only); None when no such prices exist.
          - ``total_stock``:    int|None — sum of authorized qty_available values (non-
                                None only); None when no authorized row has a known qty.
          - ``sources``:        int — count of authorized rows.

    No DB access, no side-effects. Safe to call with an empty or all-non-authorized list.
    """
    auth_rows = [r for r in rows if r.get("is_authorized")]
    if not auth_rows:
        return {"has_authorized": False, "median_price": None, "total_stock": None, "sources": 0}

    prices = [r["unit_price"] for r in auth_rows if r.get("unit_price") and r["unit_price"] > 0]
    median_price = _median(prices)

    known_qtys = [r["qty_available"] for r in auth_rows if r.get("qty_available") is not None]
    total_stock: int | None = sum(known_qtys) if known_qtys else None

    return {
        "has_authorized": True,
        "median_price": median_price,
        "total_stock": total_stock,
        "sources": len(auth_rows),
    }


# ── Search result cache (Redis, 15-min TTL) ─────────────────────────────

_SEARCH_CACHE_TTL = 900  # 15 minutes
_SEARCH_CACHE_PREFIX = "search:"


def _connect_search_redis():
    """Open a live search-cache Redis client.

    Returns ``None`` under TESTING (probe stays permanently off); raises on a transient
    connect failure so ``RedisProbe`` re-probes and recovers when Redis returns.
    """
    if os.environ.get("TESTING"):
        return None

    import redis

    from ..config import settings

    client = redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=1,
        retry_on_timeout=True,
    )
    client.ping()
    return client


_search_redis_probe = RedisProbe("search_cache", _connect_search_redis)


def _get_search_redis():
    """Lazy-init Redis for search caching, re-probing after an outage.

    Returns a live client or ``None`` (caching disabled). Unlike the old sticky init, a
    Redis outage self-heals: the probe periodically retries the real Redis and recovers
    transparently once it returns, and the degraded state is exported as a metric.
    """
    return _search_redis_probe.get()


def _search_cache_key(pns: list[str], connector_names: list[str]) -> str:
    """Deterministic cache key from sorted PNs + active connectors."""
    payload = json.dumps({"pns": sorted(pns), "connectors": sorted(connector_names)}, sort_keys=True)
    return _SEARCH_CACHE_PREFIX + hashlib.md5(payload.encode(), usedforsecurity=False).hexdigest()


def _get_search_cache(key: str) -> tuple[list[dict], list[dict], str | None] | None:
    """Return (results, source_stats, cached_at_iso) from cache or None on miss.

    ``cached_at_iso`` is the ISO timestamp the entry was written (None for
    legacy entries written before this field existed) — callers use it to
    compute the REAL data age for freshness scoring instead of assuming a
    cache hit is as fresh as a live fetch (see ``_cache_age_hours``).
    """
    r = _get_search_redis()
    if not r:
        return None
    try:
        data = r.get(key)
        if data:
            parsed = json.loads(data)
            return parsed["results"], parsed["source_stats"], parsed.get("cached_at")
    except redis.RedisError as e:
        logger.error("Redis error reading search cache key {}: {}", key, e)
    except Exception as e:
        logger.warning("Search cache read failed: {}", e)
    return None


def _set_search_cache(key: str, results: list[dict], source_stats: list[dict]) -> None:
    """Store search results in Redis with TTL, stamped with the write time so a later
    cache HIT can compute real data age instead of assuming it's brand fresh."""
    r = _get_search_redis()
    if not r:
        return
    try:
        payload = {
            "results": results,
            "source_stats": source_stats,
            "cached_at": datetime.now(UTC).isoformat(),
        }
        r.setex(key, _SEARCH_CACHE_TTL, json.dumps(payload))
    except redis.RedisError as e:
        logger.error("Redis error writing search cache key {}: {}", key, e)
    except Exception as e:
        logger.warning("Search cache write failed: {}", e)


def _cache_age_hours(cached_at_iso: str | None) -> float:
    """Hours elapsed since a search-cache entry was written.

    Returns 0.0 when ``cached_at_iso`` is missing/unparseable (legacy entry, or a
    freshly-written one) so freshness scoring degrades gracefully rather than
    raising. Used to give cache-served results their REAL age instead of the
    hardcoded age_hours=0.0 a live fetch would legitimately use.
    """
    if not cached_at_iso:
        return 0.0
    try:
        cached_at = datetime.fromisoformat(cached_at_iso)
        if cached_at.tzinfo is None:
            cached_at = cached_at.replace(tzinfo=UTC)
        return max(0.0, (datetime.now(UTC) - cached_at).total_seconds() / 3600.0)
    except (ValueError, TypeError):
        return 0.0
