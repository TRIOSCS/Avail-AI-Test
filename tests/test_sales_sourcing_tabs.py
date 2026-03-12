"""
tests/test_sales_sourcing_tabs.py — Tests unified requisition pipeline tab wiring.

Validates: Desktop and mobile pills expose one unified requisition view
(`data-view="sales"`), with legacy purchasing/sourcing aliases normalized in JS.

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
    """Desktop and mobile pill buttons should use unified requisition view keys."""

    def test_desktop_pills_no_sourcing_data_view(self, index_html):
        """The desktop #mainPills buttons should not use data-view='sourcing'."""
        # Extract the mainPills div content
        match = re.search(r'id="mainPills"[^>]*>(.*?)</div>', index_html, re.DOTALL)
        assert match, "mainPills element not found"
        pills_html = match.group(1)
        assert 'data-view="sourcing"' not in pills_html

    def test_desktop_pills_no_purchasing_data_view(self, index_html):
        """The desktop #mainPills should not expose separate purchasing view."""
        match = re.search(r'id="mainPills"[^>]*>(.*?)</div>', index_html, re.DOTALL)
        assert match, "mainPills element not found"
        pills_html = match.group(1)
        assert 'data-view="purchasing"' not in pills_html

    def test_desktop_pills_has_unified_sales_view(self, index_html):
        """The desktop #mainPills should have unified data-view='sales' button."""
        match = re.search(r'id="mainPills"[^>]*>(.*?)</div>', index_html, re.DOTALL)
        assert match, "mainPills element not found"
        pills_html = match.group(1)
        assert 'data-view="sales"' in pills_html
        assert "setMainView('sales'" in pills_html

    def test_mobile_pills_use_unified_sales_view(self, index_html):
        """The mobile #mobilePills uses unified sales view key, no purchasing key."""
        match = re.search(r'id="mobilePills"[^>]*>(.*?)</div>', index_html, re.DOTALL)
        assert match, "mobilePills element not found"
        pills_html = match.group(1)
        assert 'data-view="sourcing"' not in pills_html, "Mobile pills still uses data-view='sourcing'"
        assert 'data-view="purchasing"' not in pills_html
        assert 'data-view="sales"' in pills_html

    def test_mobile_req_pills_use_deals_not_purchasing(self, index_html):
        """The mobile #mobileReqPills should include deals and no purchasing key."""
        match = re.search(r'id="mobileReqPills"[^>]*>(.*?)</div>', index_html, re.DOTALL)
        assert match, "mobileReqPills element not found"
        pills_html = match.group(1)
        assert 'data-view="sourcing"' not in pills_html
        assert 'data-view="purchasing"' not in pills_html
        assert 'data-view="deals"' in pills_html


class TestSetMainViewLogic:
    """The JS setMainView function must normalize legacy split-view keys."""

    def test_setmainview_normalizes_legacy_view_aliases(self, app_js):
        """setMainView uses _normalizeMainView and maps purchasing/sourcing to sales."""
        assert "function _normalizeMainView" in app_js
        assert "view === 'purchasing'" in app_js
        assert "view === 'sourcing'" in app_js
        assert "return 'sales'" in app_js

    def test_legacy_storage_migrated_to_sales(self, app_js):
        """Stored split-view values should migrate to unified sales pipeline view."""
        assert "_currentMainView === 'sourcing'" in app_js
        assert "_currentMainView === 'purchasing'" in app_js
        assert "_currentMainView = 'sales'" in app_js
