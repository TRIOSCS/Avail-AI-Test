"""Tests for integrations bundle — Charts, ACS, Call Records.

Called by: pytest
Depends on: app.routers.crm.views
"""

from fastapi.testclient import TestClient

from tests.conftest import engine  # noqa: F401


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
