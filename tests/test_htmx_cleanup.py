"""
test_htmx_cleanup.py — Tests for Phase 3 Task 12: HTMX default + final cleanup.
Verifies USE_HTMX defaults to True, old SPA still works as fallback,
and all HTMX view routes are accessible.
Called by: pytest
Depends on: app.config, app.main, conftest fixtures
"""

import os

import pytest

from app.config import Settings


class TestHtmxIsDefault:
    """USE_HTMX should now default to True."""

    def test_htmx_is_default(self):
        os.environ["TESTING"] = "1"
        os.environ["DATABASE_URL"] = "sqlite:///test.db"
        s = Settings(
            database_url="sqlite:///test.db",
            _env_file=None,
        )
        assert s.use_htmx is True, "USE_HTMX should default to True after Phase 3"


class TestOldSpaFallback:
    """The old SPA at '/' should still serve index.html regardless of USE_HTMX."""

    def test_old_spa_fallback(self, client):
        """GET / should return 200 with index.html content (old SPA route)."""
        resp = client.get("/", follow_redirects=False)
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")


class TestAllViewsAccessible:
    """All HTMX view routes should return 200 (views router always registered)."""

    VIEW_ROUTES = [
        "/app",
        "/views/requisitions",
        "/views/companies",
        "/views/quotes",
        "/views/vendors",
        "/views/buy-plans",
        "/views/prospecting",
    ]

    @pytest.mark.parametrize("path", VIEW_ROUTES)
    def test_view_route_accessible(self, client, path):
        """Each HTMX view route should return 200."""
        resp = client.get(path)
        assert resp.status_code == 200, f"{path} returned {resp.status_code}"


class TestViewsRouterAlwaysRegistered:
    """The views router should be registered regardless of USE_HTMX setting."""

    def test_views_routes_exist_in_app(self):
        """Verify that /views/* routes are in the app's route table."""
        from app.main import app

        paths = [r.path for r in app.routes if hasattr(r, "path")]
        htmx_paths = [p for p in paths if p.startswith("/views/") or p == "/app"]
        assert len(htmx_paths) > 0, "HTMX view routes should always be registered"
