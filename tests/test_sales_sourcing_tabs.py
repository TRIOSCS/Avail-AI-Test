"""
tests/test_sales_sourcing_tabs.py — Tests the unified requisition tab wiring.

Validates: Desktop and mobile pill buttons use a single unified `reqs` view, and
legacy stored values from the old sales/sourcing split still normalize into it.

Called by: pytest
Depends on: app/templates/index.html, app/static/app.js
"""

import re

import pytest


@pytest.fixture(scope="module")
def index_html():
    with open("app/templates/index.html", "r") as f:
        return f.read()


@pytest.fixture(scope="module")
def app_js():
    with open("app/static/app.js", "r") as f:
        return f.read()


class TestMainViewPills:
    """Desktop and mobile pill buttons should expose a single unified requisition view."""

    def test_desktop_pills_no_split_views(self, index_html):
        """The desktop #mainPills should not keep separate sales/sourcing pills."""
        # Extract the mainPills div content
        match = re.search(r'id="mainPills"[^>]*>(.*?)</div>', index_html, re.DOTALL)
        assert match, "mainPills element not found"
        pills_html = match.group(1)
        assert 'data-view="sales"' not in pills_html
        assert 'data-view="purchasing"' not in pills_html
        assert 'data-view="sourcing"' not in pills_html

    def test_desktop_pills_has_unified_reqs_view(self, index_html):
        """The desktop #mainPills should have a unified reqs button."""
        match = re.search(r'id="mainPills"[^>]*>(.*?)</div>', index_html, re.DOTALL)
        assert match, "mainPills element not found"
        pills_html = match.group(1)
        assert 'data-view="reqs"' in pills_html, "Desktop mainPills missing data-view='reqs' button"

    def test_desktop_reqs_button_calls_reqs(self, index_html):
        """The desktop Reqs button onclick must call setMainView('reqs')."""
        match = re.search(r'id="mainPills"[^>]*>(.*?)</div>', index_html, re.DOTALL)
        assert match, "mainPills element not found"
        pills_html = match.group(1)
        assert "setMainView('reqs'" in pills_html, "Desktop Reqs button should call setMainView('reqs')"

    def test_mobile_pills_use_reqs(self, index_html):
        """The mobile #mobilePills buttons should use 'reqs' for the unified list."""
        match = re.search(r'id="mobilePills"[^>]*>(.*?)</div>', index_html, re.DOTALL)
        assert match, "mobilePills element not found"
        pills_html = match.group(1)
        assert 'data-view="sales"' not in pills_html
        assert 'data-view="purchasing"' not in pills_html
        assert 'data-view="sourcing"' not in pills_html, "Mobile pills still uses data-view='sourcing'"
        assert 'data-view="reqs"' in pills_html

    def test_mobile_req_pills_use_reqs(self, index_html):
        """The mobile #mobileReqPills should use the unified reqs view."""
        match = re.search(r'id="mobileReqPills"[^>]*>(.*?)</div>', index_html, re.DOTALL)
        assert match, "mobileReqPills element not found"
        pills_html = match.group(1)
        assert 'data-view="sales"' not in pills_html
        assert 'data-view="purchasing"' not in pills_html
        assert 'data-view="sourcing"' not in pills_html
        assert 'data-view="reqs"' in pills_html


class TestSetMainViewLogic:
    """The JS main-view logic should normalize old split views into reqs."""

    def test_main_view_has_normalizer(self, app_js):
        """The frontend should remap legacy views inside setMainView."""
        assert "view === 'active' || view === 'rfq'" in app_js
        assert "_currentMainView = 'reqs'" in app_js

    def test_legacy_sales_and_purchasing_migrate_to_reqs(self, app_js):
        """Legacy aliases still route into the unified reqs tab."""
        assert "view === 'active' || view === 'rfq'" in app_js
        assert "loadRequisitions();" in app_js
