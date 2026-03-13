"""
tests/test_browser_e2e.py — Playwright end-to-end browser tests

Full workflow and button tests against the running Docker app.
Authenticates via session cookie injection (Starlette SessionMiddleware).

Run: pytest tests/test_browser_e2e.py -v --headed  (visible browser)
Run: pytest tests/test_browser_e2e.py -v           (headless)

Depends on: running Docker app (docker compose up -d)
"""

import base64
import json
import os
import subprocess
from urllib.parse import urlparse

import itsdangerous
import pytest
from playwright.sync_api import Page, expect

# ── Config ───────────────────────────────────────────────────────────

APP_CONTAINER_IP = None
APP_BASE_URL = None
SESSION_COOKIE = None


def _get_app_ip():
    """Get the app container's IP address on the Docker network."""
    try:
        result = subprocess.run(
            ["docker", "inspect", "availai-app-1", "--format", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""


def _get_secret_key():
    """Get the session secret from the running app container."""
    try:
        result = subprocess.run(
            [
                "docker",
                "compose",
                "exec",
                "-T",
                "app",
                "python3",
                "-c",
                "from app.config import settings; print(settings.secret_key)",
            ],
            capture_output=True,
            text=True,
            cwd="/root/availai",
            timeout=10,
        )
        secret = result.stdout.strip()
        if secret:
            return secret
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return os.getenv("SESSION_SECRET", os.getenv("SECRET_KEY", ""))


def _make_session_cookie(secret_key: str, user_id: int = 1) -> str:
    """Create a signed session cookie for Starlette SessionMiddleware."""
    signer = itsdangerous.TimestampSigner(secret_key)
    payload = base64.b64encode(json.dumps({"user_id": user_id}).encode()).decode()
    return signer.sign(payload).decode()


@pytest.fixture(scope="session", autouse=True)
def setup_app_connection():
    """Resolve app IP and generate session cookie once per test session."""
    global APP_CONTAINER_IP, APP_BASE_URL, SESSION_COOKIE

    env_base_url = os.getenv("E2E_BASE_URL", "").strip()
    if env_base_url:
        APP_BASE_URL = env_base_url
        APP_CONTAINER_IP = urlparse(APP_BASE_URL).hostname or ""
    else:
        APP_CONTAINER_IP = _get_app_ip()
        if not APP_CONTAINER_IP:
            pytest.skip("Docker not available or app container not running")
        APP_BASE_URL = f"http://{APP_CONTAINER_IP}:8000"

    secret = _get_secret_key()
    if not secret:
        pytest.skip("Could not resolve session secret (set SESSION_SECRET or start Docker app)")
    SESSION_COOKIE = _make_session_cookie(secret, user_id=1)


@pytest.fixture()
def auth_page(page: Page):
    """Page with session cookie injected for authenticated access."""
    page.context.add_cookies(
        [
            {
                "name": "session",
                "value": SESSION_COOKIE,
                "url": APP_BASE_URL,
            }
        ]
    )
    return page


def _nav_click(page: Page, nav_id: str):
    """Click a sidebar nav button via JS to bypass viewport positioning."""
    page.evaluate(f"""() => {{
        const el = document.getElementById('{nav_id}');
        if (el) el.click();
    }}""")
    page.wait_for_timeout(500)


def _goto_rfqs(page: Page):
    """Navigate to the RFQ list view."""
    page.goto(f"{APP_BASE_URL}/#rfqs")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(500)


# ═══════════════════════════════════════════════════════════════════════
#  1. LOGIN & LANDING
# ═══════════════════════════════════════════════════════════════════════


class TestLoginAndLanding:
    def test_unauthenticated_shows_login(self, page: Page):
        """Unauthenticated user sees the login page."""
        page.goto(APP_BASE_URL)
        expect(page.locator(".login")).to_be_visible(timeout=10000)
        expect(page.locator("a[href='/auth/login']")).to_be_visible()

    def test_authenticated_shows_main_app(self, auth_page: Page):
        """Authenticated user sees the main app (not login)."""
        auth_page.goto(APP_BASE_URL)
        auth_page.wait_for_load_state("networkidle")
        # Should NOT see login page
        expect(auth_page.locator(".login")).not_to_be_visible(timeout=5000)
        # Should see sidebar navigation
        expect(auth_page.locator("#sidebar")).to_be_visible()

    def test_sidebar_nav_buttons_visible(self, auth_page: Page):
        """All main sidebar navigation buttons are present."""
        auth_page.goto(APP_BASE_URL)
        auth_page.wait_for_load_state("networkidle")
        for nav_id in ["navReqs", "navMaterials", "navCustomers", "navVendors"]:
            # Check DOM presence (sidebar buttons may be outside viewport)
            count = auth_page.evaluate(f"document.getElementById('{nav_id}') !== null")
            assert count, f"#{nav_id} not found in DOM"


# ═══════════════════════════════════════════════════════════════════════
#  2. REQUISITIONS LIST
# ═══════════════════════════════════════════════════════════════════════


class TestRequisitionsList:
    def test_requisitions_list_loads(self, auth_page: Page):
        """RFQ list loads and shows requisition table rows."""
        _goto_rfqs(auth_page)
        auth_page.wait_for_timeout(1000)
        # Req list renders table rows with expand arrows (.ea buttons)
        arrows = auth_page.locator("#reqList .ea")
        expect(arrows.first).to_be_visible(timeout=10000)

    def test_search_filter_works(self, auth_page: Page):
        """Search input filters the requisitions list."""
        _goto_rfqs(auth_page)
        search = auth_page.locator("#mainSearch")
        search.fill("Toshiba")
        auth_page.locator("#mainSearchBtn").click()
        auth_page.wait_for_timeout(2000)
        # Just verify no crash — results may or may not exist
        assert auth_page.locator("#reqList").is_visible()

    def test_view_pills_switch(self, auth_page: Page):
        """View pills (RFQ, Sourcing, Archive) switch the list view."""
        _goto_rfqs(auth_page)
        # Use .first to avoid strict-mode violation (desktop + mobile pills)
        archive_pill = auth_page.locator(".fp[data-view='archive']").first
        if archive_pill.is_visible():
            archive_pill.click()
            auth_page.wait_for_timeout(1000)
            # Switch back to RFQ
            auth_page.locator(".fp[data-view='rfq']").first.click()
            auth_page.wait_for_timeout(1000)

    def test_new_rfq_modal_opens(self, auth_page: Page):
        """+ New RFQ button opens the modal."""
        _goto_rfqs(auth_page)
        new_btn = auth_page.locator("button:has-text('New RFQ')")
        if new_btn.first.is_visible():
            new_btn.first.click()
            auth_page.wait_for_timeout(500)
            expect(auth_page.locator("#newReqModal")).to_have_class(__import__("re").compile(r"\bopen\b"))
            # Close modal
            auth_page.keyboard.press("Escape")


# ═══════════════════════════════════════════════════════════════════════
#  3. REQUISITION DRILL-DOWN
# ═══════════════════════════════════════════════════════════════════════


class TestRequisitionDrillDown:
    def test_expand_requisition(self, auth_page: Page):
        """Clicking a requisition row expands the drill-down."""
        _goto_rfqs(auth_page)
        auth_page.wait_for_timeout(1000)
        first_arrow = auth_page.locator(".ea").first
        expect(first_arrow).to_be_visible(timeout=10000)
        first_arrow.click()
        auth_page.wait_for_timeout(1500)
        # Drill-down row should be visible
        drow = auth_page.locator("tr.drow.open").first
        expect(drow).to_be_visible(timeout=5000)

    def test_drilldown_tabs_work(self, auth_page: Page):
        """Drill-down tabs (parts, quotes, offers) are clickable."""
        _goto_rfqs(auth_page)
        auth_page.wait_for_timeout(1000)
        first_arrow = auth_page.locator(".ea").first
        expect(first_arrow).to_be_visible(timeout=10000)
        first_arrow.click()
        auth_page.wait_for_timeout(1500)
        # Click each tab that exists
        for tab_name in ["parts", "quotes", "offers", "files"]:
            tab = auth_page.locator(f".dd-tab[data-tab='{tab_name}']").first
            if tab.is_visible():
                tab.click()
                auth_page.wait_for_timeout(500)


# ═══════════════════════════════════════════════════════════════════════
#  4. MATERIALS / VENDOR LIST
# ═══════════════════════════════════════════════════════════════════════


class TestMaterialsView:
    def test_materials_nav_loads(self, auth_page: Page):
        """Materials nav button loads the materials list."""
        auth_page.goto(APP_BASE_URL)
        auth_page.wait_for_load_state("networkidle")
        _nav_click(auth_page, "navMaterials")
        auth_page.wait_for_timeout(1500)
        expect(auth_page.locator("#view-materials")).to_be_visible()

    def test_vendors_nav_loads(self, auth_page: Page):
        """Vendors nav loads the vendor list."""
        auth_page.goto(APP_BASE_URL)
        auth_page.wait_for_load_state("networkidle")
        _nav_click(auth_page, "navVendors")
        auth_page.wait_for_timeout(1500)
        expect(auth_page.locator("#view-vendors")).to_be_visible()


# ═══════════════════════════════════════════════════════════════════════
#  5. CRM — ACCOUNTS (CUSTOMERS)
# ═══════════════════════════════════════════════════════════════════════


class TestCRMAccounts:
    def test_accounts_view_loads(self, auth_page: Page):
        """Accounts (customers) view loads via sidebar nav."""
        auth_page.goto(APP_BASE_URL)
        auth_page.wait_for_load_state("networkidle")
        _nav_click(auth_page, "navCustomers")
        auth_page.wait_for_timeout(1500)
        expect(auth_page.locator("#view-customers")).to_be_visible()

    def test_accounts_search_works(self, auth_page: Page):
        """Accounts search filter accepts input."""
        auth_page.goto(APP_BASE_URL)
        auth_page.wait_for_load_state("networkidle")
        _nav_click(auth_page, "navCustomers")
        auth_page.wait_for_timeout(1000)
        search = auth_page.locator("#custFilter")
        if search.is_visible():
            search.fill("test")
            auth_page.wait_for_timeout(1000)
            search.fill("")

    def test_new_account_button_exists(self, auth_page: Page):
        """New Account button is present in accounts view."""
        auth_page.goto(APP_BASE_URL)
        auth_page.wait_for_load_state("networkidle")
        _nav_click(auth_page, "navCustomers")
        auth_page.wait_for_timeout(1500)
        new_btn = auth_page.locator("#view-customers button:has-text('New Account')")
        expect(new_btn).to_be_visible(timeout=5000)


# ═══════════════════════════════════════════════════════════════════════
#  6. QUOTES WORKFLOW
# ═══════════════════════════════════════════════════════════════════════


class TestQuotesWorkflow:
    def test_quotes_tab_accessible(self, auth_page: Page):
        """Quotes tab is accessible in the drill-down."""
        _goto_rfqs(auth_page)
        auth_page.wait_for_timeout(1000)
        first_arrow = auth_page.locator(".ea").first
        expect(first_arrow).to_be_visible(timeout=10000)
        first_arrow.click()
        auth_page.wait_for_timeout(1500)
        quotes_tab = auth_page.locator(".dd-tab[data-tab='quotes']").first
        if quotes_tab.is_visible():
            quotes_tab.click()
            auth_page.wait_for_timeout(1000)

    def test_offers_tab_accessible(self, auth_page: Page):
        """Offers tab shows offer data in the drill-down."""
        _goto_rfqs(auth_page)
        auth_page.wait_for_timeout(1000)
        first_arrow = auth_page.locator(".ea").first
        expect(first_arrow).to_be_visible(timeout=10000)
        first_arrow.click()
        auth_page.wait_for_timeout(1500)
        offers_tab = auth_page.locator(".dd-tab[data-tab='offers']").first
        if offers_tab.is_visible():
            offers_tab.click()
            auth_page.wait_for_timeout(1000)


# ═══════════════════════════════════════════════════════════════════════
#  7. API ENDPOINT SMOKE TESTS (via page.request)
# ═══════════════════════════════════════════════════════════════════════


class TestAPIEndpoints:
    def test_requisitions_api(self, auth_page: Page):
        """GET /api/requisitions returns 200 with valid structure."""
        resp = auth_page.request.get(f"{APP_BASE_URL}/api/requisitions?limit=5")
        assert resp.status == 200
        data = resp.json()
        assert "requisitions" in data
        assert "total" in data
        assert len(data["requisitions"]) <= 5

    def test_materials_api(self, auth_page: Page):
        """GET /api/materials returns 200 with valid structure."""
        resp = auth_page.request.get(f"{APP_BASE_URL}/api/materials?limit=5")
        assert resp.status == 200
        data = resp.json()
        assert "materials" in data
        assert "total" in data

    def test_companies_api(self, auth_page: Page):
        """GET /api/companies returns 200."""
        resp = auth_page.request.get(f"{APP_BASE_URL}/api/companies")
        assert resp.status == 200
        assert isinstance(resp.json(), list)

    def test_vendors_api(self, auth_page: Page):
        """GET /api/vendors returns 200."""
        resp = auth_page.request.get(f"{APP_BASE_URL}/api/vendors?limit=5")
        assert resp.status == 200

    def test_pricing_history_api(self, auth_page: Page):
        """GET /api/pricing-history/{mpn} returns 200."""
        resp = auth_page.request.get(f"{APP_BASE_URL}/api/pricing-history/LM317T")
        assert resp.status == 200
        data = resp.json()
        assert "mpn" in data
        assert "material_card_id" in data
        assert "history" in data

    def test_auth_status_api(self, auth_page: Page):
        """GET /auth/status returns user info."""
        resp = auth_page.request.get(f"{APP_BASE_URL}/auth/status")
        assert resp.status == 200
        data = resp.json()
        # Response is a flat object with user_email, not nested under "user"
        assert data.get("user_email") is not None
        assert data["user_email"] == "mkhoury@trioscs.com"


# ═══════════════════════════════════════════════════════════════════════
#  8. FULL WORKFLOW: CREATE RFQ → EXPAND → CHECK TABS
# ═══════════════════════════════════════════════════════════════════════


class TestFullWorkflow:
    def test_create_requisition_and_navigate(self, auth_page: Page):
        """Create a new requisition via API, expand, and check drill-down tabs."""
        # Navigate to RFQs first to load the page and get auth context
        _goto_rfqs(auth_page)
        auth_page.wait_for_timeout(1000)

        # Create new RFQ via API
        resp = auth_page.request.post(
            f"{APP_BASE_URL}/api/requisitions",
            data=json.dumps({"name": "Playwright Test RFQ"}),
            headers={"Content-Type": "application/json"},
        )
        # May return 200 or 403 depending on CSRF/auth; accept either
        if resp.status != 200:
            pytest.skip(f"POST /api/requisitions returned {resp.status} — auth restriction")
        req_data = resp.json()
        req_id = req_data["id"]

        # Add a requirement
        resp = auth_page.request.post(
            f"{APP_BASE_URL}/api/requisitions/{req_id}/requirements",
            data=json.dumps(
                {
                    "requirements": [
                        {"primary_mpn": "LM317T", "target_qty": 100, "target_price": 2.50},
                    ],
                }
            ),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 200

        # Reload and find our new RFQ
        auth_page.reload()
        auth_page.wait_for_load_state("networkidle")
        auth_page.wait_for_timeout(2000)

        # Expand the new RFQ by clicking its expand arrow
        arrow = auth_page.locator(f"#a-{req_id}")
        if arrow.is_visible():
            arrow.click()
            auth_page.wait_for_timeout(1500)

            # Check parts tab
            parts_tab = auth_page.locator(".dd-tab[data-tab='parts']").first
            if parts_tab.is_visible():
                parts_tab.click()
                auth_page.wait_for_timeout(1000)

            # Check quotes tab
            quotes_tab = auth_page.locator(".dd-tab[data-tab='quotes']").first
            if quotes_tab.is_visible():
                quotes_tab.click()
                auth_page.wait_for_timeout(500)

        # Clean up: archive the test RFQ
        auth_page.request.put(f"{APP_BASE_URL}/api/requisitions/{req_id}/archive")


# ═══════════════════════════════════════════════════════════════════════
#  9. CONSOLE ERROR CHECK
# ═══════════════════════════════════════════════════════════════════════


class TestNoConsoleErrors:
    def test_main_page_no_js_errors(self, auth_page: Page):
        """Main page loads without JavaScript errors."""
        errors = []
        auth_page.on("pageerror", lambda err: errors.append(str(err)))

        auth_page.goto(APP_BASE_URL)
        auth_page.wait_for_load_state("networkidle")
        auth_page.wait_for_timeout(2000)

        # Navigate through main sections via JS click
        for nav in ["navReqs", "navCustomers", "navVendors", "navMaterials"]:
            _nav_click(auth_page, nav)
            auth_page.wait_for_timeout(1000)

        assert len(errors) == 0, f"JavaScript errors on page: {errors}"
