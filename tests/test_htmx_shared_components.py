"""
test_htmx_shared_components.py — Tests for Phase 3 Task 2: shared components.
Verifies sidebar, topbar, mobile nav partials render correctly, and global
search endpoint returns HTML partial.
Called by: pytest
Depends on: app/routers/views.py, app/templates/partials/shared/
"""

import os

os.environ.setdefault("TESTING", "1")
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("USE_HTMX", "true")

import pytest
from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader


@pytest.fixture()
def jinja_env():
    """Jinja2 environment pointing at the app templates directory."""
    return Environment(loader=FileSystemLoader("app/templates"))


@pytest.fixture()
def htmx_client(db_session, test_user):
    """TestClient with views router registered (USE_HTMX=true)."""
    from app.database import get_db
    from app.dependencies import require_user
    from app.main import app
    from app.routers.views import router as views_router

    # Ensure views router is included (may already be if USE_HTMX was true at import)
    route_paths = [r.path for r in app.routes]
    if "/search" not in route_paths:
        app.include_router(views_router)

    def _override_db():
        yield db_session

    def _override_user():
        return test_user

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_user] = _override_user

    with TestClient(app) as c:
        yield c

    app.dependency_overrides.clear()


class TestSidebarRenders:
    """Tests that sidebar partial contains all expected nav items."""

    def test_sidebar_contains_all_nav_items(self, jinja_env):
        template = jinja_env.get_template("partials/shared/sidebar.html")
        html = template.render()
        expected_items = [
            "Requisitions",
            "Companies",
            "Vendors",
            "Quotes",
            "Buy Plans",
            "Prospecting",
            "Settings",
        ]
        for item in expected_items:
            assert item in html, f"Sidebar missing nav item: {item}"

    def test_sidebar_has_hx_boost_links(self, jinja_env):
        template = jinja_env.get_template("partials/shared/sidebar.html")
        html = template.render()
        assert 'hx-boost="true"' in html
        assert 'hx-target="#main-content"' in html
        assert 'hx-push-url="true"' in html

    def test_sidebar_has_correct_hrefs(self, jinja_env):
        template = jinja_env.get_template("partials/shared/sidebar.html")
        html = template.render()
        expected_hrefs = [
            "/requisitions",
            "/companies",
            "/vendors",
            "/quotes",
            "/buy-plans",
            "/prospecting",
            "/settings",
        ]
        for href in expected_hrefs:
            assert f'href="{href}"' in html, f"Sidebar missing href: {href}"


class TestTopbarRenders:
    """Tests that topbar partial contains search input with hx-get."""

    def test_topbar_has_search_input(self, jinja_env):
        template = jinja_env.get_template("partials/shared/topbar.html")
        # Provide user context that the template expects
        html = template.render(user=type("User", (), {"name": "Test User"})())
        assert 'type="search"' in html
        assert 'hx-get="/search"' in html

    def test_topbar_has_debounced_trigger(self, jinja_env):
        template = jinja_env.get_template("partials/shared/topbar.html")
        html = template.render(user=type("User", (), {"name": "Test User"})())
        assert "delay:300ms" in html

    def test_topbar_has_search_results_target(self, jinja_env):
        template = jinja_env.get_template("partials/shared/topbar.html")
        html = template.render(user=type("User", (), {"name": "Test User"})())
        assert 'hx-target="#search-results"' in html
        assert 'id="search-results"' in html

    def test_topbar_has_user_menu(self, jinja_env):
        template = jinja_env.get_template("partials/shared/topbar.html")
        html = template.render(user=type("User", (), {"name": "Test User"})())
        assert "user-menu" in html
        assert "Test User" in html

    def test_topbar_has_notification_bell(self, jinja_env):
        template = jinja_env.get_template("partials/shared/topbar.html")
        html = template.render(user=type("User", (), {"name": "Test User"})())
        assert "notification-bell" in html


class TestMobileNavRenders:
    """Tests that mobile nav partial renders with expected items."""

    def test_mobile_nav_has_core_items(self, jinja_env):
        template = jinja_env.get_template("partials/shared/mobile_nav.html")
        html = template.render()
        expected_items = ["Reqs", "Companies", "Vendors", "Quotes"]
        for item in expected_items:
            assert item in html, f"Mobile nav missing item: {item}"

    def test_mobile_nav_has_hx_boost(self, jinja_env):
        template = jinja_env.get_template("partials/shared/mobile_nav.html")
        html = template.render()
        assert 'hx-boost="true"' in html
        assert 'hx-target="#main-content"' in html


class TestGlobalSearchEndpoint:
    """Tests for GET /search endpoint."""

    def test_search_returns_html(self, htmx_client):
        resp = htmx_client.get("/search?q=test")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    def test_search_empty_query_returns_empty(self, htmx_client):
        resp = htmx_client.get("/search?q=")
        assert resp.status_code == 200

    def test_search_with_query_shows_no_results(self, htmx_client):
        resp = htmx_client.get("/search?q=nonexistent")
        assert resp.status_code == 200
        assert "No results" in resp.text
