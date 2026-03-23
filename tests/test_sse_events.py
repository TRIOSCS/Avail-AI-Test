"""test_sse_events.py — Tests for the SSE event stream endpoint.

Verifies authentication enforcement and basic endpoint availability.

Called by: pytest
Depends on: app/routers/events.py, conftest fixtures
"""

from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app


def test_event_stream_returns_200_for_authenticated_user(client):
    """Authenticated users should get a 200 response with SSE content-type."""

    # Publish a message so the stream has something to yield, then the generator
    # will hit is_disconnected (TestClient closes after first chunk timeout).
    async def _fake_listen(channel):
        yield {"event": "ping", "data": "hello"}

    with patch("app.routers.events.broker") as mock_broker:
        mock_broker.listen = _fake_listen
        resp = client.get("/api/events/stream")
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")


def test_event_stream_requires_auth(db_session):
    """Unauthenticated requests should get 401."""
    from app.database import get_db

    def _override_db():
        yield db_session

    # Only override DB, NOT auth — so require_user raises 401
    app.dependency_overrides[get_db] = _override_db
    try:
        with TestClient(app, raise_server_exceptions=False) as raw_client:
            resp = raw_client.get("/api/events/stream")
            assert resp.status_code == 401
    finally:
        app.dependency_overrides.pop(get_db, None)
