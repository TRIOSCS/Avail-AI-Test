"""Prometheus metrics middleware + /metrics endpoint exposure.

Purpose: Record HTTP request count, in-flight gauge, and request duration for
    application traffic, and expose them in Prometheus text format at /metrics.
    Pure ASGI middleware so it composes with streaming responses (sse-starlette)
    without consuming their bodies.
Called by: app.main (mounts middleware on app + adds the GET /metrics route).
Depends on: prometheus_client (Counter, Gauge, Histogram, generate_latest, REGISTRY),
    loguru for one warning log on aborted requests.

Replaces prometheus-fastapi-instrumentator, which hard-pinned starlette<1.0.0
and so blocked the starlette 1.0.1 bump required to fix PYSEC-2026-161.

Intentional differences vs. the previous Instrumentator default suite:
- KEPT: http_requests_total, http_request_duration_seconds, http_requests_inprogress.
- DROPPED: http_request_size_bytes / http_response_size_bytes (unused in our
  Grafana / alerting; reintroduce as a Histogram if a saturation alert needs it).
- DROPPED: the highr / lowr duration histogram split (we keep the single default-
  bucketed histogram — Grafana queries using the dropped names will return No Data
  and should be migrated to http_request_duration_seconds).
- DROPPED: OpenMetrics content negotiation. /metrics always returns the
  Prometheus text format. Re-add via choose_encoder() if Exemplars/Created
  timestamps are needed by a scraper.
"""

from __future__ import annotations

import time
from typing import MutableMapping

from loguru import logger
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from starlette.types import ASGIApp, Message, Receive, Scope, Send

REQUEST_COUNT = Counter(
    "http_requests_total",
    "Total HTTP requests processed by the application.",
    ["method", "handler", "status"],
)

REQUEST_DURATION = Histogram(
    "http_request_duration_seconds",
    "HTTP request duration in seconds, by route template.",
    ["method", "handler"],
)

REQUEST_INFLIGHT = Gauge(
    "http_requests_inprogress",
    "In-flight HTTP requests being served, by method.",
    ["method"],
)

# Redis-backed subsystems (search-result cache, intel cache) fall back to a degraded
# path when Redis is unreachable. These make the degraded state observable so it can be
# alerted on (previously a failed connect disabled Redis silently for the process
# lifetime). REDIS_DEGRADED is the "is it degraded right now" signal (1/0);
# REDIS_DOWNGRADE_TOTAL counts distinct healthy→degraded transitions (outage events).
REDIS_DEGRADED = Gauge(
    "redis_degraded",
    "1 if a Redis-backed subsystem is currently serving its degraded fallback path.",
    ["subsystem"],
)

REDIS_DOWNGRADE_TOTAL = Counter(
    "redis_downgrade_total",
    "Count of healthy->degraded transitions for a Redis-backed subsystem.",
    ["subsystem"],
)

# Paths excluded from collection entirely. Each entry is either a fixed path
# (browser/health/observability noise) or a prefix; collectively they keep the
# counter free of high-volume, low-signal traffic.
_EXCLUDED_EXACT = {
    "/metrics",
    "/health",
    "/health/ready",
    "/sw.js",
    "/favicon.ico",
    "/robots.txt",
}
_EXCLUDED_PREFIXES = ("/static/",)

# Sentinel for any request that did not match a registered route (404s, bot probes).
# Using a fixed string keeps Prometheus label cardinality bounded — otherwise every
# distinct probe URL becomes its own time series.
_UNMATCHED_HANDLER = "<unmatched>"

# Sentinel status for requests that aborted before the inner app sent
# http.response.start (e.g. client disconnect, downstream raise outside
# Starlette's ServerErrorMiddleware). Operators can alert on
# http_requests_total{status="aborted"} to surface these without confusing them
# for real HTTP status codes.
_STATUS_ABORTED = "aborted"


def _excluded(path: str) -> bool:
    return path in _EXCLUDED_EXACT or path.startswith(_EXCLUDED_PREFIXES)


def _handler_for(scope: Scope) -> str:
    """Return the matched route's templated path (e.g. ``/users/{id}``).

    Starlette's router populates ``scope["route"]`` with the matched ``APIRoute``
    on a successful match (verified against starlette 1.2.1 and 1.3.1), so we read
    the templated path straight off it. This is also fastapi-0.137-safe: 0.137 turned
    ``app.routes`` into a tree (``include_router``'d routes hide behind an opaque
    ``_IncludedRouter`` wrapper), which would break any route-table walk — but the
    matched route on the scope is always the flat ``APIRoute`` regardless of nesting.

    Returns ``_UNMATCHED_HANDLER`` for requests that didn't match any route so
    that bot/scanner traffic can't blow up label cardinality.
    """
    route = scope.get("route")
    path = getattr(route, "path", None)
    return path or _UNMATCHED_HANDLER


class PrometheusMiddleware:
    """Pure ASGI middleware — does not buffer response bodies."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path: str = scope.get("path", "")
        if _excluded(path):
            await self.app(scope, receive, send)
            return

        method: str = scope.get("method", "")
        status_holder: MutableMapping[str, int] = {"code": 0}
        start = time.perf_counter()
        REQUEST_INFLIGHT.labels(method=method).inc()

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["code"] = int(message.get("status", 0))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            duration = time.perf_counter() - start
            REQUEST_INFLIGHT.labels(method=method).dec()
            handler = _handler_for(scope)
            code = status_holder["code"]
            if code == 0:
                # Downstream aborted before sending response.start (client
                # disconnect, raise above Starlette's ServerErrorMiddleware).
                # Count it under a distinct status sentinel and skip the
                # duration histogram so p50/p99 aren't poisoned by zero-time
                # samples.
                REQUEST_COUNT.labels(method=method, handler=handler, status=_STATUS_ABORTED).inc()
                logger.warning(
                    "ASGI request aborted before response.start: {method} {path}",
                    method=method,
                    path=path,
                )
            else:
                REQUEST_COUNT.labels(method=method, handler=handler, status=str(code)).inc()
                REQUEST_DURATION.labels(method=method, handler=handler).observe(duration)


def render_metrics() -> tuple[bytes, str]:
    """Return (body, content_type) for the /metrics endpoint."""
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
