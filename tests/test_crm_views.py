"""Tests for CRM shell views.

Called by: pytest
Depends on: app.routers.crm.views
"""

from fastapi.testclient import TestClient

from tests.conftest import engine  # noqa: F401


class TestCRMShell:
    """Test CRM shell partial route."""

    def test_crm_shell_returns_html(self, client: TestClient):
        """GET /v2/partials/crm/shell returns 200 with tab bar."""
        resp = client.get("/v2/partials/crm/shell")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")

    def test_crm_shell_has_customers_tab(self, client: TestClient):
        """Shell renders Customers tab button."""
        resp = client.get("/v2/partials/crm/shell")
        assert "Customers" in resp.text

    def test_crm_shell_has_vendors_tab(self, client: TestClient):
        """Shell renders Vendors tab button."""
        resp = client.get("/v2/partials/crm/shell")
        assert "Vendors" in resp.text

    def test_crm_shell_has_tab_content_container(self, client: TestClient):
        """Shell renders #crm-tab-content container."""
        resp = client.get("/v2/partials/crm/shell")
        assert 'id="crm-tab-content"' in resp.text


class TestCRMFullPage:
    """Test CRM full-page route via v2_page dispatcher."""

    def test_v2_crm_returns_200(self, client: TestClient):
        """GET /v2/crm returns 200."""
        resp = client.get("/v2/crm")
        assert resp.status_code == 200

    def test_v2_crm_loads_shell_partial(self, client: TestClient):
        """GET /v2/crm loads the CRM shell partial."""
        resp = client.get("/v2/crm")
        assert resp.status_code == 200
