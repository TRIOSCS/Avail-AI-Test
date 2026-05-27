"""Prometheus metrics middleware + /metrics endpoint exposure.

Purpose: Record HTTP request count (http_requests_total) and request duration
    (http_request_duration_seconds) for application traffic, and expose them in
    Prometheus text format at /metrics. Pure ASGI middleware so it composes with
    streaming responses (sse-starlette) without consuming their bodies.
Called by: app.main (mounts middleware on app + adds the GET /metrics route).
Depends on: prometheus_client (Counter, Histogram, generate_latest, REGISTRY).

Replaces prometheus-fastapi-instrumentator, which hard-pinned starlette<1.0.0
and so blocked the starlette 1.0.1 bump required to fix PYSEC-2026-161.
"""

from __future__ import annotations

import time
from typing import MutableMapping

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    Counter,
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

_EXCLUDED_EXACT = {"/metrics", "/health"}
_EXCLUDED_PREFIXES = ("/static/",)


def _excluded(path: str) -> bool:
    return path in _EXCLUDED_EXACT or path.startswith(_EXCLUDED_PREFIXES)


def _handler_for(scope: Scope, fallback: str) -> str:
    """Use the matched route's templated path (e.g. /users/{id}) when available.

    Falls back to the raw request path. Routing populates scope["route"] before
    http.response.start fires, so by the time we record metrics in send_wrapper the
    templated path is usually present.
    """
    route = scope.get("route")
    path = getattr(route, "path", None) if route is not None else None
    return path or fallback


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

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["code"] = int(message.get("status", 0))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            duration = time.perf_counter() - start
            handler = _handler_for(scope, fallback=path)
            REQUEST_COUNT.labels(method=method, handler=handler, status=str(status_holder["code"])).inc()
            REQUEST_DURATION.labels(method=method, handler=handler).observe(duration)


def render_metrics() -> tuple[bytes, str]:
    """Return (body, content_type) for the /metrics endpoint."""
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
