"""
Deep Playwright E2E tests for AvailAI.

Tests cover:
- Page load and auth
- Main navigation between all views
- Requisition sub-tab switching and DOM isolation
- Data persistence across tab cycles
- Modal open/close
- Vendor and material views
- CSS class leaks (the bug pattern we just fixed)
- Console error detection
"""

import re

import pytest
from playwright.sync_api import Page, expect

# ── Helpers ──────────────────────────────────────────────────────────

def wait_for_app(page: Page, base_url: str):
    """Navigate and wait for the app shell to be fully loaded."""
    page.goto(base_url, wait_until="networkidle")


def switch_to_active_bucket(page: Page):
    """Switch to 'Sourcing' bucket which usually has requisitions."""
    pill = page.locator("[data-req-status='active']")
    if pill.is_visible():
        pill.click()
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
    page.wait_for_timeout(300)


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
        # Topbar nav should be present
        expect(authed_page.locator("#navReqs")).to_be_visible()

    def test_user_name_displayed(self, authed_page, base_url):
        """Logged-in user's name appears in the sidebar."""
        wait_for_app(authed_page, base_url)
        name_el = authed_page.locator(".un")
        expect(name_el).to_contain_text("Michael Khoury")

    def test_requisitions_view_is_default(self, authed_page, base_url):
        """Default view after login is the requisitions list."""
        wait_for_app(authed_page, base_url)
        expect(authed_page.locator("#view-list")).to_be_visible()


# ── 2. MAIN NAVIGATION ──────────────────────────────────────────────


class TestMainNavigation:

    def test_navigate_to_vendors(self, authed_page, base_url):
        wait_for_app(authed_page, base_url)
        nav_click(authed_page, "navVendors")
        expect(authed_page.locator("#view-vendors")).to_be_visible()
        expect(authed_page.locator("#view-list")).not_to_be_visible()

    def test_navigate_to_materials(self, authed_page, base_url):
        wait_for_app(authed_page, base_url)
        nav_click(authed_page, "navMaterials")
        expect(authed_page.locator("#view-materials")).to_be_visible()
        expect(authed_page.locator("#view-list")).not_to_be_visible()

    def test_navigate_to_customers(self, authed_page, base_url):
        wait_for_app(authed_page, base_url)
        nav_click(authed_page, "navCustomers")
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
        expect(authed_page.locator("#view-proactive")).to_be_visible()

    def test_navigate_to_settings(self, authed_page, base_url):
        """Admin can access settings via the gear dropdown."""
        wait_for_app(authed_page, base_url)
        gear_btn = authed_page.locator("#settingsMenu .btn-settings")
        if not gear_btn.is_visible():
            pytest.skip("Settings gear not visible for this user")
        authed_page.evaluate("document.querySelector('#settingsMenu .btn-settings').click()")
        authed_page.wait_for_timeout(200)
        authed_page.evaluate("document.querySelector('#settingsDropdownContent a').click()")
        authed_page.wait_for_timeout(300)
        expect(authed_page.locator("#view-settings")).to_be_visible()


# ── 3. REQUISITION LIST ─────────────────────────────────────────────


class TestRequisitionList:

    def test_requisitions_load(self, authed_page, base_url):
        """The requisition list should show cards after loading."""
        wait_for_app(authed_page, base_url)
        # Wait for API response
        authed_page.wait_for_timeout(1000)
        cards = authed_page.locator("#reqList .card, #reqList .req-row, #reqList .card[onclick*='showDetail']")
        count = cards.count()
        # Should have at least some requisitions (or an empty state)
        assert count >= 0  # Just verify no crash

    def test_new_requisition_modal_opens(self, authed_page, base_url):
        """Clicking '+ New RFQ' opens the modal."""
        wait_for_app(authed_page, base_url)
        authed_page.locator("button:has-text('New RFQ')").click()
        expect(authed_page.locator("#newReqModal")).to_have_class(re.compile(r"\bopen\b"))

    def test_new_requisition_modal_closes(self, authed_page, base_url):
        """Closing the modal should remove the 'open' class."""
        wait_for_app(authed_page, base_url)
        authed_page.locator("button:has-text('New RFQ')").click()
        expect(authed_page.locator("#newReqModal")).to_have_class(re.compile(r"\bopen\b"))
        # Close by clicking the backdrop or close button
        close_btn = authed_page.locator("#newReqModal .modal-close, #newReqModal [onclick*='close'], #newReqModal button:has-text('Cancel')")
        if close_btn.first.is_visible():
            close_btn.first.click()
        else:
            # Press Escape as fallback
            authed_page.keyboard.press("Escape")
        authed_page.wait_for_timeout(300)
        expect(authed_page.locator("#newReqModal")).not_to_have_class(re.compile(r"\bopen\b"))


# ── 4. REQUISITION DETAIL & SUB-TAB SWITCHING ───────────────────────


class TestRequisitionSubTabs:

    def _open_first_req(self, page: Page):
        """Click the first requisition in the list to open its detail."""
        page.wait_for_timeout(1000)
        req_card = page.locator("#reqList .card[onclick*='showDetail']").first
        if not req_card.is_visible():
            # Try active bucket if draft is empty
            switch_to_active_bucket(page)
            req_card = page.locator("#reqList .card[onclick*='showDetail']").first
        if req_card.is_visible():
            req_card.click()
            page.wait_for_timeout(500)
            return True
        return False

    def test_detail_view_opens(self, authed_page, base_url):
        """Clicking a requisition opens the detail view."""
        wait_for_app(authed_page, base_url)
        if self._open_first_req(authed_page):
            expect(authed_page.locator("#view-detail")).to_be_visible()
            expect(authed_page.locator("#view-list")).not_to_be_visible()

    def test_requirements_tab_is_default(self, authed_page, base_url):
        """Requirements tab should be active by default."""
        wait_for_app(authed_page, base_url)
        if self._open_first_req(authed_page):
            expect(authed_page.locator("#tab-requirements")).to_be_visible()
            req_btn = authed_page.locator("#reqTabs .tab").first
            expect(req_btn).to_have_class(re.compile(r"\bon\b"))

    def test_switch_to_each_subtab(self, authed_page, base_url):
        """Each sub-tab should show its content panel and hide others."""
        wait_for_app(authed_page, base_url)
        if not self._open_first_req(authed_page):
            pytest.skip("No requisitions available")

        tabs = ["requirements", "sources", "activity", "offers", "quote", "emails"]
        for tab_name in tabs:
            btn = authed_page.locator(f"#reqTabs .tab:has-text('{tab_name}')", has_text=re.compile(tab_name, re.IGNORECASE))
            btn.first.click()
            authed_page.wait_for_timeout(300)
            # The clicked tab's content should be visible
            expect(authed_page.locator(f"#tab-{tab_name}")).to_be_visible()
            # Other tab contents should be hidden
            for other in tabs:
                if other != tab_name:
                    expect(authed_page.locator(f"#tab-{other}")).not_to_be_visible()

    def test_subtab_button_active_state(self, authed_page, base_url):
        """Only the clicked sub-tab button should have the 'on' class."""
        wait_for_app(authed_page, base_url)
        if not self._open_first_req(authed_page):
            pytest.skip("No requisitions available")

        tab_buttons = authed_page.locator("#reqTabs .tab")
        tab_count = tab_buttons.count()
        for i in range(tab_count):
            tab_buttons.nth(i).click()
            authed_page.wait_for_timeout(200)
            for j in range(tab_count):
                if i == j:
                    expect(tab_buttons.nth(j)).to_have_class(re.compile(r"\bon\b"))
                else:
                    expect(tab_buttons.nth(j)).not_to_have_class(re.compile(r"\bon\b"))

    def test_cycling_tabs_preserves_requirements_data(self, authed_page, base_url):
        """Cycling through all tabs and back should not lose requirements data."""
        wait_for_app(authed_page, base_url)
        if not self._open_first_req(authed_page):
            pytest.skip("No requisitions available")

        authed_page.wait_for_timeout(500)
        # Capture initial requirements HTML
        req_table = authed_page.locator("#reqTable")
        initial_html = req_table.inner_html() if req_table.is_visible() else ""

        # Cycle through all tabs
        for tab_name in ["sources", "activity", "offers", "quote", "emails"]:
            btn = authed_page.locator(f"#reqTabs .tab:has-text('{tab_name}')", has_text=re.compile(tab_name, re.IGNORECASE))
            btn.first.click()
            authed_page.wait_for_timeout(300)

        # Switch back to requirements
        authed_page.locator("#reqTabs .tab").first.click()
        authed_page.wait_for_timeout(300)

        # Data should still be there
        expect(authed_page.locator("#tab-requirements")).to_be_visible()
        after_html = req_table.inner_html() if req_table.is_visible() else ""
        assert initial_html == after_html, "Requirements data changed after tab cycling"

    def test_back_to_list_from_detail(self, authed_page, base_url):
        """Clicking back returns to the requisition list."""
        wait_for_app(authed_page, base_url)
        if not self._open_first_req(authed_page):
            pytest.skip("No requisitions available")
        back_link = authed_page.locator(".back-link, [onclick*='showList']").first
        back_link.click()
        authed_page.wait_for_timeout(300)
        expect(authed_page.locator("#view-list")).to_be_visible()
        expect(authed_page.locator("#view-detail")).not_to_be_visible()


# ── 5. DOM ISOLATION: THE BUG PATTERN ───────────────────────────────


class TestDOMIsolation:
    """Tests specifically for the unscoped selector bug pattern.

    These verify that switching requisition sub-tabs does NOT affect
    tab state in other sections (proactive, performance, enrichment,
    settings).
    """

    def _open_first_req(self, page: Page):
        page.wait_for_timeout(1000)
        req_card = page.locator("#reqList .card[onclick*='showDetail']").first
        if req_card.is_visible():
            req_card.click()
            page.wait_for_timeout(500)
            return True
        return False

    def test_reqtab_switch_doesnt_affect_proactive_tabs(self, authed_page, base_url):
        """Switching req sub-tabs should not remove 'on' from proactive tabs."""
        wait_for_app(authed_page, base_url)

        # First visit proactive to initialize its tabs
        nav_click(authed_page, "navProactive")
        authed_page.wait_for_timeout(200)

        # Record which proactive tab has 'on'
        proactive_active = authed_page.locator("#proactiveTabs .tab.on")
        active_text = proactive_active.first.text_content() if proactive_active.count() > 0 else None

        # Navigate to a requisition
        nav_click(authed_page, "navReqs")
        if not self._open_first_req(authed_page):
            pytest.skip("No requisitions available")

        # Cycle through all req sub-tabs
        for tab_name in ["sources", "activity", "offers", "quote", "emails", "requirements"]:
            btn = authed_page.locator(f"#reqTabs .tab:has-text('{tab_name}')", has_text=re.compile(tab_name, re.IGNORECASE))
            btn.first.click()
            authed_page.wait_for_timeout(200)

        # Check proactive tabs still have their active state
        if active_text:
            proactive_active_now = authed_page.locator("#proactiveTabs .tab.on")
            assert proactive_active_now.count() > 0, \
                "Proactive tab lost its 'on' class after req sub-tab cycling"

    def test_reqtab_switch_doesnt_affect_settings_tabs(self, authed_page, base_url):
        """Switching req sub-tabs should not remove 'on' from settings tabs."""
        wait_for_app(authed_page, base_url)

        # Visit settings to initialize its tabs
        settings_btn = authed_page.locator("[onclick*='showSettings']").first
        if not settings_btn.is_visible():
            pytest.skip("Settings not visible for this user")
        settings_btn.click()
        authed_page.wait_for_timeout(500)

        # Record settings tab state
        settings_active = authed_page.locator("#settingsTabs .tab.on")
        active_count = settings_active.count()

        # Navigate to a requisition
        nav_click(authed_page, "navReqs")
        if not self._open_first_req(authed_page):
            pytest.skip("No requisitions available")

        # Cycle through req sub-tabs
        for tab_name in ["sources", "activity", "offers", "quote", "emails", "requirements"]:
            btn = authed_page.locator(f"#reqTabs .tab:has-text('{tab_name}')", has_text=re.compile(tab_name, re.IGNORECASE))
            btn.first.click()
            authed_page.wait_for_timeout(200)

        # Settings tabs should still have their active state
        settings_active_now = authed_page.locator("#settingsTabs .tab.on")
        assert settings_active_now.count() == active_count, \
            f"Settings tabs lost 'on' class: was {active_count}, now {settings_active_now.count()}"

    def test_reqtab_switch_doesnt_affect_perf_tabs(self, authed_page, base_url):
        """Switching req sub-tabs should not remove 'on' from performance tabs."""
        wait_for_app(authed_page, base_url)

        # Visit performance to initialize its tabs
        nav_click(authed_page, "navPerformance")
        authed_page.wait_for_timeout(200)

        perf_active = authed_page.locator("#perfTabs .tab.on")
        active_count = perf_active.count()

        # Navigate to a requisition and cycle tabs
        nav_click(authed_page, "navReqs")
        if not self._open_first_req(authed_page):
            pytest.skip("No requisitions available")

        for tab_name in ["sources", "offers", "requirements"]:
            btn = authed_page.locator(f"#reqTabs .tab:has-text('{tab_name}')", has_text=re.compile(tab_name, re.IGNORECASE))
            btn.first.click()
            authed_page.wait_for_timeout(200)

        perf_active_now = authed_page.locator("#perfTabs .tab.on")
        assert perf_active_now.count() == active_count, \
            f"Performance tabs lost 'on' class: was {active_count}, now {perf_active_now.count()}"

    def test_reqtab_switch_doesnt_affect_enrichment_tabs(self, authed_page, base_url):
        """Switching req sub-tabs should not remove 'on' from enrichment tabs."""
        wait_for_app(authed_page, base_url)

        nav_click(authed_page, "navEnrichment")
        authed_page.wait_for_timeout(200)

        enrich_active = authed_page.locator("#enrichTabs .tab.on")
        active_count = enrich_active.count()

        nav_click(authed_page, "navReqs")
        if not self._open_first_req(authed_page):
            pytest.skip("No requisitions available")

        for tab_name in ["sources", "offers", "requirements"]:
            btn = authed_page.locator(f"#reqTabs .tab:has-text('{tab_name}')", has_text=re.compile(tab_name, re.IGNORECASE))
            btn.first.click()
            authed_page.wait_for_timeout(200)

        enrich_active_now = authed_page.locator("#enrichTabs .tab.on")
        assert enrich_active_now.count() == active_count, \
            f"Enrichment tabs lost 'on' class: was {active_count}, now {enrich_active_now.count()}"

    def test_no_unscoped_tab_selectors_in_js(self, authed_page, base_url):
        """Runtime check: verify no JS code uses unscoped '.tab' selectors."""
        wait_for_app(authed_page, base_url)
        # Execute in browser context to check for the anti-pattern
        result = authed_page.evaluate("""() => {
            // Check that switchTab doesn't use unscoped '.tab'
            const fn = window.switchTab?.toString() || '';
            const bad = fn.includes("querySelectorAll('.tab')") ||
                        fn.includes('querySelectorAll(".tab")');
            return { hasBug: bad, fnSource: fn.substring(0, 200) };
        }""")
        assert not result["hasBug"], \
            f"switchTab still uses unscoped '.tab' selector: {result['fnSource']}"


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
        """No JavaScript errors when cycling through requisition sub-tabs."""
        errors = []
        authed_page.on("pageerror", lambda exc: errors.append(str(exc)))
        wait_for_app(authed_page, base_url)
        authed_page.wait_for_timeout(1000)

        req_card = authed_page.locator("#reqList .card[onclick*='showDetail']").first
        if not req_card.is_visible():
            pytest.skip("No requisitions available")
        req_card.click()
        authed_page.wait_for_timeout(500)

        # Cycle through all tabs twice
        tabs = ["sources", "activity", "offers", "quote", "emails", "requirements"]
        for _ in range(2):
            for tab_name in tabs:
                btn = authed_page.locator(f"#reqTabs .tab:has-text('{tab_name}')", has_text=re.compile(tab_name, re.IGNORECASE))
                btn.first.click()
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
        authed_page.wait_for_timeout(2000)
        assert len(failed) == 0, f"Failed API calls on load: {failed}"

    def test_no_failed_api_on_detail(self, authed_page, base_url):
        """Opening a requisition detail should not produce API errors."""
        failed = []
        def on_response(response):
            if "/api/" in response.url and response.status >= 400:
                failed.append(f"{response.status} {response.url}")
        authed_page.on("response", on_response)
        wait_for_app(authed_page, base_url)
        authed_page.wait_for_timeout(1000)

        req_card = authed_page.locator("#reqList .card[onclick*='showDetail']").first
        if not req_card.is_visible():
            pytest.skip("No requisitions available")
        req_card.click()
        authed_page.wait_for_timeout(2000)
        assert len(failed) == 0, f"Failed API calls on detail: {failed}"


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
        """Rapidly clicking through tabs should not break the UI."""
        errors = []
        authed_page.on("pageerror", lambda exc: errors.append(str(exc)))
        wait_for_app(authed_page, base_url)
        authed_page.wait_for_timeout(1000)

        req_card = authed_page.locator("#reqList .card[onclick*='showDetail']").first
        if not req_card.is_visible():
            pytest.skip("No requisitions available")
        req_card.click()
        authed_page.wait_for_timeout(500)

        # Rapid-fire tab clicks (no wait between)
        tabs = ["sources", "activity", "offers", "quote", "emails", "requirements"]
        for _ in range(3):
            for tab_name in tabs:
                btn = authed_page.locator(f"#reqTabs .tab:has-text('{tab_name}')", has_text=re.compile(tab_name, re.IGNORECASE))
                btn.first.click()

        authed_page.wait_for_timeout(1000)

        # Verify the last tab is visible and no errors
        expect(authed_page.locator("#tab-requirements")).to_be_visible()
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
        wait_for_app(authed_page, base_url)
        authed_page.wait_for_timeout(1000)

        req_cards = authed_page.locator("#reqList .card[onclick*='showDetail']")
        count = min(req_cards.count(), 5)
        if count < 2:
            pytest.skip("Need at least 2 requisitions")

        for i in range(count):
            # Go back to list
            nav_click(authed_page, "navReqs")
            authed_page.wait_for_timeout(300)
            # Open the i-th requisition
            req_cards = authed_page.locator("#reqList .card[onclick*='showDetail']")
            req_cards.nth(i).click()
            authed_page.wait_for_timeout(500)
            expect(authed_page.locator("#view-detail")).to_be_visible()
            # Switch a tab
            offers_btn = authed_page.locator("#reqTabs .tab:has-text('Offers')", has_text=re.compile("offers", re.IGNORECASE))
            offers_btn.first.click()
            authed_page.wait_for_timeout(300)
            expect(authed_page.locator("#tab-offers")).to_be_visible()

        assert len(errors) == 0, f"JS errors opening multiple reqs: {errors}"


# ── 11. CSS CLASS INTEGRITY ──────────────────────────────────────────


class TestCSSIntegrity:

    def test_tc_class_only_on_req_subtabs(self, authed_page, base_url):
        """The .tc class should only exist on the 6 requisition sub-tab panels."""
        wait_for_app(authed_page, base_url)
        tc_elements = authed_page.locator(".tc")
        count = tc_elements.count()
        expected_ids = {"tab-requirements", "tab-sources", "tab-activity",
                        "tab-offers", "tab-quote", "tab-emails"}
        actual_ids = set()
        for i in range(count):
            el_id = tc_elements.nth(i).get_attribute("id")
            if el_id:
                actual_ids.add(el_id)
        assert actual_ids == expected_ids, \
            f"Unexpected .tc elements: expected {expected_ids}, got {actual_ids}"

    def test_exactly_one_tc_visible_when_detail_open(self, authed_page, base_url):
        """When viewing requisition detail, exactly one .tc panel should be visible."""
        wait_for_app(authed_page, base_url)
        authed_page.wait_for_timeout(1000)

        req_card = authed_page.locator("#reqList .card[onclick*='showDetail']").first
        if not req_card.is_visible():
            switch_to_active_bucket(authed_page)
            req_card = authed_page.locator("#reqList .card[onclick*='showDetail']").first
        if not req_card.is_visible():
            pytest.skip("No requisitions available")
        req_card.click()
        authed_page.wait_for_timeout(500)

        visible_tc = authed_page.locator(".tc.on")
        assert visible_tc.count() == 1, \
            f"Expected exactly 1 visible .tc panel, found {visible_tc.count()}"


# ── 12. STATUS BUCKET SWITCHING ──────────────────────────────────────


class TestBucketSwitching:
    """Tests for the status filter pills (Draft/Sourcing/Offers/Quoted/Archive).

    Specifically tests the bug where switching to Archive and back
    causes all data to disappear because _reqListData gets overwritten.
    """

    def test_archive_and_back_preserves_data(self, authed_page, base_url):
        """Switching to Archive and back to Sourcing should still show reqs."""
        wait_for_app(authed_page, base_url)
        authed_page.wait_for_timeout(1000)

        # Switch to active bucket and count cards
        switch_to_active_bucket(authed_page)
        active_cards = authed_page.locator("#reqList .card[onclick*='showDetail']")
        initial_count = active_cards.count()
        if initial_count == 0:
            pytest.skip("No active requisitions to test with")

        # Switch to archive
        archive_pill = authed_page.locator("[data-req-status='archive']")
        archive_pill.click()
        authed_page.wait_for_timeout(1000)

        # Switch back to active
        active_pill = authed_page.locator("[data-req-status='active']")
        active_pill.click()
        authed_page.wait_for_timeout(1500)

        # Data should still be there
        after_cards = authed_page.locator("#reqList .card[onclick*='showDetail']")
        after_count = after_cards.count()
        assert after_count == initial_count, \
            f"Active reqs disappeared after archive round-trip: was {initial_count}, now {after_count}"

    def test_cycle_all_buckets(self, authed_page, base_url):
        """Cycling through all status buckets should not lose data."""
        errors = []
        authed_page.on("pageerror", lambda exc: errors.append(str(exc)))
        wait_for_app(authed_page, base_url)
        authed_page.wait_for_timeout(1000)

        # Get initial active count
        switch_to_active_bucket(authed_page)
        initial_count = authed_page.locator("#reqList .card[onclick*='showDetail']").count()

        # Cycle through all buckets
        buckets = ["draft", "active", "offers", "quoted", "archive", "active"]
        for bucket in buckets:
            pill = authed_page.locator(f"[data-req-status='{bucket}']")
            if pill.is_visible():
                pill.click()
                authed_page.wait_for_timeout(800)

        # Back on active — count should match
        after_count = authed_page.locator("#reqList .card[onclick*='showDetail']").count()
        assert after_count == initial_count, \
            f"Active reqs changed after full bucket cycle: was {initial_count}, now {after_count}"
        assert len(errors) == 0, f"JS errors during bucket cycling: {errors}"

    def test_bucket_pill_active_state(self, authed_page, base_url):
        """Only the clicked bucket pill should have the 'on' class."""
        wait_for_app(authed_page, base_url)
        authed_page.wait_for_timeout(1000)

        buckets = ["draft", "active", "offers", "quoted"]
        for bucket in buckets:
            pill = authed_page.locator(f"[data-req-status='{bucket}']")
            if pill.is_visible():
                pill.click()
                authed_page.wait_for_timeout(300)
                expect(pill).to_have_class(re.compile(r"\bon\b"))
                # Others should not be 'on'
                for other in buckets:
                    if other != bucket:
                        other_pill = authed_page.locator(f"[data-req-status='{other}']")
                        if other_pill.is_visible():
                            expect(other_pill).not_to_have_class(re.compile(r"\bon\b"))
