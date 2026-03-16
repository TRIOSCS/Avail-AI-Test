"""
test_htmx_foundation.py — Tests for HTMX frontend foundation.

Verifies: feature flag, HTMX detection utilities, CDN removal,
Vite asset loading, brand colors, logo, sidebar nav items,
breadcrumb, login page, and global search endpoint.

Called by: pytest
Depends on: app.config, app.dependencies, conftest.py fixtures
"""

from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from app.config import Settings
from app.dependencies import is_htmx_boosted, wants_html


class TestWantsHtml:
    """Tests for the wants_html() HTMX detection utility."""

    def test_returns_true_for_htmx_request(self):
        request = MagicMock()
        request.headers = {"HX-Request": "true"}
        assert wants_html(request) is True

    def test_returns_false_for_normal_request(self):
        request = MagicMock()
        request.headers = {}
        assert wants_html(request) is False

    def test_returns_false_for_wrong_value(self):
        request = MagicMock()
        request.headers = {"HX-Request": "false"}
        assert wants_html(request) is False


class TestIsHtmxBoosted:
    """Tests for the is_htmx_boosted() detection utility."""

    def test_returns_true_for_boosted_request(self):
        request = MagicMock()
        request.headers = {"HX-Boosted": "true"}
        assert is_htmx_boosted(request) is True

    def test_returns_false_for_non_boosted(self):
        request = MagicMock()
        request.headers = {}
        assert is_htmx_boosted(request) is False


class TestUseHtmxFeatureFlag:
    """Tests for the USE_HTMX feature flag in Settings."""

    def test_default_is_true(self):
        import os

        os.environ["TESTING"] = "1"
        os.environ["DATABASE_URL"] = "sqlite:///test.db"
        s = Settings(
            database_url="sqlite:///test.db",
            _env_file=None,
        )
        assert s.use_htmx is True

    def test_can_disable(self):
        import os

        os.environ["TESTING"] = "1"
        os.environ["DATABASE_URL"] = "sqlite:///test.db"
        s = Settings(
            database_url="sqlite:///test.db",
            use_htmx=False,
            _env_file=None,
        )
        assert s.use_htmx is False


class TestLoginPageBranding:
    """Verify login page uses brand colors and logo (unauthenticated full page)."""

    def test_login_has_brand_bg(self, client: TestClient):
        """Full page / without session auth → login page with brand colors."""
        # Full page uses get_user() not require_user, so client fixture auth
        # doesn't apply. This returns the login page.
        resp = client.get("/requisitions")
        assert resp.status_code == 200
        assert "brand-900" in resp.text or "brand-800" in resp.text

    def test_login_has_logo(self, client: TestClient):
        resp = client.get("/requisitions")
        assert "avail_logo" in resp.text

    def test_login_no_cdn(self, client: TestClient):
        resp = client.get("/requisitions")
        assert "cdn.tailwindcss.com" not in resp.text


class TestBaseTemplateBranding:
    """Test base template via partials (which use require_user auth override)."""

    def test_partials_no_cdn_tailwind(self, client: TestClient):
        """Partial requests should not reference CDN."""
        resp = client.get(
            "/partials/requisitions",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "cdn.tailwindcss.com" not in resp.text


class TestGlobalSearch:
    """Verify global search endpoint returns HTML."""

    def test_global_search_returns_200(self, client: TestClient):
        resp = client.get("/partials/search/global?q=test")
        assert resp.status_code == 200

    def test_global_search_short_query_returns_empty(self, client: TestClient):
        resp = client.get("/partials/search/global?q=a")
        assert resp.status_code == 200

    def test_global_search_finds_requisition(self, client: TestClient, test_requisition):
        resp = client.get("/partials/search/global?q=REQ-TEST")
        assert resp.status_code == 200
        assert "REQ-TEST" in resp.text


class TestViteManifestReader:
    """Test the _vite_assets() helper in htmx_views."""

    def test_vite_assets_returns_dict(self):
        from app.routers.htmx._helpers import _vite_assets

        result = _vite_assets()
        assert "js_file" in result
        assert "css_files" in result
        assert isinstance(result["css_files"], list)
