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


async def close_clients():
    """Shut down both shared clients.

    Call from app lifespan shutdown.
    """
    for name, client in (("http", http), ("http_redirect", http_redirect)):
        try:
            await client.aclose()
        except RuntimeError as e:
            logger.debug("{} client close RuntimeError (expected during shutdown): {}", name, e)
