"""Shared rate limiter with Redis storage and in-memory fallback.

Uses Redis for distributed rate limiting across workers when available.
Falls back to in-memory storage if Redis is unreachable at startup
(limits won't be shared across workers in that case).
"""

from loguru import logger
from slowapi import Limiter
from slowapi.util import get_remote_address

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
            "Redis unavailable — rate limiter using in-memory storage "
            "(limits won't be shared across workers)"
        )
        return None


limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[settings.rate_limit_default],
    enabled=settings.rate_limit_enabled,
    storage_uri=_resolve_storage(),
)
