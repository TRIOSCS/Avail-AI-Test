"""
tests/test_sales_sourcing_tabs.py — Tests that sales/sourcing tab views are wired correctly.

Validates: Desktop and mobile pill buttons use consistent data-view values that
match the JS setMainView() logic (which expects 'sales' and 'purchasing').

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
    """Desktop and mobile pill buttons must use 'purchasing' (not 'sourcing') as data-view."""

    def test_desktop_pills_no_sourcing_data_view(self, index_html):
        """The desktop #mainPills buttons should not use data-view='sourcing'."""
        # Extract the mainPills div content
        match = re.search(r'id="mainPills"[^>]*>(.*?)</div>', index_html, re.DOTALL)
        assert match, "mainPills element not found"
        pills_html = match.group(1)
        assert 'data-view="sourcing"' not in pills_html, (
            "Desktop mainPills still uses data-view='sourcing' — should be 'purchasing'"
        )

    def test_desktop_pills_has_purchasing_view(self, index_html):
        """The desktop #mainPills should have a purchasing button."""
        match = re.search(r'id="mainPills"[^>]*>(.*?)</div>', index_html, re.DOTALL)
        assert match, "mainPills element not found"
        pills_html = match.group(1)
        assert 'data-view="purchasing"' in pills_html, "Desktop mainPills missing data-view='purchasing' button"

    def test_desktop_sourcing_button_calls_purchasing(self, index_html):
        """The desktop Sourcing button onclick must call setMainView('purchasing')."""
        match = re.search(r'id="mainPills"[^>]*>(.*?)</div>', index_html, re.DOTALL)
        assert match, "mainPills element not found"
        pills_html = match.group(1)
        assert "setMainView('purchasing'" in pills_html, "Desktop Sourcing button should call setMainView('purchasing')"

    def test_mobile_pills_use_purchasing(self, index_html):
        """The mobile #mobilePills buttons should use 'purchasing' not 'sourcing'."""
        match = re.search(r'id="mobilePills"[^>]*>(.*?)</div>', index_html, re.DOTALL)
        assert match, "mobilePills element not found"
        pills_html = match.group(1)
        assert 'data-view="sourcing"' not in pills_html, "Mobile pills still uses data-view='sourcing'"
        assert 'data-view="purchasing"' in pills_html

    def test_mobile_req_pills_use_purchasing(self, index_html):
        """The mobile #mobileReqPills should use 'purchasing' not 'sourcing'."""
        match = re.search(r'id="mobileReqPills"[^>]*>(.*?)</div>', index_html, re.DOTALL)
        assert match, "mobileReqPills element not found"
        pills_html = match.group(1)
        assert 'data-view="sourcing"' not in pills_html
        assert 'data-view="purchasing"' in pills_html


class TestSetMainViewLogic:
    """The JS setMainView function must handle 'purchasing' view correctly."""

    def test_setmainview_handles_purchasing(self, app_js):
        """setMainView should have a branch for view === 'purchasing'."""
        assert "view === 'purchasing'" in app_js or 'view === "purchasing"' in app_js, (
            "setMainView does not check for 'purchasing' view"
        )

    def test_legacy_sourcing_migrated(self, app_js):
        """Stored 'sourcing' value should be migrated to 'purchasing'."""
        assert "sourcing') _currentMainView = 'purchasing'" in app_js, (
            "Missing legacy migration from 'sourcing' to 'purchasing'"
        )
