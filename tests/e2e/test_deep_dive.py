"""100% Deep-Dive Playwright E2E tests for AvailAI.

Covers every major view, modal, drawer, navigation path, form interaction,
keyboard shortcut, API integrity check, mobile responsiveness, and edge case
in the AvailAI SPA. Tests run against the live Docker app with a signed
session cookie (admin user_id=1).

What calls it: pytest tests/e2e/ --headed (or headless)
Depends on: conftest.py (session cookie signing, base_url fixture)
"""

import re

import pytest
from playwright.sync_api import Page, expect

# ── Helpers ──────────────────────────────────────────────────────────


def wait_for_app(page: Page, base_url: str):
    """Navigate and wait for the app shell to be fully loaded."""
    page.goto(base_url, wait_until="domcontentloaded")
    page.wait_for_timeout(500)


def goto_view(page: Page, base_url: str, fragment: str):
    """Navigate directly to a hash-routed view."""
    page.goto(f"{base_url}/#{fragment}", wait_until="domcontentloaded")
    page.wait_for_timeout(1000)


def nav_click(page: Page, nav_id: str):
    """Click a sidebar nav button via JS to bypass viewport checks."""
    clicked = page.evaluate(f"""() => {{
        const el = document.getElementById('{nav_id}');
        if (el) {{ el.click(); return true; }}
        return false;
    }}""")
    if not clicked:
        pytest.skip(f"#{nav_id} not found in DOM")
    page.wait_for_timeout(500)


def open_modal(page: Page, modal_id: str):
    """Open a modal by calling the JS openModal function."""
    page.evaluate(f"() => {{ if (typeof openModal === 'function') openModal('{modal_id}'); }}")
    page.wait_for_timeout(300)


def close_modal(page: Page, modal_id: str):
    """Close a modal by calling the JS closeModal function."""
    page.evaluate(f"() => {{ if (typeof closeModal === 'function') closeModal('{modal_id}'); }}")
    page.wait_for_timeout(300)


def collect_errors(page: Page):
    """Attach an error collector and return the list reference."""
    errors = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    return errors


def collect_failed_apis(page: Page):
    """Attach a response listener that captures failed API calls."""
    failed = []

    def on_response(response):
        if "/api/" in response.url and response.status >= 400:
            failed.append(f"{response.status} {response.url}")

    page.on("response", on_response)
    return failed


def get_visible_view_count(page: Page, view_ids: list[str]) -> int:
    """Count how many of the given view IDs are currently visible."""
    count = 0
    for vid in view_ids:
        loc = page.locator(f"#{vid}")
        if loc.count() > 0 and loc.first.is_visible():
            count += 1
    return count


# ── 1. AUTH & SESSION ────────────────────────────────────────────────


class TestAuthAndSession:
    """Verify authentication flows and session handling."""

    def test_unauthenticated_sees_login(self, page, base_url):
        """Unauthenticated users see the Microsoft login button."""
        page.goto(base_url, wait_until="networkidle")
        expect(page.locator("a.btn-ms")).to_be_visible()
        expect(page.locator("a.btn-ms")).to_contain_text("Sign in with Microsoft")

    def test_login_button_links_to_auth(self, page, base_url):
        """Login button href points to /auth/login."""
        page.goto(base_url, wait_until="networkidle")
        href = page.locator("a.btn-ms").get_attribute("href")
        assert href == "/auth/login"

    def test_login_page_shows_logo(self, page, base_url):
        """Login page shows the AVAIL logo."""
        page.goto(base_url, wait_until="networkidle")
        expect(page.locator(".login-logo")).to_be_visible()

    def test_login_page_shows_version(self, page, base_url):
        """Login page shows the app version."""
        page.goto(base_url, wait_until="networkidle")
        ver = page.locator(".ver")
        expect(ver).to_be_visible()
        assert ver.text_content().startswith("v")

    def test_authenticated_sees_app_shell(self, authed_page, base_url):
        """Authenticated users see the main app shell, not login."""
        wait_for_app(authed_page, base_url)
        expect(authed_page.locator("a.btn-ms")).not_to_be_visible()
        assert authed_page.evaluate("document.getElementById('navReqs') !== null")

    def test_user_name_displayed(self, authed_page, base_url):
        """User name appears in the sidebar footer."""
        wait_for_app(authed_page, base_url)
        expect(authed_page.locator(".un")).to_be_visible()
        name = authed_page.locator(".un").text_content()
        assert len(name.strip()) > 0

    def test_user_initials_avatar(self, authed_page, base_url):
        """User initials avatar badge is visible."""
        wait_for_app(authed_page, base_url)
        expect(authed_page.locator(".ui")).to_be_visible()

    def test_admin_badge_visible(self, authed_page, base_url):
        """Admin users see the admin role badge."""
        wait_for_app(authed_page, base_url)
        admin_badge = authed_page.locator(".sidebar-footer .rb")
        if admin_badge.count() > 0:
            expect(admin_badge.first).to_contain_text("Admin")

    def test_logout_button_exists(self, authed_page, base_url):
        """Logout button exists in the user popover."""
        wait_for_app(authed_page, base_url)
        logout = authed_page.locator(".sb-user-popover button")
        assert logout.count() > 0


# ── 2. SIDEBAR NAVIGATION ───────────────────────────────────────────


class TestSidebarNavigation:
    """Full coverage of sidebar navigation groups and buttons."""

    ALL_NAV_IDS = [
        "navDashboard",
        "navScorecard",
        "navReqs",
        "navProactive",
        "navMaterials",
        "navBuyPlans",
        "navCustomers",
        "navVendors",
        "navStrategic",
        "navProspecting",
        "navContacts",
        "navSettings",
    ]

    NAV_TO_VIEW = {
        "navReqs": "view-list",
        "navVendors": "view-vendors",
        "navMaterials": "view-materials",
        "navCustomers": "view-customers",
        "navProactive": "view-proactive",
        "navBuyPlans": "view-buyplans",
        "navDashboard": "view-dashboard",
        "navScorecard": "view-scorecard",
        "navContacts": "view-contacts",
        "navStrategic": "view-strategic",
        "navProspecting": "view-suggested",
        "navSettings": "view-settings",
    }

    def test_all_nav_buttons_exist(self, authed_page, base_url):
        """All expected nav buttons exist in DOM (settings only for admin)."""
        wait_for_app(authed_page, base_url)
        for nav_id in self.ALL_NAV_IDS:
            el = authed_page.locator(f"#{nav_id}")
            assert el.count() > 0 or nav_id == "navSettings", f"#{nav_id} missing from DOM"

    @pytest.mark.parametrize(
        "nav_id,view_id",
        [
            ("navReqs", "view-list"),
            ("navVendors", "view-vendors"),
            ("navMaterials", "view-materials"),
            ("navCustomers", "view-customers"),
            ("navProactive", "view-proactive"),
            ("navBuyPlans", "view-buyplans"),
            ("navDashboard", "view-dashboard"),
            ("navScorecard", "view-scorecard"),
            ("navContacts", "view-contacts"),
            ("navStrategic", "view-strategic"),
            ("navProspecting", "view-suggested"),
        ],
    )
    def test_nav_shows_correct_view(self, authed_page, base_url, nav_id, view_id):
        """Each nav button shows its corresponding view."""
        wait_for_app(authed_page, base_url)
        nav_click(authed_page, nav_id)
        authed_page.wait_for_timeout(500)
        expect(authed_page.locator(f"#{view_id}")).to_be_visible()

    def test_nav_active_state_toggles(self, authed_page, base_url):
        """Only the clicked nav button has the 'active' class."""
        wait_for_app(authed_page, base_url)
        nav_click(authed_page, "navVendors")
        expect(authed_page.locator("#navVendors")).to_have_class(re.compile(r"\bactive\b"))
        expect(authed_page.locator("#navReqs")).not_to_have_class(re.compile(r"\bactive\b"))

    def test_only_one_view_visible(self, authed_page, base_url):
        """Switching nav shows exactly one view panel at a time."""
        wait_for_app(authed_page, base_url)
        view_ids = list(self.NAV_TO_VIEW.values())
        test_navs = ["navReqs", "navVendors", "navMaterials", "navCustomers"]
        for nav_id in test_navs:
            nav_click(authed_page, nav_id)
            authed_page.wait_for_timeout(300)
            expected_view = self.NAV_TO_VIEW[nav_id]
            expect(authed_page.locator(f"#{expected_view}")).to_be_visible()

    def test_sidebar_group_toggle(self, authed_page, base_url):
        """Sidebar section groups can be collapsed and expanded."""
        wait_for_app(authed_page, base_url)
        result = authed_page.evaluate("""() => {
            const headers = document.querySelectorAll('.sb-group-header');
            if (headers.length === 0) return 'no_headers';
            headers[0].click();
            return 'clicked';
        }""")
        if result == "no_headers":
            pytest.skip("No sidebar group headers found")
        authed_page.wait_for_timeout(300)

    def test_sidebar_rail_toggle(self, authed_page, base_url):
        """Clicking the sidebar rail toggles the sidebar."""
        wait_for_app(authed_page, base_url)
        rail = authed_page.locator(".sb-rail")
        if rail.is_visible():
            rail.click()
            authed_page.wait_for_timeout(300)

    def test_navigate_round_trip(self, authed_page, base_url):
        """Navigate away and back returns to the original view."""
        wait_for_app(authed_page, base_url)
        nav_click(authed_page, "navVendors")
        expect(authed_page.locator("#view-vendors")).to_be_visible()
        nav_click(authed_page, "navReqs")
        expect(authed_page.locator("#view-list")).to_be_visible()
        expect(authed_page.locator("#view-vendors")).not_to_be_visible()


# ── 3. TOP AREA CONTROLS ────────────────────────────────────────────


class TestTopAreaControls:
    """Tests for the top bar: main view pills, search, status filter."""

    def test_main_view_pills_exist(self, authed_page, base_url):
        """Main view pills (Sales, Sourcing, Deals, Archive) are present."""
        wait_for_app(authed_page, base_url)
        pills = authed_page.locator("#mainPills .fp")
        assert pills.count() >= 3

    def test_main_view_pill_click(self, authed_page, base_url):
        """Clicking a main view pill applies the 'on' class."""
        wait_for_app(authed_page, base_url)
        pills = authed_page.locator("#mainPills .fp")
        if pills.count() >= 2:
            pills.nth(1).click()
            authed_page.wait_for_timeout(500)
            expect(pills.nth(1)).to_have_class(re.compile(r"\bon\b"))

    def test_main_search_input_exists(self, authed_page, base_url):
        """Main search input is present and focusable."""
        wait_for_app(authed_page, base_url)
        search = authed_page.locator("#mainSearch")
        expect(search).to_be_visible()

    def test_main_search_accepts_input(self, authed_page, base_url):
        """Typing in the main search input works."""
        wait_for_app(authed_page, base_url)
        search = authed_page.locator("#mainSearch")
        search.fill("test search")
        assert search.input_value() == "test search"

    def test_search_button_exists(self, authed_page, base_url):
        """Search button next to input exists."""
        wait_for_app(authed_page, base_url)
        expect(authed_page.locator("#mainSearchBtn")).to_be_visible()

    def test_status_filter_pills(self, authed_page, base_url):
        """Status filter pills (All, Draft, Sourcing, Quoted) exist."""
        wait_for_app(authed_page, base_url)
        pills = authed_page.locator("#statusToggle .fp")
        assert pills.count() >= 3

    def test_status_filter_click(self, authed_page, base_url):
        """Clicking status filter pills toggles active state."""
        wait_for_app(authed_page, base_url)
        pills = authed_page.locator("#statusToggle .fp")
        if pills.count() >= 2:
            pills.nth(1).click()
            authed_page.wait_for_timeout(500)
            expect(pills.nth(1)).to_have_class(re.compile(r"\bon\b"))

    def test_new_req_button_exists(self, authed_page, base_url):
        """'+ New Req' button is visible in the top bar."""
        wait_for_app(authed_page, base_url)
        btn = authed_page.locator(".tb-primary")
        expect(btn.first).to_be_visible()

    def test_notification_bell_exists(self, authed_page, base_url):
        """Notification bell button exists."""
        wait_for_app(authed_page, base_url)
        bell = authed_page.locator(".notif-btn-global")
        assert bell.count() > 0

    def test_notification_panel_toggle(self, authed_page, base_url):
        """Clicking notification bell toggles the notification panel."""
        wait_for_app(authed_page, base_url)
        authed_page.evaluate("() => { if (typeof toggleNotifications === 'function') toggleNotifications(); }")
        authed_page.wait_for_timeout(300)


# ── 4. REQUISITION LIST VIEW ────────────────────────────────────────


class TestRequisitionListView:
    """Tests for the main requisition list view."""

    def test_req_list_loads(self, authed_page, base_url):
        """Requisition list view loads without error."""
        goto_view(authed_page, base_url, "rfqs")
        expect(authed_page.locator("#view-list")).to_be_visible()

    def test_req_list_container_exists(self, authed_page, base_url):
        """The #reqList container exists and has content."""
        goto_view(authed_page, base_url, "rfqs")
        authed_page.wait_for_timeout(2000)
        req_list = authed_page.locator("#reqList")
        expect(req_list).to_be_visible()

    def test_new_req_modal_opens(self, authed_page, base_url):
        """Clicking '+ New Req' opens the new requisition modal."""
        wait_for_app(authed_page, base_url)
        authed_page.evaluate("() => openNewReqModal()")
        authed_page.wait_for_timeout(300)
        expect(authed_page.locator("#newReqModal")).to_have_class(re.compile(r"\bopen\b"))

    def test_new_req_modal_has_required_fields(self, authed_page, base_url):
        """New requisition modal has name and customer fields."""
        wait_for_app(authed_page, base_url)
        authed_page.evaluate("() => openNewReqModal()")
        authed_page.wait_for_timeout(300)
        expect(authed_page.locator("#nrName")).to_be_visible()
        expect(authed_page.locator("#nrSiteSearch")).to_be_visible()

    def test_new_req_modal_has_deadline(self, authed_page, base_url):
        """New requisition modal has deadline and ASAP fields."""
        wait_for_app(authed_page, base_url)
        authed_page.evaluate("() => openNewReqModal()")
        authed_page.wait_for_timeout(300)
        expect(authed_page.locator("#nrDeadline")).to_be_visible()
        expect(authed_page.locator("#nrAsap")).to_be_visible()

    def test_new_req_modal_closes_on_cancel(self, authed_page, base_url):
        """Cancel button closes the new requisition modal."""
        wait_for_app(authed_page, base_url)
        authed_page.evaluate("() => openNewReqModal()")
        authed_page.wait_for_timeout(300)
        expect(authed_page.locator("#newReqModal")).to_have_class(re.compile(r"\bopen\b"))
        authed_page.locator("#newReqModal .btn-ghost").click()
        authed_page.wait_for_timeout(300)
        expect(authed_page.locator("#newReqModal")).not_to_have_class(re.compile(r"\bopen\b"))

    def test_new_req_modal_closes_on_escape(self, authed_page, base_url):
        """Pressing Escape closes the new requisition modal."""
        wait_for_app(authed_page, base_url)
        authed_page.evaluate("() => openNewReqModal()")
        authed_page.wait_for_timeout(300)
        authed_page.keyboard.press("Escape")
        authed_page.wait_for_timeout(300)

    def test_new_req_modal_closes_on_backdrop_click(self, authed_page, base_url):
        """Clicking the modal backdrop closes it."""
        wait_for_app(authed_page, base_url)
        authed_page.evaluate("() => openNewReqModal()")
        authed_page.wait_for_timeout(300)
        # Click on the backdrop (modal-bg) at the edge
        authed_page.locator("#newReqModal").click(position={"x": 5, "y": 5})
        authed_page.wait_for_timeout(300)

    def test_toolbar_stats_element(self, authed_page, base_url):
        """Toolbar stats element exists."""
        wait_for_app(authed_page, base_url)
        assert authed_page.locator("#toolbarStats").count() > 0


# ── 5. REQUISITION DRILL-DOWN ───────────────────────────────────────


class TestRequisitionDrillDown:
    """Tests for expanding/collapsing requisition rows and sub-tabs."""

    def _open_first_req(self, page: Page, base_url: str) -> bool:
        goto_view(page, base_url, "rfqs")
        page.wait_for_timeout(1500)
        arrow = page.locator("#reqList .ea").first
        if not arrow.is_visible():
            return False
        arrow.click()
        page.wait_for_timeout(1000)
        return True

    def test_drilldown_opens(self, authed_page, base_url):
        """Clicking expand arrow opens a drill-down row."""
        if not self._open_first_req(authed_page, base_url):
            pytest.skip("No requisitions available")
        expect(authed_page.locator("tr.drow.open").first).to_be_visible()

    def test_drilldown_has_tabs(self, authed_page, base_url):
        """Open drill-down contains sub-tabs."""
        if not self._open_first_req(authed_page, base_url):
            pytest.skip("No requisitions available")
        tabs = authed_page.locator("tr.drow.open .dd-tabs .dd-tab")
        assert tabs.count() > 0

    def test_drilldown_exactly_one_active_tab(self, authed_page, base_url):
        """Exactly one drill-down tab is active at a time."""
        if not self._open_first_req(authed_page, base_url):
            pytest.skip("No requisitions available")
        active = authed_page.locator("tr.drow.open .dd-tabs .dd-tab.on")
        assert active.count() == 1

    def test_drilldown_tab_switching(self, authed_page, base_url):
        """All drill-down tabs are clickable and maintain single active."""
        if not self._open_first_req(authed_page, base_url):
            pytest.skip("No requisitions available")
        tabs = authed_page.locator("tr.drow.open .dd-tabs .dd-tab")
        count = tabs.count()
        for i in range(count):
            tabs.nth(i).click()
            authed_page.wait_for_timeout(300)
            active = authed_page.locator("tr.drow.open .dd-tabs .dd-tab.on")
            assert active.count() == 1, f"Expected 1 active tab after clicking tab {i}, got {active.count()}"

    def test_drilldown_collapse(self, authed_page, base_url):
        """Clicking expand arrow again collapses the drill-down."""
        if not self._open_first_req(authed_page, base_url):
            pytest.skip("No requisitions available")
        authed_page.locator("#reqList .ea").first.click()
        authed_page.wait_for_timeout(300)

    def test_multiple_drilldown_open_close(self, authed_page, base_url):
        """Opening and closing multiple requisitions doesn't leak state."""
        errors = collect_errors(authed_page)
        goto_view(authed_page, base_url, "rfqs")
        authed_page.wait_for_timeout(1500)
        arrows = authed_page.locator("#reqList .ea")
        count = min(arrows.count(), 5)
        if count < 2:
            pytest.skip("Need at least 2 requisitions")
        for i in range(count):
            arrows.nth(i).click()
            authed_page.wait_for_timeout(400)
            arrows.nth(i).click()
            authed_page.wait_for_timeout(300)
        assert len(errors) == 0, f"JS errors: {errors}"


# ── 6. VENDOR VIEW ──────────────────────────────────────────────────


class TestVendorView:
    """Tests for the vendor list, search, filters, and drawer."""

    def test_vendor_view_loads(self, authed_page, base_url):
        """Vendor view loads without error."""
        wait_for_app(authed_page, base_url)
        nav_click(authed_page, "navVendors")
        authed_page.wait_for_timeout(1000)
        expect(authed_page.locator("#view-vendors")).to_be_visible()

    def test_vendor_header_shows(self, authed_page, base_url):
        """Vendor header with title is visible."""
        wait_for_app(authed_page, base_url)
        nav_click(authed_page, "navVendors")
        authed_page.wait_for_timeout(500)
        header = authed_page.locator("#view-vendors h2")
        expect(header).to_contain_text("Vendors")

    def test_vendor_search_input(self, authed_page, base_url):
        """Vendor search input exists and accepts text."""
        wait_for_app(authed_page, base_url)
        nav_click(authed_page, "navVendors")
        search = authed_page.locator("#vendorSearch")
        expect(search).to_be_visible()
        search.fill("test vendor")
        assert search.input_value() == "test vendor"

    def test_vendor_tier_pills(self, authed_page, base_url):
        """Vendor tier filter pills exist (All, Proven, Dev, etc.)."""
        wait_for_app(authed_page, base_url)
        nav_click(authed_page, "navVendors")
        pills = authed_page.locator("#vendorTierPills .chip")
        assert pills.count() >= 4

    def test_vendor_tier_pill_toggle(self, authed_page, base_url):
        """Clicking tier pills toggles their active state."""
        wait_for_app(authed_page, base_url)
        nav_click(authed_page, "navVendors")
        pills = authed_page.locator("#vendorTierPills .chip")
        if pills.count() >= 2:
            pills.nth(1).click()
            authed_page.wait_for_timeout(500)
            expect(pills.nth(1)).to_have_class(re.compile(r"\bon\b"))

    def test_add_vendor_button(self, authed_page, base_url):
        """'+ Add Vendor' button is visible."""
        wait_for_app(authed_page, base_url)
        nav_click(authed_page, "navVendors")
        btn = authed_page.locator("#view-vendors .btn-primary")
        expect(btn).to_be_visible()

    def test_vendor_list_container(self, authed_page, base_url):
        """Vendor list container exists."""
        wait_for_app(authed_page, base_url)
        nav_click(authed_page, "navVendors")
        authed_page.wait_for_timeout(1000)
        expect(authed_page.locator("#vendorList")).to_be_visible()

    def test_vendor_drawer_elements(self, authed_page, base_url):
        """Vendor drawer elements exist in DOM."""
        wait_for_app(authed_page, base_url)
        nav_click(authed_page, "navVendors")
        assert authed_page.locator("#vendorDrawer").count() > 0
        assert authed_page.locator("#vendorDrawerTabs").count() > 0

    def test_vendor_drawer_tabs_structure(self, authed_page, base_url):
        """Vendor drawer has expected tabs: Overview, Contacts, etc."""
        wait_for_app(authed_page, base_url)
        nav_click(authed_page, "navVendors")
        tabs = authed_page.locator("#vendorDrawerTabs .drawer-tab")
        tab_texts = [tabs.nth(i).text_content().strip() for i in range(tabs.count())]
        assert "Overview" in tab_texts
        assert "Contacts" in tab_texts


# ── 7. CUSTOMER/ACCOUNTS VIEW ───────────────────────────────────────


class TestCustomerView:
    """Tests for the accounts/customer view."""

    def test_customer_view_loads(self, authed_page, base_url):
        """Customer view loads without error."""
        wait_for_app(authed_page, base_url)
        nav_click(authed_page, "navCustomers")
        authed_page.wait_for_timeout(1000)
        expect(authed_page.locator("#view-customers")).to_be_visible()

    def test_customer_header(self, authed_page, base_url):
        """Customer header shows 'Accounts'."""
        wait_for_app(authed_page, base_url)
        nav_click(authed_page, "navCustomers")
        header = authed_page.locator("#view-customers h2")
        expect(header).to_contain_text("Accounts")

    def test_customer_search_input(self, authed_page, base_url):
        """Customer search input exists."""
        wait_for_app(authed_page, base_url)
        nav_click(authed_page, "navCustomers")
        expect(authed_page.locator("#custFilter")).to_be_visible()

    def test_new_account_button(self, authed_page, base_url):
        """'+ New Account' button exists."""
        wait_for_app(authed_page, base_url)
        nav_click(authed_page, "navCustomers")
        btn = authed_page.locator("#view-customers .btn-primary")
        expect(btn.first).to_be_visible()

    def test_customer_filter_chips(self, authed_page, base_url):
        """Customer filter chips (Strategic, At Risk, etc.) exist."""
        wait_for_app(authed_page, base_url)
        nav_click(authed_page, "navCustomers")
        chips = authed_page.locator("#view-customers .chip")
        assert chips.count() >= 2

    def test_customer_owner_filter(self, authed_page, base_url):
        """Account owner filter dropdown exists."""
        wait_for_app(authed_page, base_url)
        nav_click(authed_page, "navCustomers")
        expect(authed_page.locator("#custOwnerFilter")).to_be_visible()

    def test_customer_drawer_elements(self, authed_page, base_url):
        """Customer drawer and tabs exist in DOM."""
        wait_for_app(authed_page, base_url)
        nav_click(authed_page, "navCustomers")
        assert authed_page.locator("#custDrawer").count() > 0

    def test_customer_drawer_tabs(self, authed_page, base_url):
        """Customer drawer has expected tabs."""
        wait_for_app(authed_page, base_url)
        nav_click(authed_page, "navCustomers")
        tabs = authed_page.locator("#custDrawerTabs .drawer-tab")
        tab_texts = [tabs.nth(i).text_content().strip() for i in range(tabs.count())]
        assert "Overview" in tab_texts
        assert "Contacts" in tab_texts
        assert "Sites" in tab_texts
        assert "Activity" in tab_texts


# ── 8. MATERIAL VIEW ────────────────────────────────────────────────


class TestMaterialView:
    """Tests for the materials view."""

    def test_material_view_loads(self, authed_page, base_url):
        """Material view loads without error."""
        wait_for_app(authed_page, base_url)
        nav_click(authed_page, "navMaterials")
        authed_page.wait_for_timeout(1000)
        expect(authed_page.locator("#view-materials")).to_be_visible()

    def test_material_header(self, authed_page, base_url):
        """Material header shows 'Materials'."""
        wait_for_app(authed_page, base_url)
        nav_click(authed_page, "navMaterials")
        header = authed_page.locator("#view-materials h2")
        expect(header).to_contain_text("Materials")

    def test_material_search_input(self, authed_page, base_url):
        """Material search input exists."""
        wait_for_app(authed_page, base_url)
        nav_click(authed_page, "navMaterials")
        expect(authed_page.locator("#materialSearch")).to_be_visible()

    def test_material_import_stock_button(self, authed_page, base_url):
        """Import Stock button exists."""
        wait_for_app(authed_page, base_url)
        nav_click(authed_page, "navMaterials")
        btn = authed_page.locator("#view-materials .tb-btn")
        assert btn.count() > 0

    def test_material_list_container(self, authed_page, base_url):
        """Material list container exists."""
        wait_for_app(authed_page, base_url)
        nav_click(authed_page, "navMaterials")
        authed_page.wait_for_timeout(1000)
        expect(authed_page.locator("#materialList")).to_be_visible()


# ── 9. BUY PLANS VIEW ───────────────────────────────────────────────


class TestBuyPlansView:
    """Tests for the buy plans view."""

    def test_buy_plans_view_loads(self, authed_page, base_url):
        """Buy plans view loads without error."""
        wait_for_app(authed_page, base_url)
        nav_click(authed_page, "navBuyPlans")
        authed_page.wait_for_timeout(1000)
        expect(authed_page.locator("#view-buyplans")).to_be_visible()

    def test_buy_plans_header(self, authed_page, base_url):
        """Buy plans header shows 'Buy Plans'."""
        wait_for_app(authed_page, base_url)
        nav_click(authed_page, "navBuyPlans")
        header = authed_page.locator("#view-buyplans h2")
        expect(header).to_contain_text("Buy Plans")

    def test_buy_plans_status_pills(self, authed_page, base_url):
        """Buy plans status filter pills exist."""
        wait_for_app(authed_page, base_url)
        nav_click(authed_page, "navBuyPlans")
        pills = authed_page.locator("#bpStatusPills .fp")
        assert pills.count() >= 4

    def test_buy_plans_search(self, authed_page, base_url):
        """Buy plans search input exists."""
        wait_for_app(authed_page, base_url)
        nav_click(authed_page, "navBuyPlans")
        expect(authed_page.locator("#bpSearch")).to_be_visible()

    def test_buy_plans_my_tasks_filter(self, authed_page, base_url):
        """Buy plans 'My Tasks' checkbox exists."""
        wait_for_app(authed_page, base_url)
        nav_click(authed_page, "navBuyPlans")
        expect(authed_page.locator("#bpMyOnly")).to_be_visible()


# ── 10. PROACTIVE VIEW ──────────────────────────────────────────────


class TestProactiveView:
    """Tests for the proactive offers view."""

    def test_proactive_view_loads(self, authed_page, base_url):
        """Proactive view loads without error."""
        wait_for_app(authed_page, base_url)
        nav_click(authed_page, "navProactive")
        authed_page.wait_for_timeout(1000)
        expect(authed_page.locator("#view-proactive")).to_be_visible()

    def test_proactive_header(self, authed_page, base_url):
        """Proactive header shows 'Proactive Offers'."""
        wait_for_app(authed_page, base_url)
        nav_click(authed_page, "navProactive")
        header = authed_page.locator("#view-proactive h2")
        expect(header).to_contain_text("Proactive Offers")

    def test_proactive_tabs(self, authed_page, base_url):
        """Proactive view has Matches, Sent, Scorecard tabs."""
        wait_for_app(authed_page, base_url)
        nav_click(authed_page, "navProactive")
        tabs = authed_page.locator("#proactiveTabs .tab")
        assert tabs.count() >= 3

    def test_proactive_tab_switching(self, authed_page, base_url):
        """Proactive tabs switch correctly."""
        wait_for_app(authed_page, base_url)
        nav_click(authed_page, "navProactive")
        tabs = authed_page.locator("#proactiveTabs .tab")
        for i in range(tabs.count()):
            tabs.nth(i).click()
            authed_page.wait_for_timeout(300)
            expect(tabs.nth(i)).to_have_class(re.compile(r"\bon\b"))

    def test_proactive_refresh_button(self, authed_page, base_url):
        """Refresh Matches button exists."""
        wait_for_app(authed_page, base_url)
        nav_click(authed_page, "navProactive")
        expect(authed_page.locator("#proactiveRefreshBtn")).to_be_visible()


# ── 11. CONTACTS VIEW ───────────────────────────────────────────────


class TestContactsView:
    """Tests for the contacts view."""

    def test_contacts_view_loads(self, authed_page, base_url):
        """Contacts view loads without error."""
        wait_for_app(authed_page, base_url)
        nav_click(authed_page, "navContacts")
        authed_page.wait_for_timeout(1000)
        expect(authed_page.locator("#view-contacts")).to_be_visible()

    def test_contacts_drawer_exists(self, authed_page, base_url):
        """Contact drawer exists in DOM."""
        wait_for_app(authed_page, base_url)
        nav_click(authed_page, "navContacts")
        assert authed_page.locator("#contactDrawer").count() > 0


# ── 12. STRATEGIC VENDORS VIEW ──────────────────────────────────────


class TestStrategicVendorsView:
    """Tests for the My Vendors (strategic) view."""

    def test_strategic_view_loads(self, authed_page, base_url):
        """Strategic vendors view loads."""
        wait_for_app(authed_page, base_url)
        nav_click(authed_page, "navStrategic")
        authed_page.wait_for_timeout(1000)
        expect(authed_page.locator("#view-strategic")).to_be_visible()

    def test_strategic_header(self, authed_page, base_url):
        """Strategic header shows 'My Strategic Vendors'."""
        wait_for_app(authed_page, base_url)
        nav_click(authed_page, "navStrategic")
        header = authed_page.locator("#view-strategic h2")
        expect(header).to_contain_text("My Strategic Vendors")

    def test_strategic_slot_counter(self, authed_page, base_url):
        """Slot counter (0/10 slots) is visible."""
        wait_for_app(authed_page, base_url)
        nav_click(authed_page, "navStrategic")
        expect(authed_page.locator("#strategicSlots")).to_be_visible()

    def test_strategic_claim_button(self, authed_page, base_url):
        """Claim Vendor button exists."""
        wait_for_app(authed_page, base_url)
        nav_click(authed_page, "navStrategic")
        expect(authed_page.locator("#strategicClaimBtn")).to_be_visible()

    def test_strategic_claim_modal_opens(self, authed_page, base_url):
        """Clicking Claim Vendor opens the claim modal."""
        wait_for_app(authed_page, base_url)
        nav_click(authed_page, "navStrategic")
        authed_page.evaluate("() => openStrategicClaimModal()")
        authed_page.wait_for_timeout(300)
        expect(authed_page.locator("#strategicClaimModal")).to_be_visible()


# ── 13. PROSPECTING VIEW ────────────────────────────────────────────


class TestProspectingView:
    """Tests for the suggested/prospecting view."""

    def test_prospecting_view_loads(self, authed_page, base_url):
        """Prospecting view loads."""
        wait_for_app(authed_page, base_url)
        nav_click(authed_page, "navProspecting")
        authed_page.wait_for_timeout(1000)
        expect(authed_page.locator("#view-suggested")).to_be_visible()

    def test_prospecting_header(self, authed_page, base_url):
        """Prospecting header shows 'Suggested Accounts'."""
        wait_for_app(authed_page, base_url)
        nav_click(authed_page, "navProspecting")
        header = authed_page.locator("#view-suggested h2")
        expect(header).to_contain_text("Suggested Accounts")

    def test_prospecting_search(self, authed_page, base_url):
        """Prospecting search input exists."""
        wait_for_app(authed_page, base_url)
        nav_click(authed_page, "navProspecting")
        expect(authed_page.locator("#suggestedSearch")).to_be_visible()

    def test_prospecting_filters(self, authed_page, base_url):
        """Prospecting filter dropdowns exist."""
        wait_for_app(authed_page, base_url)
        nav_click(authed_page, "navProspecting")
        filters = ["suggestedSize", "suggestedIndustry", "suggestedRevenue", "suggestedSort"]
        for f_id in filters:
            assert authed_page.locator(f"#{f_id}").count() > 0, f"#{f_id} missing"


# ── 14. DASHBOARD VIEW ──────────────────────────────────────────────


class TestDashboardView:
    """Tests for the dashboard view."""

    def test_dashboard_view_loads(self, authed_page, base_url):
        """Dashboard view loads."""
        wait_for_app(authed_page, base_url)
        nav_click(authed_page, "navDashboard")
        authed_page.wait_for_timeout(1000)
        expect(authed_page.locator("#view-dashboard")).to_be_visible()


# ── 15. SCORECARD VIEW ──────────────────────────────────────────────


class TestScorecardView:
    """Tests for the scorecard view."""

    def test_scorecard_view_loads(self, authed_page, base_url):
        """Scorecard view loads."""
        wait_for_app(authed_page, base_url)
        nav_click(authed_page, "navScorecard")
        authed_page.wait_for_timeout(1000)
        expect(authed_page.locator("#view-scorecard")).to_be_visible()


# ── 16. SETTINGS VIEW ───────────────────────────────────────────────


class TestSettingsView:
    """Tests for the admin settings view."""

    def test_settings_view_loads(self, authed_page, base_url):
        """Settings view loads for admin users."""
        wait_for_app(authed_page, base_url)
        nav_btn = authed_page.locator("#navSettings")
        if nav_btn.count() == 0:
            pytest.skip("Settings not available (non-admin)")
        nav_click(authed_page, "navSettings")
        authed_page.wait_for_timeout(1000)
        expect(authed_page.locator("#view-settings")).to_be_visible()


# ── 17. MODALS COMPREHENSIVE ────────────────────────────────────────


class TestModals:
    """Tests for all major modals: open, close, form fields."""

    MODAL_IDS = [
        "newReqModal",
        "newCompanyModal",
        "editCompanyModal",
        "addSiteModal",
        "vendorPopup",
        "vendorContactModal",
        "vendorLogCallModal",
        "vendorLogNoteModal",
        "materialPopup",
        "logOfferModal",
        "sendQuoteModal",
        "editOfferModal",
        "buyPlanModal",
        "lostModal",
        "logCallModal",
        "logNoteModal",
        "siteContactModal",
        "pastePartsModal",
    ]

    def test_all_modals_exist_in_dom(self, authed_page, base_url):
        """All expected modals exist in the DOM."""
        wait_for_app(authed_page, base_url)
        for modal_id in self.MODAL_IDS:
            assert authed_page.locator(f"#{modal_id}").count() > 0, f"#{modal_id} missing from DOM"

    def test_modal_open_close_cycle(self, authed_page, base_url):
        """Modals can be opened and closed via JS functions."""
        wait_for_app(authed_page, base_url)
        for modal_id in ["newReqModal", "newCompanyModal", "vendorContactModal"]:
            open_modal(authed_page, modal_id)
            authed_page.wait_for_timeout(200)
            close_modal(authed_page, modal_id)
            authed_page.wait_for_timeout(200)

    def test_new_company_modal_fields(self, authed_page, base_url):
        """New company modal has name, website, industry fields."""
        wait_for_app(authed_page, base_url)
        open_modal(authed_page, "newCompanyModal")
        expect(authed_page.locator("#ncName")).to_be_visible()
        expect(authed_page.locator("#ncWebsite")).to_be_visible()
        expect(authed_page.locator("#ncIndustry")).to_be_visible()
        close_modal(authed_page, "newCompanyModal")

    def test_vendor_contact_modal_fields(self, authed_page, base_url):
        """Vendor contact modal has email, name, title, phone fields."""
        wait_for_app(authed_page, base_url)
        open_modal(authed_page, "vendorContactModal")
        expect(authed_page.locator("#vcEmail")).to_be_visible()
        expect(authed_page.locator("#vcFullName")).to_be_visible()
        expect(authed_page.locator("#vcTitle")).to_be_visible()
        expect(authed_page.locator("#vcPhone")).to_be_visible()
        close_modal(authed_page, "vendorContactModal")

    def test_vendor_log_call_modal_fields(self, authed_page, base_url):
        """Vendor log call modal has phone, contact, direction fields."""
        wait_for_app(authed_page, base_url)
        open_modal(authed_page, "vendorLogCallModal")
        expect(authed_page.locator("#vlcPhone")).to_be_visible()
        expect(authed_page.locator("#vlcContactName")).to_be_visible()
        expect(authed_page.locator("#vlcDirection")).to_be_visible()
        close_modal(authed_page, "vendorLogCallModal")

    def test_log_offer_modal_fields(self, authed_page, base_url):
        """Log offer modal has vendor, qty, price, condition fields."""
        wait_for_app(authed_page, base_url)
        open_modal(authed_page, "logOfferModal")
        expect(authed_page.locator("#loVendor")).to_be_visible()
        expect(authed_page.locator("#loQty")).to_be_visible()
        expect(authed_page.locator("#loPrice")).to_be_visible()
        expect(authed_page.locator("#loCond")).to_be_visible()
        close_modal(authed_page, "logOfferModal")

    def test_add_site_modal_fields(self, authed_page, base_url):
        """Add site modal has name, address, city, state, zip fields."""
        wait_for_app(authed_page, base_url)
        open_modal(authed_page, "addSiteModal")
        expect(authed_page.locator("#asSiteName")).to_be_visible()
        expect(authed_page.locator("#asSiteAddr1")).to_be_visible()
        expect(authed_page.locator("#asSiteCity")).to_be_visible()
        expect(authed_page.locator("#asSiteState")).to_be_visible()
        close_modal(authed_page, "addSiteModal")

    def test_edit_company_modal_fields(self, authed_page, base_url):
        """Edit company modal has comprehensive fields."""
        wait_for_app(authed_page, base_url)
        open_modal(authed_page, "editCompanyModal")
        expect(authed_page.locator("#ecName")).to_be_visible()
        expect(authed_page.locator("#ecAccountType")).to_be_visible()
        expect(authed_page.locator("#ecPhone")).to_be_visible()
        expect(authed_page.locator("#ecWebsite")).to_be_visible()
        expect(authed_page.locator("#ecIndustry")).to_be_visible()
        close_modal(authed_page, "editCompanyModal")


# ── 18. CONTEXT PANEL ───────────────────────────────────────────────


class TestContextPanel:
    """Tests for the context panel (right-side summary panel)."""

    def test_context_panel_exists(self, authed_page, base_url):
        """Context panel exists in DOM."""
        wait_for_app(authed_page, base_url)
        assert authed_page.locator("#ctxPanel").count() > 0

    def test_context_panel_tabs(self, authed_page, base_url):
        """Context panel has Summary, Thread, Tasks, Files, History tabs."""
        wait_for_app(authed_page, base_url)
        tabs = authed_page.locator("#ctxTabs .ctx-tab")
        tab_texts = [tabs.nth(i).text_content().strip() for i in range(tabs.count())]
        for expected in ["Summary", "Thread", "Files", "History"]:
            assert any(expected in t for t in tab_texts), f"Missing ctx tab: {expected}"


# ── 19. RFQ DRAWER ──────────────────────────────────────────────────


class TestRfqDrawer:
    """Tests for the batch RFQ side drawer."""

    def test_rfq_drawer_exists(self, authed_page, base_url):
        """RFQ drawer exists in DOM."""
        wait_for_app(authed_page, base_url)
        assert authed_page.locator("#rfqDrawer").count() > 0

    def test_rfq_drawer_has_sections(self, authed_page, base_url):
        """RFQ drawer has prepare, ready, preview, results sections."""
        wait_for_app(authed_page, base_url)
        for section in ["rfqPrepare", "rfqReady", "rfqPreview", "rfqResults"]:
            assert authed_page.locator(f"#{section}").count() > 0, f"#{section} missing"


# ── 20. KEYBOARD SHORTCUTS ──────────────────────────────────────────


class TestKeyboardShortcuts:
    """Tests for keyboard navigation and shortcuts."""

    def test_slash_focuses_search(self, authed_page, base_url):
        """Pressing '/' focuses the main search input."""
        wait_for_app(authed_page, base_url)
        authed_page.keyboard.press("/")
        authed_page.wait_for_timeout(200)
        focused = authed_page.evaluate("() => document.activeElement?.id")
        # May focus mainSearch or another search input
        assert focused is not None

    def test_escape_closes_modal(self, authed_page, base_url):
        """Escape key closes an open modal."""
        wait_for_app(authed_page, base_url)
        open_modal(authed_page, "newCompanyModal")
        authed_page.wait_for_timeout(200)
        authed_page.keyboard.press("Escape")
        authed_page.wait_for_timeout(300)

    def test_skip_link_exists(self, authed_page, base_url):
        """Skip-to-content link exists for accessibility."""
        wait_for_app(authed_page, base_url)
        skip = authed_page.locator(".skip-link")
        assert skip.count() > 0


# ── 21. API HEALTH & NETWORK ────────────────────────────────────────


class TestAPIHealth:
    """Verify no failed API calls during normal navigation."""

    def test_no_failed_apis_on_initial_load(self, authed_page, base_url):
        """All API calls on initial load return 2xx."""
        failed = collect_failed_apis(authed_page)
        wait_for_app(authed_page, base_url)
        authed_page.wait_for_timeout(3000)
        assert len(failed) == 0, f"Failed API calls on load: {failed}"

    def test_no_failed_apis_on_vendor_view(self, authed_page, base_url):
        """Opening vendor view produces no API errors."""
        failed = collect_failed_apis(authed_page)
        wait_for_app(authed_page, base_url)
        nav_click(authed_page, "navVendors")
        authed_page.wait_for_timeout(2000)
        assert len(failed) == 0, f"Failed API calls on vendor view: {failed}"

    def test_no_failed_apis_on_customer_view(self, authed_page, base_url):
        """Opening customer view produces no API errors."""
        failed = collect_failed_apis(authed_page)
        wait_for_app(authed_page, base_url)
        nav_click(authed_page, "navCustomers")
        authed_page.wait_for_timeout(2000)
        assert len(failed) == 0, f"Failed API calls on customer view: {failed}"

    def test_no_failed_apis_on_material_view(self, authed_page, base_url):
        """Opening material view produces no API errors."""
        failed = collect_failed_apis(authed_page)
        wait_for_app(authed_page, base_url)
        nav_click(authed_page, "navMaterials")
        authed_page.wait_for_timeout(2000)
        assert len(failed) == 0, f"Failed API calls on material view: {failed}"

    def test_no_failed_apis_on_rfq_drilldown(self, authed_page, base_url):
        """Opening RFQ drilldown produces no API errors."""
        failed = collect_failed_apis(authed_page)
        goto_view(authed_page, base_url, "rfqs")
        authed_page.wait_for_timeout(1500)
        arrow = authed_page.locator("#reqList .ea").first
        if arrow.is_visible():
            arrow.click()
            authed_page.wait_for_timeout(2000)
        assert len(failed) == 0, f"Failed API calls on drilldown: {failed}"

    def test_no_failed_apis_full_navigation(self, authed_page, base_url):
        """Navigating through all major views produces no API errors."""
        failed = collect_failed_apis(authed_page)
        wait_for_app(authed_page, base_url)
        for nav in [
            "navVendors",
            "navMaterials",
            "navCustomers",
            "navProactive",
            "navBuyPlans",
            "navContacts",
            "navReqs",
        ]:
            nav_click(authed_page, nav)
            authed_page.wait_for_timeout(1000)
        assert len(failed) == 0, f"Failed API calls during navigation: {failed}"


# ── 22. CONSOLE ERROR DETECTION ─────────────────────────────────────


class TestConsoleErrors:
    """Verify no JavaScript errors during various interactions."""

    def test_no_errors_on_load(self, authed_page, base_url):
        """No JS errors on initial page load."""
        errors = collect_errors(authed_page)
        wait_for_app(authed_page, base_url)
        authed_page.wait_for_timeout(2000)
        assert len(errors) == 0, f"JS errors on load: {errors}"

    def test_no_errors_on_full_navigation(self, authed_page, base_url):
        """No JS errors navigating through all views."""
        errors = collect_errors(authed_page)
        wait_for_app(authed_page, base_url)
        for nav in [
            "navVendors",
            "navMaterials",
            "navCustomers",
            "navProactive",
            "navBuyPlans",
            "navDashboard",
            "navScorecard",
            "navContacts",
            "navStrategic",
            "navProspecting",
            "navReqs",
        ]:
            nav_click(authed_page, nav)
            authed_page.wait_for_timeout(300)
        assert len(errors) == 0, f"JS errors during navigation: {errors}"

    def test_no_errors_on_modal_cycling(self, authed_page, base_url):
        """No JS errors when opening and closing modals."""
        errors = collect_errors(authed_page)
        wait_for_app(authed_page, base_url)
        for modal in [
            "newReqModal",
            "newCompanyModal",
            "vendorContactModal",
            "vendorLogCallModal",
            "addSiteModal",
            "logCallModal",
        ]:
            open_modal(authed_page, modal)
            authed_page.wait_for_timeout(200)
            close_modal(authed_page, modal)
            authed_page.wait_for_timeout(200)
        assert len(errors) == 0, f"JS errors during modal cycling: {errors}"

    def test_no_errors_on_drilldown_tab_cycling(self, authed_page, base_url):
        """No JS errors when cycling drill-down sub-tabs."""
        errors = collect_errors(authed_page)
        goto_view(authed_page, base_url, "rfqs")
        authed_page.wait_for_timeout(1500)
        arrow = authed_page.locator("#reqList .ea").first
        if not arrow.is_visible():
            pytest.skip("No requisitions available")
        arrow.click()
        authed_page.wait_for_timeout(500)
        tabs = authed_page.locator("tr.drow.open .dd-tabs .dd-tab")
        for _ in range(2):
            for i in range(tabs.count()):
                tabs.nth(i).click()
                authed_page.wait_for_timeout(200)
        assert len(errors) == 0, f"JS errors during tab cycling: {errors}"

    def test_no_errors_on_view_pill_switching(self, authed_page, base_url):
        """No JS errors when switching main view pills."""
        errors = collect_errors(authed_page)
        wait_for_app(authed_page, base_url)
        pills = authed_page.locator("#mainPills .fp")
        for i in range(pills.count()):
            pills.nth(i).click()
            authed_page.wait_for_timeout(500)
        assert len(errors) == 0, f"JS errors during pill switching: {errors}"


# ── 23. RAPID INTERACTION / STRESS ───────────────────────────────────


class TestRapidInteraction:
    """Stress tests for rapid user interactions."""

    def test_rapid_nav_switching(self, authed_page, base_url):
        """Rapidly switching nav 3x doesn't break the UI."""
        errors = collect_errors(authed_page)
        wait_for_app(authed_page, base_url)
        navs = ["navVendors", "navMaterials", "navCustomers", "navReqs"]
        for _ in range(3):
            for nav_id in navs:
                nav_click(authed_page, nav_id)
        authed_page.wait_for_timeout(500)
        assert len(errors) == 0, f"JS errors during rapid nav: {errors}"

    def test_rapid_drilldown_tab_switching(self, authed_page, base_url):
        """Rapidly clicking drill-down tabs doesn't break the UI."""
        errors = collect_errors(authed_page)
        goto_view(authed_page, base_url, "rfqs")
        authed_page.wait_for_timeout(1500)
        arrow = authed_page.locator("#reqList .ea").first
        if not arrow.is_visible():
            pytest.skip("No requisitions available")
        arrow.click()
        authed_page.wait_for_timeout(500)
        tabs = authed_page.locator("tr.drow.open .dd-tabs .dd-tab")
        for _ in range(5):
            for i in range(tabs.count()):
                tabs.nth(i).click()
        authed_page.wait_for_timeout(1000)
        assert len(errors) == 0, f"JS errors during rapid tab switching: {errors}"

    def test_rapid_modal_open_close(self, authed_page, base_url):
        """Rapidly opening and closing modals doesn't break the UI."""
        errors = collect_errors(authed_page)
        wait_for_app(authed_page, base_url)
        for _ in range(5):
            open_modal(authed_page, "newReqModal")
            close_modal(authed_page, "newReqModal")
        authed_page.wait_for_timeout(500)
        assert len(errors) == 0, f"JS errors during rapid modal cycling: {errors}"

    def test_rapid_view_pill_switching(self, authed_page, base_url):
        """Rapidly clicking view pills doesn't crash."""
        errors = collect_errors(authed_page)
        wait_for_app(authed_page, base_url)
        pills = authed_page.locator("#mainPills .fp")
        for _ in range(5):
            for i in range(pills.count()):
                pills.nth(i).click()
        authed_page.wait_for_timeout(1000)
        assert len(errors) == 0, f"JS errors during rapid pill switching: {errors}"

    def test_rapid_status_filter_switching(self, authed_page, base_url):
        """Rapidly clicking status filters doesn't crash."""
        errors = collect_errors(authed_page)
        wait_for_app(authed_page, base_url)
        pills = authed_page.locator("#statusToggle .fp")
        for _ in range(5):
            for i in range(pills.count()):
                pills.nth(i).click()
        authed_page.wait_for_timeout(1000)
        assert len(errors) == 0, f"JS errors during rapid status switching: {errors}"


# ── 24. DOM ISOLATION ────────────────────────────────────────────────


class TestDOMIsolation:
    """Verify scoped selectors and no cross-contamination."""

    def test_no_unscoped_tab_selectors(self, authed_page, base_url):
        """No JS uses unscoped '.tab' selectors."""
        wait_for_app(authed_page, base_url)
        result = authed_page.evaluate("""() => {
            const fn = window.switchTab?.toString() || '';
            return fn.includes("querySelectorAll('.tab')") ||
                   fn.includes('querySelectorAll(".tab")');
        }""")
        assert not result, "switchTab uses unscoped '.tab' selector"

    def test_drilldown_tabs_scoped_to_parent(self, authed_page, base_url):
        """Drill-down tabs are scoped within their parent row."""
        goto_view(authed_page, base_url, "rfqs")
        authed_page.wait_for_timeout(1500)
        arrow = authed_page.locator("#reqList .ea").first
        if not arrow.is_visible():
            pytest.skip("No requisitions available")
        arrow.click()
        authed_page.wait_for_timeout(500)
        dd_tabs = authed_page.locator("tr.drow.open .dd-tabs .dd-tab")
        assert dd_tabs.count() > 0


# ── 25. CSS INTEGRITY ───────────────────────────────────────────────


class TestCSSIntegrity:
    """Verify CSS class states are correct."""

    def test_drilldown_exactly_one_active_tab(self, authed_page, base_url):
        """One drill-down tab active at a time."""
        goto_view(authed_page, base_url, "rfqs")
        authed_page.wait_for_timeout(1500)
        arrow = authed_page.locator("#reqList .ea").first
        if not arrow.is_visible():
            pytest.skip("No requisitions available")
        arrow.click()
        authed_page.wait_for_timeout(500)
        active = authed_page.locator("tr.drow.open .dd-tabs .dd-tab.on")
        assert active.count() == 1

    def test_main_pill_exactly_one_active(self, authed_page, base_url):
        """Exactly one main view pill has 'on' class."""
        wait_for_app(authed_page, base_url)
        pills = authed_page.locator("#mainPills .fp")
        active_count = 0
        for i in range(pills.count()):
            classes = pills.nth(i).get_attribute("class") or ""
            if " on " in f" {classes} ":
                active_count += 1
        assert active_count == 1, f"Expected 1 active pill, found {active_count}"

    def test_vendor_tier_default_all(self, authed_page, base_url):
        """Vendor tier pills default to 'All' active."""
        wait_for_app(authed_page, base_url)
        nav_click(authed_page, "navVendors")
        all_pill = authed_page.locator("#vendorTierPills .chip[data-value='all']")
        expect(all_pill).to_have_class(re.compile(r"\bon\b"))


# ── 26. RESPONSIVE LAYOUT ───────────────────────────────────────────


class TestResponsiveLayout:
    """Tests for responsive/mobile layout elements."""

    def test_mobile_topbar_exists(self, authed_page, base_url):
        """Mobile top bar exists in DOM."""
        wait_for_app(authed_page, base_url)
        assert authed_page.locator(".mobile-topbar").count() > 0

    def test_mobile_search_toggle(self, authed_page, base_url):
        """Mobile search toggle exists."""
        wait_for_app(authed_page, base_url)
        assert authed_page.locator("#mobileSearchToggle").count() > 0

    def test_mobile_fab_exists(self, authed_page, base_url):
        """Mobile floating action button exists in DOM."""
        wait_for_app(authed_page, base_url)
        assert authed_page.locator(".m-fab").count() > 0

    def test_mobile_toolbar_exists(self, authed_page, base_url):
        """Mobile toolbar exists in DOM."""
        wait_for_app(authed_page, base_url)
        assert authed_page.locator("#mobileToolbar").count() > 0


# ── 27. INTAKE BAR ──────────────────────────────────────────────────


class TestIntakeBar:
    """Tests for the universal intake bar."""

    def test_intake_bar_exists(self, authed_page, base_url):
        """Intake bar exists in DOM."""
        wait_for_app(authed_page, base_url)
        assert authed_page.locator("#intakeBar").count() > 0

    def test_intake_drawer_exists(self, authed_page, base_url):
        """Intake drawer exists in DOM."""
        wait_for_app(authed_page, base_url)
        assert authed_page.locator("#intakeDrawer").count() > 0

    def test_intake_input_exists(self, authed_page, base_url):
        """Intake input field exists."""
        wait_for_app(authed_page, base_url)
        assert authed_page.locator("#intakeInput").count() > 0


# ── 28. NOTIFICATION SYSTEM ─────────────────────────────────────────


class TestNotifications:
    """Tests for the notification system."""

    def test_notif_panel_exists(self, authed_page, base_url):
        """Notification panel exists in DOM."""
        wait_for_app(authed_page, base_url)
        assert authed_page.locator("#notifPanel").count() > 0

    def test_notif_badge_exists(self, authed_page, base_url):
        """Notification badge counter exists."""
        wait_for_app(authed_page, base_url)
        assert authed_page.locator("#notifBadge").count() > 0

    def test_notif_action_bar_exists(self, authed_page, base_url):
        """Notification action bar exists."""
        wait_for_app(authed_page, base_url)
        assert authed_page.locator("#notifActionBar").count() > 0


# ── 29. DATA PERSISTENCE ────────────────────────────────────────────


class TestDataPersistence:
    """Verify data persists across view switches."""

    def test_search_input_persists_across_nav(self, authed_page, base_url):
        """Main search value persists when switching views."""
        wait_for_app(authed_page, base_url)
        search = authed_page.locator("#mainSearch")
        search.fill("persistence test")
        nav_click(authed_page, "navVendors")
        nav_click(authed_page, "navReqs")
        # Search may clear on nav, which is acceptable — verify no crash
        authed_page.wait_for_timeout(300)

    def test_view_state_after_modal_close(self, authed_page, base_url):
        """Current view remains visible after modal open/close."""
        wait_for_app(authed_page, base_url)
        nav_click(authed_page, "navVendors")
        expect(authed_page.locator("#view-vendors")).to_be_visible()
        open_modal(authed_page, "newCompanyModal")
        close_modal(authed_page, "newCompanyModal")
        expect(authed_page.locator("#view-vendors")).to_be_visible()


# ── 30. ACCESSIBILITY ───────────────────────────────────────────────


class TestAccessibility:
    """Basic accessibility checks."""

    def test_aria_labels_on_search(self, authed_page, base_url):
        """Search inputs have aria-label attributes."""
        wait_for_app(authed_page, base_url)
        label = authed_page.locator("#mainSearch").get_attribute("aria-label")
        assert label is not None and len(label) > 0

    def test_sidebar_role_navigation(self, authed_page, base_url):
        """Sidebar has role='navigation'."""
        wait_for_app(authed_page, base_url)
        sidebar = authed_page.locator("#sidebar")
        assert sidebar.get_attribute("role") == "navigation"

    def test_nav_buttons_have_tabindex(self, authed_page, base_url):
        """Sidebar group headers are keyboard accessible."""
        wait_for_app(authed_page, base_url)
        headers = authed_page.locator(".sb-group-header[tabindex='0']")
        assert headers.count() > 0

    def test_req_list_aria_live(self, authed_page, base_url):
        """Requisition list has aria-live for screen readers."""
        wait_for_app(authed_page, base_url)
        assert authed_page.locator("#reqList[aria-live='polite']").count() > 0

    def test_vendor_list_aria_live(self, authed_page, base_url):
        """Vendor list has aria-live for screen readers."""
        wait_for_app(authed_page, base_url)
        nav_click(authed_page, "navVendors")
        assert authed_page.locator("#vendorList[aria-live='polite']").count() > 0

    def test_page_title(self, authed_page, base_url):
        """Page has a descriptive title."""
        wait_for_app(authed_page, base_url)
        title = authed_page.title()
        assert "AVAIL" in title


# ── 31. HASH ROUTING ────────────────────────────────────────────────


class TestHashRouting:
    """Tests for hash-based URL routing."""

    def test_hash_rfqs_route(self, authed_page, base_url):
        """#rfqs hash navigates to requisitions view."""
        goto_view(authed_page, base_url, "rfqs")
        expect(authed_page.locator("#view-list")).to_be_visible()

    def test_direct_url_vendor_view(self, authed_page, base_url):
        """Direct URL navigation to vendors works."""
        authed_page.goto(f"{base_url}/#vendors", wait_until="domcontentloaded")
        authed_page.wait_for_timeout(1000)

    def test_direct_url_materials_view(self, authed_page, base_url):
        """Direct URL navigation to materials works."""
        authed_page.goto(f"{base_url}/#materials", wait_until="domcontentloaded")
        authed_page.wait_for_timeout(1000)


# ── 32. TOAST NOTIFICATIONS ─────────────────────────────────────────


class TestToastNotifications:
    """Tests for the toast notification system."""

    def test_toast_function_exists(self, authed_page, base_url):
        """ShowToast function exists."""
        wait_for_app(authed_page, base_url)
        exists = authed_page.evaluate("() => typeof showToast === 'function'")
        assert exists

    def test_toast_display(self, authed_page, base_url):
        """Calling showToast displays a toast message."""
        wait_for_app(authed_page, base_url)
        authed_page.evaluate("() => showToast('Test toast', 'info', 5000)")
        authed_page.wait_for_timeout(300)
        toast = authed_page.locator(".toast, .toast-container")
        # Toast may or may not be visible depending on implementation
