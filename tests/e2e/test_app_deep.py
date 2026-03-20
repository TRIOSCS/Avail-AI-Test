"""Deep Playwright E2E tests for AvailAI (HTMX + Alpine.js UI).

Tests cover:
- Login page and auth flow
- Page load with authenticated session
- Bottom navigation between all views
- Parts list workspace (split panel, status filters)
- HTMX content swapping
- Console error detection
- API health checks
- Rapid interaction stress tests
"""

import re

import pytest
from playwright.sync_api import Page, expect

# ── Helpers ──────────────────────────────────────────────────────────


def wait_for_app(page: Page, base_url: str):
    """Navigate to the app root and wait for the shell to load."""
    page.goto(f"{base_url}/v2/requisitions", wait_until="domcontentloaded")
    page.wait_for_timeout(500)


def goto_requisitions(page: Page, base_url: str):
    """Navigate directly to the requisitions page."""
    page.goto(f"{base_url}/v2/requisitions", wait_until="domcontentloaded")
    page.wait_for_timeout(1000)


def nav_click(page: Page, href: str):
    """Click a bottom nav link by its href and wait for HTMX swap."""
    link = page.locator(f"nav a[href='{href}']")
    if link.count() == 0:
        pytest.skip(f"Nav link with href='{href}' not found")
    link.first.click()
    page.wait_for_timeout(800)


# Navigation map: (href, label text)
NAV_ITEMS = [
    ("/v2/requisitions", "Reqs"),
    ("/v2/search", "Search"),
    ("/v2/buy-plans", "Buys"),
    ("/v2/vendors", "Vendors"),
    ("/v2/materials", "Materials"),
    ("/v2/companies", "Cos"),
    ("/v2/proactive", "Proact"),
    ("/v2/quotes", "Quotes"),
    ("/v2/prospecting", "Prospect"),
    ("/v2/settings", "Config"),
]


# ── 1. AUTH & PAGE LOAD ─────────────────────────────────────────────


class TestPageLoad:
    def test_login_page_shows_for_unauthenticated(self, page, base_url):
        """Unauthenticated users see the login screen with Microsoft sign-in."""
        page.goto(base_url, wait_until="networkidle")
        # Login page has a link to /auth/login with Microsoft sign-in text
        ms_link = page.locator("a[href='/auth/login']")
        expect(ms_link).to_be_visible()
        expect(ms_link).to_contain_text("Sign in with Microsoft")

    def test_login_page_has_logo(self, page, base_url):
        """Login page displays the AVAIL logo."""
        page.goto(base_url, wait_until="networkidle")
        logo = page.locator("img[alt='AVAIL']")
        expect(logo).to_be_visible()

    def test_login_page_has_sign_in_heading(self, page, base_url):
        """Login page shows 'Sign in to continue' heading."""
        page.goto(base_url, wait_until="networkidle")
        heading = page.get_by_text("Sign in to continue")
        expect(heading).to_be_visible()

    def test_app_loads_for_authenticated_user(self, authed_page, base_url):
        """Authenticated users see the main app shell with header and nav."""
        wait_for_app(authed_page, base_url)
        # Login link should NOT be visible
        expect(authed_page.locator("a[href='/auth/login']")).not_to_be_visible()
        # Top bar header should be present
        expect(authed_page.locator("header")).to_be_visible()
        # Bottom nav should be present
        expect(authed_page.locator("nav")).to_be_visible()

    def test_topbar_has_logo(self, authed_page, base_url):
        """Top bar displays the AVAIL logo as a link."""
        wait_for_app(authed_page, base_url)
        logo = authed_page.locator("header a img[alt='AVAIL']")
        expect(logo).to_be_visible()

    def test_topbar_has_search_input(self, authed_page, base_url):
        """Top bar has the global search input."""
        wait_for_app(authed_page, base_url)
        search = authed_page.locator("header input[type='search']")
        expect(search).to_be_visible()
        # Check placeholder mentions MPN
        placeholder = search.get_attribute("placeholder") or ""
        assert "MPN" in placeholder, f"Search placeholder should mention MPN, got: {placeholder}"

    def test_main_content_area_exists(self, authed_page, base_url):
        """Main content area renders inside <main>."""
        wait_for_app(authed_page, base_url)
        expect(authed_page.locator("main")).to_be_visible()
        expect(authed_page.locator("#main-content")).to_be_visible()


# ── 2. BOTTOM NAVIGATION ────────────────────────────────────────────


class TestBottomNavigation:
    def test_all_nav_links_present(self, authed_page, base_url):
        """All 10 navigation links are present in the bottom nav."""
        wait_for_app(authed_page, base_url)
        for href, label in NAV_ITEMS:
            link = authed_page.locator(f"nav a[href='{href}']")
            assert link.count() > 0, f"Nav link for {label} ({href}) not found"

    def test_nav_labels_present(self, authed_page, base_url):
        """Each nav link contains its expected label text."""
        wait_for_app(authed_page, base_url)
        for href, label in NAV_ITEMS:
            link = authed_page.locator(f"nav a[href='{href}']")
            expect(link.first).to_contain_text(label)

    def test_navigate_to_vendors(self, authed_page, base_url):
        """Clicking Vendors nav loads vendor content."""
        wait_for_app(authed_page, base_url)
        nav_click(authed_page, "/v2/vendors")
        # URL should update to /v2/vendors
        expect(authed_page).to_have_url(re.compile(r"/v2/vendors"))

    def test_navigate_to_materials(self, authed_page, base_url):
        """Clicking Materials nav loads materials content."""
        wait_for_app(authed_page, base_url)
        nav_click(authed_page, "/v2/materials")
        expect(authed_page).to_have_url(re.compile(r"/v2/materials"))

    def test_navigate_to_companies(self, authed_page, base_url):
        """Clicking Cos nav loads companies content."""
        wait_for_app(authed_page, base_url)
        nav_click(authed_page, "/v2/companies")
        expect(authed_page).to_have_url(re.compile(r"/v2/companies"))

    def test_navigate_to_search(self, authed_page, base_url):
        """Clicking Search nav loads search content."""
        wait_for_app(authed_page, base_url)
        nav_click(authed_page, "/v2/search")
        expect(authed_page).to_have_url(re.compile(r"/v2/search"))

    def test_navigate_to_buy_plans(self, authed_page, base_url):
        """Clicking Buys nav loads buy plans content."""
        wait_for_app(authed_page, base_url)
        nav_click(authed_page, "/v2/buy-plans")
        expect(authed_page).to_have_url(re.compile(r"/v2/buy-plans"))

    def test_navigate_to_proactive(self, authed_page, base_url):
        """Clicking Proact nav loads proactive content."""
        wait_for_app(authed_page, base_url)
        nav_click(authed_page, "/v2/proactive")
        expect(authed_page).to_have_url(re.compile(r"/v2/proactive"))

    def test_navigate_to_quotes(self, authed_page, base_url):
        """Clicking Quotes nav loads quotes content."""
        wait_for_app(authed_page, base_url)
        nav_click(authed_page, "/v2/quotes")
        expect(authed_page).to_have_url(re.compile(r"/v2/quotes"))

    def test_navigate_to_settings(self, authed_page, base_url):
        """Clicking Config nav loads settings content."""
        wait_for_app(authed_page, base_url)
        nav_click(authed_page, "/v2/settings")
        expect(authed_page).to_have_url(re.compile(r"/v2/settings"))

    def test_navigate_back_to_requisitions(self, authed_page, base_url):
        """Navigating away and back to Reqs restores requisitions view."""
        wait_for_app(authed_page, base_url)
        nav_click(authed_page, "/v2/vendors")
        expect(authed_page).to_have_url(re.compile(r"/v2/vendors"))
        nav_click(authed_page, "/v2/requisitions")
        expect(authed_page).to_have_url(re.compile(r"/v2/requisitions"))

    def test_logo_click_returns_to_requisitions(self, authed_page, base_url):
        """Clicking the logo in the header returns to requisitions."""
        wait_for_app(authed_page, base_url)
        nav_click(authed_page, "/v2/vendors")
        # Click the logo link in the header
        logo_link = authed_page.locator("header a").first
        logo_link.click()
        authed_page.wait_for_timeout(800)
        expect(authed_page).to_have_url(re.compile(r"/v2/requisitions"))


# ── 3. REQUISITIONS WORKSPACE ───────────────────────────────────────


class TestRequisitionsWorkspace:
    def test_parts_list_panel_loads(self, authed_page, base_url):
        """The parts list panel loads inside the workspace."""
        goto_requisitions(authed_page, base_url)
        # Wait for HTMX to load the parts list
        authed_page.wait_for_timeout(1500)
        parts_list = authed_page.locator("#parts-list")
        expect(parts_list).to_be_visible()

    def test_status_filter_pills_present(self, authed_page, base_url):
        """Status filter pills (All, Open, Src, Ofd, Qtd, Arc) are present."""
        goto_requisitions(authed_page, base_url)
        authed_page.wait_for_timeout(1500)
        expected_labels = ["All", "Open", "Src", "Ofd", "Qtd", "Arc"]
        for label in expected_labels:
            pill = authed_page.locator(f"#parts-list button:has-text('{label}')")
            assert pill.count() > 0, f"Status pill '{label}' not found"

    def test_status_pill_click_filters(self, authed_page, base_url):
        """Clicking a status pill triggers an HTMX request to filter parts."""
        goto_requisitions(authed_page, base_url)
        authed_page.wait_for_timeout(1500)
        # Click "Open" pill
        open_pill = authed_page.locator("#parts-list button:has-text('Open')").first
        if open_pill.is_visible():
            open_pill.click()
            authed_page.wait_for_timeout(800)
            # Parts list should still be visible after filtering
            expect(authed_page.locator("#parts-list")).to_be_visible()

    def test_split_panel_layout(self, authed_page, base_url):
        """Workspace has a split panel layout."""
        goto_requisitions(authed_page, base_url)
        authed_page.wait_for_timeout(1500)
        # The workspace is a flex container with left and right panels
        workspace = authed_page.locator("#main-content .flex").first
        expect(workspace).to_be_visible()


# ── 4. STATUS PILL CYCLING ──────────────────────────────────────────


class TestStatusPillCycling:
    def test_cycle_through_all_pills(self, authed_page, base_url):
        """Clicking each status pill in sequence should not break the UI."""
        goto_requisitions(authed_page, base_url)
        authed_page.wait_for_timeout(1500)

        labels = ["Open", "Src", "Ofd", "Qtd", "Arc", "All"]
        for label in labels:
            pill = authed_page.locator(f"#parts-list button:has-text('{label}')").first
            if pill.is_visible():
                pill.click()
                authed_page.wait_for_timeout(500)

    def test_pill_cycling_no_errors(self, authed_page, base_url):
        """Cycling through all status pills should not produce JS errors."""
        errors = []
        authed_page.on("pageerror", lambda exc: errors.append(str(exc)))
        goto_requisitions(authed_page, base_url)
        authed_page.wait_for_timeout(1500)

        labels = ["Open", "Src", "Ofd", "Qtd", "Arc", "All"]
        for label in labels:
            pill = authed_page.locator(f"#parts-list button:has-text('{label}')").first
            if pill.is_visible():
                pill.click()
                authed_page.wait_for_timeout(500)

        assert len(errors) == 0, f"JS errors during pill cycling: {errors}"


# ── 5. HTMX CONTENT LOADING ─────────────────────────────────────────


class TestHTMXContentLoading:
    def test_main_content_swaps_on_nav(self, authed_page, base_url):
        """Navigating between views swaps #main-content via HTMX."""
        wait_for_app(authed_page, base_url)
        # Get initial content
        initial_html = authed_page.locator("#main-content").inner_html()

        # Navigate to vendors
        nav_click(authed_page, "/v2/vendors")
        authed_page.wait_for_timeout(500)
        vendor_html = authed_page.locator("#main-content").inner_html()

        # Content should be different after navigation
        assert initial_html != vendor_html, "Main content should change after navigation"

    def test_htmx_preserves_header(self, authed_page, base_url):
        """HTMX navigation should not replace the header or bottom nav."""
        wait_for_app(authed_page, base_url)
        nav_click(authed_page, "/v2/vendors")
        # Header and nav should still be visible
        expect(authed_page.locator("header")).to_be_visible()
        expect(authed_page.locator("nav")).to_be_visible()

    def test_url_updates_on_htmx_nav(self, authed_page, base_url):
        """HTMX navigation should update the browser URL via hx-push-url."""
        wait_for_app(authed_page, base_url)
        nav_click(authed_page, "/v2/materials")
        expect(authed_page).to_have_url(re.compile(r"/v2/materials"))

        nav_click(authed_page, "/v2/requisitions")
        expect(authed_page).to_have_url(re.compile(r"/v2/requisitions"))


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
        """No JavaScript errors when navigating between views."""
        errors = []
        authed_page.on("pageerror", lambda exc: errors.append(str(exc)))
        wait_for_app(authed_page, base_url)

        for href, _ in NAV_ITEMS[:5]:  # Test first 5 nav items
            nav_click(authed_page, href)
            authed_page.wait_for_timeout(300)

        assert len(errors) == 0, f"JS errors during navigation: {errors}"


# ── 7. API HEALTH ───────────────────────────────────────────────────


class TestAPIHealth:
    def test_no_failed_api_calls_on_load(self, authed_page, base_url):
        """All API/partial calls on initial load should return 2xx."""
        failed = []

        def on_response(response):
            if ("/api/" in response.url or "/v2/partials/" in response.url) and response.status >= 400:
                failed.append(f"{response.status} {response.url}")

        authed_page.on("response", on_response)
        wait_for_app(authed_page, base_url)
        authed_page.wait_for_timeout(3000)
        assert len(failed) == 0, f"Failed API calls on load: {failed}"

    def test_no_failed_api_on_requisitions(self, authed_page, base_url):
        """Loading requisitions should not produce API errors."""
        failed = []

        def on_response(response):
            if ("/api/" in response.url or "/v2/partials/" in response.url) and response.status >= 400:
                failed.append(f"{response.status} {response.url}")

        authed_page.on("response", on_response)
        goto_requisitions(authed_page, base_url)
        authed_page.wait_for_timeout(2000)
        assert len(failed) == 0, f"Failed API calls: {failed}"

    def test_no_failed_api_on_vendor_nav(self, authed_page, base_url):
        """Navigating to vendors should not produce API errors."""
        failed = []

        def on_response(response):
            if ("/api/" in response.url or "/v2/partials/" in response.url) and response.status >= 400:
                failed.append(f"{response.status} {response.url}")

        authed_page.on("response", on_response)
        wait_for_app(authed_page, base_url)
        nav_click(authed_page, "/v2/vendors")
        authed_page.wait_for_timeout(2000)
        assert len(failed) == 0, f"Failed API calls: {failed}"


# ── 8. VENDOR VIEW ──────────────────────────────────────────────────


class TestVendorView:
    def test_vendor_page_loads(self, authed_page, base_url):
        """Vendor view loads content into main."""
        wait_for_app(authed_page, base_url)
        nav_click(authed_page, "/v2/vendors")
        expect(authed_page.locator("#main-content")).to_be_visible()
        # Content should not be empty
        content = authed_page.locator("#main-content").inner_text()
        assert len(content.strip()) > 0, "Vendor view content is empty"


# ── 9. MATERIALS VIEW ───────────────────────────────────────────────


class TestMaterialView:
    def test_material_page_loads(self, authed_page, base_url):
        """Material view loads content into main."""
        wait_for_app(authed_page, base_url)
        nav_click(authed_page, "/v2/materials")
        expect(authed_page.locator("#main-content")).to_be_visible()
        content = authed_page.locator("#main-content").inner_text()
        assert len(content.strip()) > 0, "Material view content is empty"


# ── 10. RAPID INTERACTION STRESS ─────────────────────────────────────


class TestRapidInteraction:
    def test_rapid_nav_switching(self, authed_page, base_url):
        """Rapidly switching nav should not break the UI or produce JS errors."""
        errors = []
        authed_page.on("pageerror", lambda exc: errors.append(str(exc)))
        wait_for_app(authed_page, base_url)

        nav_hrefs = ["/v2/vendors", "/v2/materials", "/v2/companies", "/v2/requisitions"]
        for _ in range(3):
            for href in nav_hrefs:
                nav_click(authed_page, href)

        authed_page.wait_for_timeout(500)
        # Should end on requisitions
        expect(authed_page).to_have_url(re.compile(r"/v2/requisitions"))
        assert len(errors) == 0, f"JS errors during rapid nav: {errors}"

    def test_rapid_pill_switching(self, authed_page, base_url):
        """Rapidly clicking status pills should not break the UI."""
        errors = []
        authed_page.on("pageerror", lambda exc: errors.append(str(exc)))
        goto_requisitions(authed_page, base_url)
        authed_page.wait_for_timeout(1500)

        labels = ["Open", "Src", "Ofd", "Qtd", "Arc", "All"]
        for _ in range(2):
            for label in labels:
                pill = authed_page.locator(f"#parts-list button:has-text('{label}')").first
                if pill.is_visible():
                    pill.click()
                    authed_page.wait_for_timeout(100)

        authed_page.wait_for_timeout(500)
        assert len(errors) == 0, f"JS errors during rapid pill switching: {errors}"


# ── 11. GLOBAL SEARCH INPUT ─────────────────────────────────────────


class TestGlobalSearch:
    def test_search_input_accepts_text(self, authed_page, base_url):
        """The global search input accepts typed text."""
        wait_for_app(authed_page, base_url)
        search = authed_page.locator("header input[type='search']")
        search.fill("LM358")
        expect(search).to_have_value("LM358")

    def test_search_escape_clears_focus(self, authed_page, base_url):
        """Pressing Escape on the search input blurs it."""
        wait_for_app(authed_page, base_url)
        search = authed_page.locator("header input[type='search']")
        search.focus()
        search.fill("test")
        authed_page.keyboard.press("Escape")
        authed_page.wait_for_timeout(300)
        # The search results dropdown should not be visible
        results = authed_page.locator("#global-search-results")
        expect(results).not_to_be_visible()


# ── 12. DIRECT URL NAVIGATION ───────────────────────────────────────


class TestDirectURLNavigation:
    def test_direct_url_to_vendors(self, authed_page, base_url):
        """Navigating directly to /v2/vendors loads the vendor view."""
        authed_page.goto(f"{base_url}/v2/vendors", wait_until="domcontentloaded")
        authed_page.wait_for_timeout(1000)
        expect(authed_page.locator("header")).to_be_visible()
        expect(authed_page.locator("nav")).to_be_visible()
        expect(authed_page.locator("#main-content")).to_be_visible()

    def test_direct_url_to_materials(self, authed_page, base_url):
        """Navigating directly to /v2/materials loads the materials view."""
        authed_page.goto(f"{base_url}/v2/materials", wait_until="domcontentloaded")
        authed_page.wait_for_timeout(1000)
        expect(authed_page.locator("header")).to_be_visible()
        expect(authed_page.locator("#main-content")).to_be_visible()

    def test_direct_url_to_settings(self, authed_page, base_url):
        """Navigating directly to /v2/settings loads the settings view."""
        authed_page.goto(f"{base_url}/v2/settings", wait_until="domcontentloaded")
        authed_page.wait_for_timeout(1000)
        expect(authed_page.locator("header")).to_be_visible()
        expect(authed_page.locator("#main-content")).to_be_visible()
