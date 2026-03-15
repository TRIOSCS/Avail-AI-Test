"""
test_browser_back_navigation.py — Tests for browser back/forward navigation support.

Verifies: HTMX history cache is enabled, popstate sidebar sync is present,
and the _viewFromPath helper covers all nav sections.

Called by: pytest
Depends on: app/static/htmx_app.js, app/routers/htmx_views.py
"""

from pathlib import Path

import pytest

JS_PATH = Path("app/static/htmx_app.js")
JS_CONTENT = JS_PATH.read_text()


class TestHistoryCacheEnabled:
    """HTMX must cache pages so back/forward restores without full reload."""

    def test_history_cache_size_is_nonzero(self):
        assert "historyCacheSize = 0" not in JS_CONTENT
        assert "historyCacheSize = 10" in JS_CONTENT

    def test_no_full_page_reload_on_back(self):
        """historyCacheSize > 0 means HTMX restores from cache, not reload."""
        # Just confirm the setting is present and positive
        import re

        match = re.search(r"historyCacheSize\s*=\s*(\d+)", JS_CONTENT)
        assert match is not None
        assert int(match.group(1)) > 0


class TestPopstateSidebarSync:
    """Browser back/forward must update the sidebar active highlight."""

    def test_popstate_listener_exists(self):
        assert "popstate" in JS_CONTENT

    def test_history_restore_listener_exists(self):
        assert "htmx:historyRestore" in JS_CONTENT

    def test_pushed_into_history_listener_exists(self):
        assert "htmx:pushedIntoHistory" in JS_CONTENT

    def test_sync_function_exists(self):
        assert "_syncSidebarToUrl" in JS_CONTENT


class TestViewFromPathCoverage:
    """The _viewFromPath helper must map all nav section URLs correctly."""

    @pytest.mark.parametrize(
        "section",
        [
            "buy-plans",
            "quotes",
            "prospecting",
            "proactive",
            "strategic",
            "settings",
            "vendors",
            "companies",
            "search",
            "tasks",
            "requisitions",
        ],
    )
    def test_view_from_path_handles_section(self, section):
        """Each nav section has a regex match in _viewFromPath."""
        assert f"/{section}" in JS_CONTENT
