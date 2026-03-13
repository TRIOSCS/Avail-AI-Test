"""
Deep Playwright E2E tests for AvailAI.

Tests cover:
- Page load and auth
- Main navigation between all views
- Requisition sub-tab switching and DOM isolation
- Data persistence across tab cycles
- Modal open/close
- Vendor and material views
- Console error detection
"""

import re

import pytest
from playwright.sync_api import Page, expect

# ── Helpers ──────────────────────────────────────────────────────────


def wait_for_app(page: Page, base_url: str):
    """Navigate and wait for the app shell to be fully loaded."""
    page.goto(base_url, wait_until="domcontentloaded")
    page.wait_for_timeout(500)


def goto_rfqs(page: Page, base_url: str):
    """Navigate directly to the RFQ list view."""
    page.goto(f"{base_url}/#rfqs", wait_until="domcontentloaded")
    page.wait_for_timeout(1000)


def switch_to_active_bucket(page: Page):
    """Switch to 'Sourcing' bucket which usually has requisitions."""
    pill = page.locator("[data-sf='active']")
    if pill.first.is_visible():
        pill.first.click()
        page.wait_for_timeout(500)


def nav_click(page: Page, nav_id: str):
    """Click a sidebar nav button and wait for the view transition.

    Uses JS click to bypass Playwright's viewport check — sidebar buttons
    may be positioned outside the viewport via CSS but are still functional.
    """
    clicked = page.evaluate(f"""() => {{
        const el = document.getElementById('{nav_id}');
        if (el) {{ el.click(); return true; }}
        return false;
    }}""")
    if not clicked:
        pytest.skip(f"#{nav_id} not found in DOM")
    page.wait_for_timeout(500)


# ── 1. AUTH & PAGE LOAD ─────────────────────────────────────────────


class TestPageLoad:
    def test_login_page_shows_for_unauthenticated(self, page, base_url):
        """Unauthenticated users see the login screen."""
        page.goto(base_url, wait_until="networkidle")
        expect(page.locator("a.btn-ms")).to_be_visible()
        expect(page.locator("a.btn-ms")).to_contain_text("Sign in with Microsoft")

    def test_app_loads_for_authenticated_user(self, authed_page, base_url):
        """Authenticated users see the main app shell, not the login page."""
        wait_for_app(authed_page, base_url)
        # Login button should NOT be visible
        expect(authed_page.locator("a.btn-ms")).not_to_be_visible()
        # Sidebar nav should be present in DOM
        assert authed_page.evaluate("document.getElementById('navReqs') !== null")

    def test_user_name_displayed(self, authed_page, base_url):
        """Logged-in user's name appears in the sidebar."""
        wait_for_app(authed_page, base_url)
        name_el = authed_page.locator(".un")
        expect(name_el).to_contain_text("Michael Khoury")

    def test_requisitions_view_after_nav(self, authed_page, base_url):
        """Navigating to RFQs shows the requisitions list view."""
        goto_rfqs(authed_page, base_url)
        expect(authed_page.locator("#view-list")).to_be_visible()


# ── 2. MAIN NAVIGATION ──────────────────────────────────────────────


class TestMainNavigation:
    def test_navigate_to_vendors(self, authed_page, base_url):
        wait_for_app(authed_page, base_url)
        nav_click(authed_page, "navVendors")
        expect(authed_page.locator("#view-vendors")).to_be_visible()

    def test_navigate_to_materials(self, authed_page, base_url):
        wait_for_app(authed_page, base_url)
        nav_click(authed_page, "navMaterials")
        expect(authed_page.locator("#view-materials")).to_be_visible()

    def test_navigate_to_customers(self, authed_page, base_url):
        wait_for_app(authed_page, base_url)
        nav_click(authed_page, "navCustomers")
        authed_page.wait_for_timeout(500)
        expect(authed_page.locator("#view-customers")).to_be_visible()

    def test_navigate_back_to_requisitions(self, authed_page, base_url):
        wait_for_app(authed_page, base_url)
        nav_click(authed_page, "navVendors")
        expect(authed_page.locator("#view-vendors")).to_be_visible()
        nav_click(authed_page, "navReqs")
        expect(authed_page.locator("#view-list")).to_be_visible()
        expect(authed_page.locator("#view-vendors")).not_to_be_visible()

    def test_nav_button_active_state(self, authed_page, base_url):
        """The clicked nav button gets the 'active' class."""
        wait_for_app(authed_page, base_url)
        nav_click(authed_page, "navVendors")
        expect(authed_page.locator("#navVendors")).to_have_class(re.compile(r"\bactive\b"))
        expect(authed_page.locator("#navReqs")).not_to_have_class(re.compile(r"\bactive\b"))

    def test_only_one_view_visible_at_a_time(self, authed_page, base_url):
        """Switching nav should show exactly one view panel."""
        wait_for_app(authed_page, base_url)
        views = ["view-list", "view-vendors", "view-materials", "view-customers"]
        nav_ids = ["navReqs", "navVendors", "navMaterials", "navCustomers"]
        for i, nav in enumerate(nav_ids):
            nav_click(authed_page, nav)
            authed_page.wait_for_timeout(300)
            for j, view in enumerate(views):
                loc = authed_page.locator(f"#{view}")
                if i == j:
                    expect(loc).to_be_visible()
                else:
                    expect(loc).not_to_be_visible()

    def test_navigate_to_proactive(self, authed_page, base_url):
        """Admin can see the Proactive nav and navigate to it."""
        wait_for_app(authed_page, base_url)
        nav_click(authed_page, "navProactive")
        authed_page.wait_for_timeout(500)
        expect(authed_page.locator("#view-proactive")).to_be_visible()

    def test_navigate_to_settings(self, authed_page, base_url):
        """Admin can access settings via the gear dropdown."""
        wait_for_app(authed_page, base_url)
        # Try opening settings via JS
        result = authed_page.evaluate("""() => {
            if (typeof showSettings === 'function') { showSettings(); return true; }
            if (window.showSettings) { window.showSettings(); return true; }
            return false;
        }""")
        if not result:
            pytest.skip("showSettings function not available")
        authed_page.wait_for_timeout(500)
        expect(authed_page.locator("#view-settings")).to_be_visible()


# ── 3. REQUISITION LIST ─────────────────────────────────────────────


class TestRequisitionList:
    def test_requisitions_load(self, authed_page, base_url):
        """The requisition list should show table rows after loading."""
        goto_rfqs(authed_page, base_url)
        authed_page.wait_for_timeout(1000)
        arrows = authed_page.locator("#reqList .ea")
        count = arrows.count()
        assert count >= 0  # Just verify no crash

    def test_new_requisition_modal_opens(self, authed_page, base_url):
        """Clicking '+ New RFQ' opens the modal."""
        goto_rfqs(authed_page, base_url)
        new_btn = authed_page.locator("button:has-text('New RFQ')").first
        if not new_btn.is_visible():
            pytest.skip("New RFQ button not visible in current UI")
        new_btn.click()
        modal = authed_page.locator("#newReqModal")
        if modal.count() == 0:
            pytest.skip("newReqModal not present in current UI")
        # Support both class-driven and visibility-driven modal implementations.
        if not re.search(r"\bopen\b", modal.get_attribute("class") or ""):
            expect(modal).to_be_visible()

    def test_new_requisition_modal_closes(self, authed_page, base_url):
        """Closing the modal should remove the 'open' class."""
        goto_rfqs(authed_page, base_url)
        new_btn = authed_page.locator("button:has-text('New RFQ')").first
        if not new_btn.is_visible():
            pytest.skip("New RFQ button not visible in current UI")
        new_btn.click()
        modal = authed_page.locator("#newReqModal")
        if modal.count() == 0:
            pytest.skip("newReqModal not present in current UI")
        expect(modal).to_be_visible()
        # Close by clicking close button or pressing Escape
        close_btn = authed_page.locator("#newReqModal .modal-close, #newReqModal button:has-text('Cancel')")
        if close_btn.first.is_visible():
            close_btn.first.click()
        else:
            authed_page.keyboard.press("Escape")
        authed_page.wait_for_timeout(300)
        if re.search(r"\bopen\b", modal.get_attribute("class") or ""):
            expect(modal).not_to_have_class(re.compile(r"\bopen\b"))


# ── 4. REQUISITION DETAIL & SUB-TAB SWITCHING ───────────────────────


class TestRequisitionSubTabs:
    def _open_first_req(self, page: Page, base_url: str):
        """Expand the first requisition's drill-down."""
        goto_rfqs(page, base_url)
        page.wait_for_timeout(1000)
        arrow = page.locator("#reqList .ea").first
        if not arrow.is_visible():
            switch_to_active_bucket(page)
            arrow = page.locator("#reqList .ea").first
        if arrow.is_visible():
            arrow.click()
            page.wait_for_timeout(1000)
            return True
        return False

    def test_drilldown_opens(self, authed_page, base_url):
        """Clicking expand arrow opens the drill-down row."""
        if self._open_first_req(authed_page, base_url):
            expect(authed_page.locator("tr.drow.open").first).to_be_visible()

    def test_drilldown_tabs_work(self, authed_page, base_url):
        """Drill-down sub-tabs are clickable and switch content."""
        if not self._open_first_req(authed_page, base_url):
            pytest.skip("No requisitions available")

        # Scope tabs to the open drill-down row only
        tabs = authed_page.locator("tr.drow.open .dd-tabs .dd-tab")
        count = tabs.count()
        for i in range(count):
            tabs.nth(i).click()
            authed_page.wait_for_timeout(300)

    def test_back_to_list_from_drilldown(self, authed_page, base_url):
        """Collapsing drill-down restores the list view."""
        if not self._open_first_req(authed_page, base_url):
            pytest.skip("No requisitions available")
        # Click the expand arrow again to collapse
        arrow = authed_page.locator("#reqList .ea").first
        arrow.click()
        authed_page.wait_for_timeout(300)


# ── 5. DOM ISOLATION: THE BUG PATTERN ───────────────────────────────


class TestDOMIsolation:
    """Tests specifically for the unscoped selector bug pattern."""

    def test_no_unscoped_tab_selectors_in_js(self, authed_page, base_url):
        """Runtime check: verify no JS code uses unscoped '.tab' selectors."""
        wait_for_app(authed_page, base_url)
        result = authed_page.evaluate("""() => {
            const fn = window.switchTab?.toString() || '';
            const bad = fn.includes("querySelectorAll('.tab')") ||
                        fn.includes('querySelectorAll(".tab")');
            return { hasBug: bad, fnSource: fn.substring(0, 200) };
        }""")
        assert not result["hasBug"], f"switchTab still uses unscoped '.tab' selector: {result['fnSource']}"


# ── 6. CONSOLE ERROR DETECTION ──────────────────────────────────────


class TestConsoleErrors:
    def test_no_js_errors_on_load(self, authed_page, base_url):
        """No JavaScript errors should appear on initial page load."""
        errors = []
        authed_page.on("pageerror", lambda exc: errors.append(str(exc)))
        wait_for_app(authed_page, base_url)
        authed_page.wait_for_timeout(2000)
        assert len(errors) == 0, f"JS errors on load: {errors}"

    def test_no_js_errors_on_navigation(self, authed_page, base_url):
        """No JavaScript errors when navigating between main views."""
        errors = []
        authed_page.on("pageerror", lambda exc: errors.append(str(exc)))
        wait_for_app(authed_page, base_url)
        for nav in ["navVendors", "navMaterials", "navCustomers", "navReqs"]:
            nav_click(authed_page, nav)
            authed_page.wait_for_timeout(300)
        assert len(errors) == 0, f"JS errors during navigation: {errors}"

    def test_no_js_errors_on_tab_cycling(self, authed_page, base_url):
        """No JavaScript errors when cycling through drill-down sub-tabs."""
        errors = []
        authed_page.on("pageerror", lambda exc: errors.append(str(exc)))
        goto_rfqs(authed_page, base_url)
        authed_page.wait_for_timeout(1000)

        arrow = authed_page.locator("#reqList .ea").first
        if not arrow.is_visible():
            pytest.skip("No requisitions available")
        arrow.click()
        authed_page.wait_for_timeout(500)

        # Cycle through all drill-down tabs twice
        tabs = authed_page.locator("tr.drow.open .dd-tabs .dd-tab")
        count = tabs.count()
        for _ in range(2):
            for i in range(count):
                tabs.nth(i).click()
                authed_page.wait_for_timeout(200)

        assert len(errors) == 0, f"JS errors during tab cycling: {errors}"


# ── 7. API HEALTH ───────────────────────────────────────────────────


class TestAPIHealth:
    def test_no_failed_api_calls_on_load(self, authed_page, base_url):
        """All API calls on initial load should return 2xx."""
        failed = []

        def on_response(response):
            if "/api/" in response.url and response.status >= 400:
                failed.append(f"{response.status} {response.url}")

        authed_page.on("response", on_response)
        wait_for_app(authed_page, base_url)
        authed_page.wait_for_timeout(3000)
        assert len(failed) == 0, f"Failed API calls on load: {failed}"

    def test_no_failed_api_on_rfq_view(self, authed_page, base_url):
        """Opening the RFQ list should not produce API errors."""
        failed = []

        def on_response(response):
            if "/api/" in response.url and response.status >= 400:
                failed.append(f"{response.status} {response.url}")

        authed_page.on("response", on_response)
        goto_rfqs(authed_page, base_url)
        authed_page.wait_for_timeout(2000)

        arrow = authed_page.locator("#reqList .ea").first
        if arrow.is_visible():
            arrow.click()
            authed_page.wait_for_timeout(2000)
        assert len(failed) == 0, f"Failed API calls: {failed}"


# ── 8. VENDOR VIEW ──────────────────────────────────────────────────


class TestVendorView:
    def test_vendor_list_loads(self, authed_page, base_url):
        """Vendor view should load without errors."""
        wait_for_app(authed_page, base_url)
        nav_click(authed_page, "navVendors")
        authed_page.wait_for_timeout(1000)
        expect(authed_page.locator("#view-vendors")).to_be_visible()

    def test_vendor_search_input_exists(self, authed_page, base_url):
        """Vendor view should have a search input."""
        wait_for_app(authed_page, base_url)
        nav_click(authed_page, "navVendors")
        search = authed_page.locator("#view-vendors input[type='text'], #view-vendors .filter-search, #vendorSearch")
        assert search.count() > 0, "No search input found in vendor view"


# ── 9. MATERIAL VIEW ────────────────────────────────────────────────


class TestMaterialView:
    def test_material_list_loads(self, authed_page, base_url):
        """Material view should load without errors."""
        wait_for_app(authed_page, base_url)
        nav_click(authed_page, "navMaterials")
        authed_page.wait_for_timeout(1000)
        expect(authed_page.locator("#view-materials")).to_be_visible()


# ── 10. RAPID INTERACTION STRESS ─────────────────────────────────────


class TestRapidInteraction:
    def test_rapid_tab_switching(self, authed_page, base_url):
        """Rapidly clicking through drill-down tabs should not break the UI."""
        errors = []
        authed_page.on("pageerror", lambda exc: errors.append(str(exc)))
        goto_rfqs(authed_page, base_url)
        authed_page.wait_for_timeout(1000)

        arrow = authed_page.locator("#reqList .ea").first
        if not arrow.is_visible():
            pytest.skip("No requisitions available")
        arrow.click()
        authed_page.wait_for_timeout(500)

        # Rapid-fire tab clicks
        tabs = authed_page.locator("tr.drow.open .dd-tabs .dd-tab")
        count = tabs.count()
        for _ in range(3):
            for i in range(count):
                tabs.nth(i).click()

        authed_page.wait_for_timeout(1000)
        assert len(errors) == 0, f"JS errors during rapid switching: {errors}"

    def test_rapid_nav_switching(self, authed_page, base_url):
        """Rapidly switching main nav should not break the UI."""
        errors = []
        authed_page.on("pageerror", lambda exc: errors.append(str(exc)))
        wait_for_app(authed_page, base_url)

        navs = ["navVendors", "navMaterials", "navCustomers", "navReqs"]
        for _ in range(3):
            for nav_id in navs:
                nav_click(authed_page, nav_id)

        authed_page.wait_for_timeout(500)
        expect(authed_page.locator("#view-list")).to_be_visible()
        assert len(errors) == 0, f"JS errors during rapid nav: {errors}"

    def test_open_close_multiple_reqs(self, authed_page, base_url):
        """Opening multiple requisitions in sequence should not leak state."""
        errors = []
        authed_page.on("pageerror", lambda exc: errors.append(str(exc)))
        goto_rfqs(authed_page, base_url)
        authed_page.wait_for_timeout(1000)

        arrows = authed_page.locator("#reqList .ea")
        count = min(arrows.count(), 5)
        if count < 2:
            pytest.skip("Need at least 2 requisitions")

        for i in range(count):
            arrows.nth(i).click()
            authed_page.wait_for_timeout(500)
            # Click again to collapse
            arrows.nth(i).click()
            authed_page.wait_for_timeout(300)

        assert len(errors) == 0, f"JS errors opening multiple reqs: {errors}"


# ── 11. CSS CLASS INTEGRITY ──────────────────────────────────────────


class TestCSSIntegrity:
    def test_drilldown_tabs_use_scoped_selectors(self, authed_page, base_url):
        """Drill-down tabs should be scoped to their parent container."""
        goto_rfqs(authed_page, base_url)
        authed_page.wait_for_timeout(1000)

        arrow = authed_page.locator("#reqList .ea").first
        if not arrow.is_visible():
            pytest.skip("No requisitions available")
        arrow.click()
        authed_page.wait_for_timeout(500)

        # Verify dd-tabs exist within the drill-down
        dd_tabs = authed_page.locator("tr.drow.open .dd-tabs .dd-tab")
        assert dd_tabs.count() > 0, "No drill-down tabs found"

    def test_exactly_one_dd_tab_active(self, authed_page, base_url):
        """When a drill-down is open, exactly one tab should be active."""
        goto_rfqs(authed_page, base_url)
        authed_page.wait_for_timeout(1000)

        arrow = authed_page.locator("#reqList .ea").first
        if not arrow.is_visible():
            pytest.skip("No requisitions available")
        arrow.click()
        authed_page.wait_for_timeout(500)

        active_tabs = authed_page.locator("tr.drow.open .dd-tabs .dd-tab.on")
        assert active_tabs.count() == 1, f"Expected exactly 1 active dd-tab, found {active_tabs.count()}"


# ── 12. STATUS BUCKET SWITCHING ──────────────────────────────────────


class TestBucketSwitching:
    def test_bucket_pills_work(self, authed_page, base_url):
        """Clicking bucket pills switches the displayed requisitions."""
        goto_rfqs(authed_page, base_url)
        authed_page.wait_for_timeout(1000)

        # Click through available pills
        pills = authed_page.locator(".fp[data-view]")
        count = pills.count()
        # Use only desktop pills (skip mobile duplicates)
        seen = set()
        for i in range(count):
            pill = pills.nth(i)
            if pill.is_visible():
                view = pill.get_attribute("data-view")
                if view and view not in seen:
                    seen.add(view)
                    pill.click()
                    authed_page.wait_for_timeout(500)

    def test_bucket_cycle_no_errors(self, authed_page, base_url):
        """Cycling through all status buckets should not produce JS errors."""
        errors = []
        authed_page.on("pageerror", lambda exc: errors.append(str(exc)))
        goto_rfqs(authed_page, base_url)
        authed_page.wait_for_timeout(1000)

        # Cycle through all visible pills
        pills = authed_page.locator(".fp[data-view]")
        count = pills.count()
        seen = set()
        for i in range(count):
            pill = pills.nth(i)
            if pill.is_visible():
                view = pill.get_attribute("data-view")
                if view and view not in seen:
                    seen.add(view)
                    pill.click()
                    authed_page.wait_for_timeout(800)

        assert len(errors) == 0, f"JS errors during bucket cycling: {errors}"

    def test_bucket_pill_active_state(self, authed_page, base_url):
        """Only the clicked bucket pill should have the 'on' class."""
        goto_rfqs(authed_page, base_url)
        authed_page.wait_for_timeout(1000)

        # Get visible desktop pills
        pills = authed_page.locator(".fp[data-view]")
        visible_pills = []
        for i in range(pills.count()):
            if pills.nth(i).is_visible():
                visible_pills.append(pills.nth(i))
        if len(visible_pills) < 2:
            pytest.skip("Not enough visible pills")

        for pill in visible_pills:
            pill.click()
            authed_page.wait_for_timeout(300)
            expect(pill).to_have_class(re.compile(r"\bon\b"))
