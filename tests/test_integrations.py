"""Tests for integrations bundle — Charts, Presence, ACS, Call Records.

Called by: pytest
Depends on: app.routers.crm.views, app.services.presence_service
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from tests.conftest import engine  # noqa: F401


class TestPresenceService:
    """Test Teams presence detection."""

    async def test_get_presence_returns_status(self):
        """get_presence returns availability string."""
        from app.services.presence_service import get_presence

        mock_gc = MagicMock()
        mock_gc.get_json = AsyncMock(return_value={"availability": "Available"})

        result = await get_presence("user@example.com", mock_gc)
        assert result == "Available"

    async def test_get_presence_caches_result(self):
        """Repeated calls use cache, not API."""
        from app.services.presence_service import _presence_cache, get_presence

        _presence_cache.clear()

        mock_gc = MagicMock()
        mock_gc.get_json = AsyncMock(return_value={"availability": "Away"})

        await get_presence("cached@example.com", mock_gc)
        await get_presence("cached@example.com", mock_gc)

        # Only one API call despite two get_presence calls
        assert mock_gc.get_json.call_count == 1

    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            ("Available", "bg-emerald-400"),
            ("Away", "bg-amber-400"),
            ("Busy", "bg-rose-400"),
            (None, "bg-gray-300"),
        ],
    )
    def test_presence_color(self, status, expected):
        """presence_color maps each availability status to its badge color."""
        from app.services.presence_service import presence_color

        assert presence_color(status) == expected


class TestPerformanceMetricsEndpoint:
    """The Chart.js JSON metrics endpoint is retired with the Team Performance
    dashboard."""

    def test_metrics_endpoint_gone(self, client: TestClient):
        """GET /api/crm/performance-metrics no longer exists (route removed)."""
        resp = client.get("/api/crm/performance-metrics")
        assert resp.status_code == 404


class TestACSService:
    """Test Azure Communication Services integration."""

    def test_acs_webhook_endpoint_exists(self, client: TestClient):
        """POST /api/webhooks/acs returns 200 or 400 (not 404)."""
        resp = client.post("/api/webhooks/acs", json={})
        assert resp.status_code != 404

    def test_call_initiate_endpoint_exists(self, client: TestClient):
        """POST /api/calls/initiate returns 200 or 422 (not 404)."""
        resp = client.post("/api/calls/initiate", json={"to_phone": "+15551234567"})
        # Will fail with config error since ACS not configured, but route exists
        assert resp.status_code != 404


class TestTeamsCallRecordsJob:
    """Test Teams call records sync job."""

    def test_register_teams_call_jobs_exists(self):
        """register_teams_call_jobs function exists."""
        from app.jobs.teams_call_jobs import register_teams_call_jobs

        assert callable(register_teams_call_jobs)
