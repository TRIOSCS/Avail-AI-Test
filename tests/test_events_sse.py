"""Tests for SSE events stream endpoint auth and response.

Called by: pytest
Depends on: conftest.py fixtures, app.routers.events, app.services.sse_broker
"""

from contextlib import contextmanager
from unittest.mock import patch


def _single_event_broker(event):
    """Mock broker.listen to yield one event then stop.

    Avoids the infinite async generator that would otherwise hang the test worker.
    """

    async def _one_event(*_args, **_kwargs):
        yield event

    @contextmanager
    def _patched():
        with patch("app.routers.events.broker") as mock_broker:
            mock_broker.listen = _one_event
            yield

    return _patched()


class TestSSEStreamAuth:
    """GET /api/events/stream requires authentication."""

    ENDPOINT = "/api/events/stream"

    def test_unauthenticated_returns_401(self, unauthenticated_client):
        """SSE stream endpoint rejects unauthenticated requests."""
        resp = unauthenticated_client.get(self.ENDPOINT)
        assert resp.status_code in (401, 403)

    def test_authenticated_returns_200_with_sse_content_type(self, client):
        """Authenticated user gets 200 with text/event-stream content type."""
        with _single_event_broker({"event": "ping", "data": ""}):
            resp = client.get(self.ENDPOINT)
            assert resp.status_code == 200
            assert "text/event-stream" in resp.headers.get("content-type", "")

    def test_sse_response_contains_event_data(self, client):
        """SSE response body contains the yielded event formatted as SSE."""
        with _single_event_broker({"event": "test-event", "data": "hello"}):
            resp = client.get(self.ENDPOINT)
            assert resp.status_code == 200
            body = resp.text
            assert "event: test-event" in body
            assert "hello" in body

    def test_sse_response_disables_caching(self, client):
        """SSE responses disable caching to ensure real-time delivery."""
        with _single_event_broker({"event": "ping", "data": ""}):
            resp = client.get(self.ENDPOINT)
            assert resp.status_code == 200
            cache_control = resp.headers.get("cache-control", "")
            assert "no-store" in cache_control or "no-cache" in cache_control
