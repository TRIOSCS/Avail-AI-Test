"""Tests for Teams DM alert service and config router.

Covers: hybrid delivery (Graph + webhook), rate limiting, config CRUD,
test endpoint, and edge cases.

Called by: pytest
Depends on: conftest fixtures (db_session, test_user, client)
"""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.auth import User
from app.models.teams_alert_config import TeamsAlertConfig
from app.services import teams_alert_service

# ── send_alert tests ────────────────────────────────────────────────


def test_send_alert_graph_dm_success(db_session, test_user):
    """Alert succeeds via Graph API DM when user has access_token."""
    test_user.access_token = "valid-token"
    db_session.commit()

    with patch.object(teams_alert_service, "_try_graph_dm", new_callable=AsyncMock, return_value=True):
        result = asyncio.get_event_loop().run_until_complete(
            teams_alert_service.send_alert(db_session, test_user.id, "Test message", "test", "1")
        )
    assert result is True


def test_send_alert_webhook_fallback(db_session, test_user):
    """Falls back to webhook when Graph DM fails."""
    config = TeamsAlertConfig(user_id=test_user.id, teams_webhook_url="https://hooks.example.com/test")
    db_session.add(config)
    db_session.commit()

    with (
        patch.object(teams_alert_service, "_try_graph_dm", new_callable=AsyncMock, return_value=False),
        patch.object(teams_alert_service, "_try_webhook", new_callable=AsyncMock, return_value=True),
    ):
        result = asyncio.get_event_loop().run_until_complete(
            teams_alert_service.send_alert(db_session, test_user.id, "Test", "test", "1")
        )
    assert result is True


def test_send_alert_no_config_no_graph(db_session, test_user):
    """Returns False when no Graph token and no webhook config."""
    with patch.object(teams_alert_service, "_try_graph_dm", new_callable=AsyncMock, return_value=False):
        result = asyncio.get_event_loop().run_until_complete(
            teams_alert_service.send_alert(db_session, test_user.id, "Test", "test", "1")
        )
    assert result is False


def test_send_alert_disabled(db_session, test_user):
    """Returns False when alerts_enabled=False."""
    config = TeamsAlertConfig(user_id=test_user.id, alerts_enabled=False)
    db_session.add(config)
    db_session.commit()

    result = asyncio.get_event_loop().run_until_complete(
        teams_alert_service.send_alert(db_session, test_user.id, "Test", "test", "1")
    )
    assert result is False


def test_send_alert_webhook_retry_on_5xx(db_session):
    """Webhook retries once on 5xx response."""
    mock_resp_500 = MagicMock(status_code=500)
    mock_resp_200 = MagicMock(status_code=200)

    call_count = 0

    async def mock_post(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return mock_resp_500 if call_count == 1 else mock_resp_200

    with patch("app.services.teams_alert_service.http") as mock_http:
        mock_http.post = mock_post
        result = asyncio.get_event_loop().run_until_complete(
            teams_alert_service._try_webhook("https://hooks.example.com/test", "msg")
        )
    assert result is True
    assert call_count == 2


def test_rate_limiter_blocks_excess(db_session, test_user):
    """Rate limiter blocks 21st message in one hour window."""
    test_user.access_token = "valid-token"
    db_session.commit()

    # Pre-fill rate bucket
    teams_alert_service._rate_buckets[test_user.id] = [time.time()] * 20

    with patch.object(teams_alert_service, "_try_graph_dm", new_callable=AsyncMock, return_value=True):
        result = asyncio.get_event_loop().run_until_complete(
            teams_alert_service.send_alert(db_session, test_user.id, "Test", "test", "1")
        )
    assert result is False

    # Cleanup
    teams_alert_service._rate_buckets.pop(test_user.id, None)


def test_rate_limiter_resets_after_window(db_session, test_user):
    """Rate limiter resets after the hour boundary."""
    # Fill with old timestamps (2 hours ago)
    old_time = time.time() - 7200
    teams_alert_service._rate_buckets[test_user.id] = [old_time] * 20

    assert not teams_alert_service._is_rate_limited(test_user.id)

    # Cleanup
    teams_alert_service._rate_buckets.pop(test_user.id, None)


# ── send_alert_to_role tests ───────────────────────────────────────


def test_send_alert_to_role(db_session, test_user):
    """Sends to all active users with matching role."""
    buyer2 = User(email="buyer2@trioscs.com", name="Buyer 2", role="buyer", azure_id="az-buyer2")
    db_session.add(buyer2)
    db_session.commit()

    with patch.object(teams_alert_service, "send_alert", new_callable=AsyncMock, return_value=True) as mock:
        count = asyncio.get_event_loop().run_until_complete(
            teams_alert_service.send_alert_to_role(db_session, "buyer", "New RFQ", "new_req", "1")
        )
    assert count == 2
    assert mock.call_count == 2


def test_send_alert_to_role_excludes_user(db_session, test_user):
    """Skips the excluded user (creator)."""
    with patch.object(teams_alert_service, "send_alert", new_callable=AsyncMock, return_value=True) as mock:
        count = asyncio.get_event_loop().run_until_complete(
            teams_alert_service.send_alert_to_role(
                db_session, "buyer", "New RFQ", "new_req", "1", exclude_user_id=test_user.id
            )
        )
    assert count == 0
    assert mock.call_count == 0


# ── resolve_director_id ────────────────────────────────────────────


def test_resolve_director_id(db_session):
    """Returns first active manager user ID."""
    mgr = User(email="mgr@trioscs.com", name="Manager", role="manager", azure_id="az-mgr")
    db_session.add(mgr)
    db_session.commit()

    result = teams_alert_service._resolve_director_id(db_session)
    assert result == mgr.id


def test_resolve_director_id_none(db_session):
    """Returns None when no manager exists."""
    result = teams_alert_service._resolve_director_id(db_session)
    assert result is None


# ── Config CRUD endpoints ──────────────────────────────────────────


@pytest.fixture
def _skip_if_teams_alert_router_disabled(client):
    has_route = any(getattr(route, "path", "") == "/api/teams-alerts/config" for route in client.app.routes)
    if not has_route:
        pytest.skip("Teams alerts router disabled in MVP mode")


def test_config_crud(client, db_session, test_user, _skip_if_teams_alert_router_disabled):
    """Full CRUD cycle for alert config."""
    # GET — no config yet
    resp = client.get("/api/teams-alerts/config")
    assert resp.status_code == 200
    assert resp.json()["configured"] is False

    # PUT — create
    resp = client.put(
        "/api/teams-alerts/config", json={"teams_webhook_url": "https://hook.test/abc", "alerts_enabled": True}
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    # GET — now configured
    resp = client.get("/api/teams-alerts/config")
    assert resp.status_code == 200
    data = resp.json()
    assert data["configured"] is True
    assert data["teams_webhook_url"] == "https://hook.test/abc"

    # DELETE
    resp = client.delete("/api/teams-alerts/config")
    assert resp.status_code == 200

    # GET — gone
    resp = client.get("/api/teams-alerts/config")
    assert resp.json()["configured"] is False
