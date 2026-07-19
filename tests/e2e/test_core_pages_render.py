"""test_core_pages_render.py — deterministic "core pages MUST render" e2e guards.

The rest of the e2e suite tends to ``pytest.skip`` when a requisition / sighting /
connector card / nav link is absent, so a genuinely broken page can read green. These
tests close that gap: backed by the ``seed_e2e_data`` fixture (which runs the idempotent
``seed_sample_data`` seeder inside the app container), the CORE surfaces MUST render
their key elements — a missing element is a hard FAILURE, not a skip. The only skips
retained are genuinely environment-specific (a remote / docker-less app that cannot be
seeded), routed through ``require_core_or_skip``.

Called by: pytest tests/e2e/test_core_pages_render.py (needs the app container running;
authed via the admin session cookie from conftest).
Depends on: tests/e2e/conftest.py (authed_page, base_url, seed_e2e_data,
    require_core_or_skip).
"""

from playwright.sync_api import Page, expect

# Core bottom-nav items — always part of the app shell, never data-dependent.
CORE_NAV_HREFS = [
    "/v2/requisitions",
    "/v2/search",
    "/v2/quotes",
    "/v2/customers",
    "/v2/vendors",
    "/v2/materials",
    "/v2/approvals",
    "/v2/proactive",
    "/v2/settings",
]


class TestCoreNavShell:
    """Every core nav link is part of the always-rendered shell — a miss is a
    failure."""

    def test_all_core_nav_links_present(self, authed_page: Page, base_url: str):
        authed_page.goto(f"{base_url}/v2/requisitions", wait_until="networkidle")
        authed_page.wait_for_timeout(500)
        missing = [href for href in CORE_NAV_HREFS if authed_page.locator(f"nav a[href='{href}']").count() == 0]
        assert not missing, f"core nav links missing from the app shell: {missing}"


class TestRequisitionsPageRenders:
    """The requisitions workspace must render actual requisition rows after seeding."""

    def test_reqlist_container_renders(self, authed_page: Page, base_url: str):
        # The list container is structural — it must always render.
        authed_page.goto(f"{base_url}/v2/requisitions", wait_until="networkidle")
        authed_page.wait_for_timeout(1500)
        expect(authed_page.locator("#reqList")).to_be_visible()

    def test_requisition_rows_render(self, authed_page: Page, base_url: str, seed_e2e_data: bool, core_guard):
        authed_page.goto(f"{base_url}/v2/requisitions", wait_until="networkidle")
        authed_page.wait_for_timeout(2000)
        has_rows = authed_page.locator("#reqList .ea").count() > 0
        core_guard(seed_e2e_data, has_rows, "#reqList requisition rows")


class TestSightingsPageRenders:
    """The sightings workspace must render sighting rows after seeding."""

    def test_sighting_rows_render(self, authed_page: Page, base_url: str, seed_e2e_data: bool, core_guard):
        authed_page.goto(f"{base_url}/v2/sightings", wait_until="domcontentloaded")
        try:
            authed_page.wait_for_selector("#sightings-table tr[data-req-id]", timeout=15000)
            has_rows = True
        except Exception:
            has_rows = False
        core_guard(seed_e2e_data, has_rows, "#sightings-table rows")


class TestConnectorsPageRenders:
    """Settings → Connectors must render its shell and its (startup-seeded) cards."""

    def test_connectors_root_and_cards_render(self, authed_page: Page, base_url: str):
        authed_page.goto(f"{base_url}/v2/settings", wait_until="domcontentloaded")
        # The connectors root is structural — the tab must render.
        expect(authed_page.locator("#connectors-root")).to_be_visible(timeout=15000)
        authed_page.wait_for_timeout(400)
        # Connector cards come from the startup-seeded source registry, so a running
        # app always has them — zero cards means the connectors page is broken.
        assert authed_page.locator("[id^='connector-card-']").count() > 0, (
            "no connector cards rendered on Settings → Connectors"
        )
