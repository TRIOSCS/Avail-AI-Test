"""Shared HTTP clients — connection pooling for all outbound requests.

Two module-level singleton httpx.AsyncClient instances:
  - http: default (no redirects, 30s timeout, connection pooling)
  - http_redirect: for website scraping (follow_redirects=True)

Per-request timeout overrides via http.get(url, timeout=15).

Usage:
    from app.http_client import http, http_redirect
    resp = await http.post(url, json=payload, timeout=15)
    resp = await http_redirect.get(url)
"""

from typing import Any

import httpx
from loguru import logger

_LIMITS = httpx.Limits(
    max_connections=50,
    max_keepalive_connections=20,
    keepalive_expiry=30,
)

http = httpx.AsyncClient(
    timeout=30,
    limits=_LIMITS,
    follow_redirects=False,
)

http_redirect = httpx.AsyncClient(
    timeout=30,
    limits=_LIMITS,
    follow_redirects=True,
)


# ── Shared synchronous Anthropic SDK client (pooled, reused) ─────────
#
# The synchronous services (sighting_aggregation, vendor_affinity) call Claude from
# thread-pool / sync search-fanout contexts where the async claude_client can't be
# awaited. Building a fresh ``anthropic.Anthropic()`` per call spins up (and never
# closes) a new httpx connection pool every time; caching one client per API key reuses
# that pool across calls. Model selection stays with the caller (claude_client.MODELS) —
# this getter only owns client construction + reuse.
_anthropic_clients: dict[str, Any] = {}


def get_anthropic_client(api_key: str) -> Any:
    """Return a cached, reused synchronous ``anthropic.Anthropic`` client for
    ``api_key``.

    One client per distinct key is built lazily and reused across calls, so repeated
    calls share the SDK's internal httpx connection pool instead of re-creating it.
    Construction failures are not cached (the exception propagates to the caller).
    """
    client = _anthropic_clients.get(api_key)
    if client is None:
        import anthropic

        client = anthropic.Anthropic(api_key=api_key)
        _anthropic_clients[api_key] = client
    return client


async def close_clients():
    """Shut down both shared clients.

    Call from app lifespan shutdown.
    """
    for name, client in (("http", http), ("http_redirect", http_redirect)):
        try:
            await client.aclose()
        except RuntimeError as e:
            logger.debug("{} client close RuntimeError (expected during shutdown): {}", name, e)
