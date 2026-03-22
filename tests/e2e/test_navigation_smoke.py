"""test_navigation_smoke.py — Playwright smoke tests for bottom nav navigation.

Verifies: every nav link loads, URL updates correctly, currentView syncs,
active styling applies, back button works, direct URL access works.

Called by: pytest tests/e2e/test_navigation_smoke.py
Depends on: tests/e2e/conftest.py (authed_page, base_url fixtures)
"""

import pytest
from playwright.sync_api import Page

# 12 bottom nav items (requisitions hardcoded + 11 in loop): (id, push_url, partial_url)
# Note: trouble-tickets is NOT in the bottom nav — it's accessed via /v2/trouble-tickets directly.
NAV_ITEMS = [
    ("requisitions", "/v2/requisitions", "/v2/partials/parts/workspace"),
    ("search", "/v2/search", "/v2/partials/search"),
    ("quotes", "/v2/quotes", "/v2/partials/quotes"),
    ("companies", "/v2/companies", "/v2/partials/companies"),
    ("vendors", "/v2/vendors", "/v2/partials/vendors"),
    ("prospecting", "/v2/prospecting", "/v2/partials/prospecting"),
    ("materials", "/v2/materials", "/v2/partials/materials/workspace"),
    ("buy-plans", "/v2/buy-plans", "/v2/partials/buy-plans"),
    ("follow-ups", "/v2/follow-ups", "/v2/partials/follow-ups"),
    ("proactive", "/v2/proactive", "/v2/partials/proactive"),
    ("excess", "/v2/excess", "/v2/partials/excess"),
    ("settings", "/v2/settings", "/v2/partials/settings"),
]

# Pages accessible via direct URL but not in bottom nav
DIRECT_ACCESS_PAGES = [
    ("trouble-tickets", "/v2/trouble-tickets", "/v2/partials/trouble-tickets/workspace"),
]


def _get_current_view(page: Page) -> str:
    """Read Alpine currentView from body x-data."""
    return page.evaluate("() => document.body._x_dataStack?.[0]?.currentView || ''")


def _wait_for_nav(page: Page):
    """Wait for HTMX swap to complete."""
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(300)


class TestNavClickLoadsCorrectly:
    """Click each nav item and verify URL, currentView, and HTTP status."""

    @pytest.mark.parametrize("nav_id, push_url, partial_url", NAV_ITEMS)
    def test_nav_click(self, authed_page: Page, base_url: str, nav_id: str, push_url: str, partial_url: str):
        # Start from requisitions
        authed_page.goto(f"{base_url}/v2/requisitions", wait_until="networkidle")
        authed_page.wait_for_timeout(500)

        if nav_id == "requisitions":
            return  # Already here

        # Intercept the partial request to check status
        with authed_page.expect_response(lambda r: partial_url in r.url) as response_info:
            # Click the nav link
            nav_link = authed_page.locator(f"nav a[href='{push_url}']")
            nav_link.click()
            _wait_for_nav(authed_page)

        # Verify HTTP 200
        assert response_info.value.status == 200, f"{nav_id}: got {response_info.value.status}"

        # Verify URL
        assert authed_page.url.endswith(push_url), f"{nav_id}: URL is {authed_page.url}"

        # Verify currentView
        cv = _get_current_view(authed_page)
        assert cv == nav_id, f"{nav_id}: currentView is '{cv}'"


class TestActiveStyleApplied:
    """Verify the active nav item gets brand-500 styling."""

    @pytest.mark.parametrize("nav_id, push_url, partial_url", NAV_ITEMS)
    def test_active_class(self, authed_page: Page, base_url: str, nav_id: str, push_url: str, partial_url: str):
        authed_page.goto(f"{base_url}{push_url}", wait_until="networkidle")
        authed_page.wait_for_timeout(500)

        # Alpine reactive :class should have applied text-brand-500
        # We check via evaluate since :class is dynamic
        has_active = authed_page.evaluate(
            f"""() => {{
                const link = document.querySelector("nav a[href='{push_url}']");
                return link ? link.classList.contains('text-brand-500') : false;
            }}"""
        )
        assert has_active, f"{nav_id}: missing active class at {push_url}"


class TestBackButton:
    """Navigate A→B, press back, verify A is restored."""

    def test_back_restores_view(self, authed_page: Page, base_url: str):
        # Go to vendors
        authed_page.goto(f"{base_url}/v2/vendors", wait_until="networkidle")
        authed_page.wait_for_timeout(500)

        # Click materials
        authed_page.locator("nav a[href='/v2/materials']").click()
        _wait_for_nav(authed_page)
        assert authed_page.url.endswith("/v2/materials")

        # Press back
        authed_page.go_back()
        authed_page.wait_for_timeout(500)

        # Should be back at vendors
        assert "/v2/vendors" in authed_page.url
        assert _get_current_view(authed_page) == "vendors"


class TestDirectUrlAccess:
    """Hit each /v2/{page} directly — full page load should work."""

    @pytest.mark.parametrize("nav_id, push_url, partial_url", NAV_ITEMS + DIRECT_ACCESS_PAGES)
    def test_direct_access(self, authed_page: Page, base_url: str, nav_id: str, push_url: str, partial_url: str):
        response = authed_page.goto(f"{base_url}{push_url}", wait_until="networkidle")
        assert response.status == 200, f"{nav_id}: direct access got {response.status}"
        authed_page.wait_for_timeout(500)

        cv = _get_current_view(authed_page)
        assert cv == nav_id, f"{nav_id}: direct access currentView is '{cv}'"


class TestErrorRecovery:
    """After a failed nav request, currentView should stay on the previous value."""

    def test_failed_request_keeps_previous_view(self, authed_page: Page, base_url: str):
        # Start at vendors
        authed_page.goto(f"{base_url}/v2/vendors", wait_until="networkidle")
        authed_page.wait_for_timeout(500)
        assert _get_current_view(authed_page) == "vendors"

        # Intercept the materials partial to return 500
        authed_page.route("**/v2/partials/materials/workspace", lambda route: route.fulfill(status=500, body="error"))

        # Click materials nav
        authed_page.locator("nav a[href='/v2/materials']").click()
        authed_page.wait_for_timeout(1000)

        # currentView should still be "vendors" since the request failed
        cv = _get_current_view(authed_page)
        assert cv == "vendors", f"After failed request, currentView is '{cv}' (expected 'vendors')"

        # Clean up route intercept
        authed_page.unroute("**/v2/partials/materials/workspace")
