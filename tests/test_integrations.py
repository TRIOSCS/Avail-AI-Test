"""Tests for integrations bundle — Charts, ACS.

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
