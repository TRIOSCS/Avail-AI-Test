"""test_browser_back_navigation.py — Tests for browser back/forward navigation support.

Verifies: HTMX history cache is enabled, and nav highlighting is reactive
via Alpine.js in mobile_nav.html (no sidebar).

Called by: pytest
Depends on: app/static/htmx_app.js, app/templates/htmx/partials/shared/mobile_nav.html
"""

from pathlib import Path

import pytest

JS_PATH = Path("app/static/htmx_app.js")
JS_CONTENT = JS_PATH.read_text()

NAV_PATH = Path("app/templates/htmx/partials/shared/mobile_nav.html")
NAV_CONTENT = NAV_PATH.read_text()


class TestHistoryCacheEnabled:
    """HTMX must cache pages so back/forward restores without full reload."""

    def test_history_cache_size_is_nonzero(self):
        assert "historyCacheSize = 0" not in JS_CONTENT
        assert "historyCacheSize = 10" in JS_CONTENT

    def test_no_full_page_reload_on_back(self):
        """HistoryCacheSize > 0 means HTMX restores from cache, not reload."""
        import re

        match = re.search(r"historyCacheSize\s*=\s*(\d+)", JS_CONTENT)
        assert match is not None
        assert int(match.group(1)) > 0


class TestNavHighlightingReactive:
    """Bottom nav must reactively update active highlight on HTMX navigation."""

    def test_alpine_active_nav_state(self):
        """Nav uses Alpine activeNav state for highlighting."""
        assert "activeNav" in NAV_CONTENT

    def test_pushed_into_history_listener(self):
        """Nav listens for htmx:pushed-into-history to update active state."""
        assert "htmx:pushed-into-history" in NAV_CONTENT

    def test_url_to_nav_mapping(self):
        """Nav has URL-to-nav-id mapping for all sections."""
        assert "urlToNav" in NAV_CONTENT

    def test_click_updates_active_nav(self):
        """Clicking a nav item updates activeNav immediately."""
        assert '@click="activeNav' in NAV_CONTENT

    @pytest.mark.parametrize(
        "section",
        [
            "requisitions",
            "sightings",
            "search",
            "buy-plans",
            "crm",
            "proactive",
            "quotes",
            "prospecting",
            "settings",
        ],
    )
    def test_nav_has_section(self, section):
        """Each nav section is present in the bottom nav."""
        assert section in NAV_CONTENT


class TestNoSidebar:
    """Sidebar has been removed — app uses bottom nav only."""

    def test_no_sidebar_store(self):
        assert "store('sidebar'" not in JS_CONTENT

    def test_no_sidebar_sync(self):
        assert "_syncSidebarToUrl" not in JS_CONTENT

    def test_no_sidebar_template(self):
        assert not Path("app/templates/htmx/partials/shared/sidebar.html").exists()
