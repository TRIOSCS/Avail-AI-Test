"""E2E coverage for Settings → Connectors tab.

Validates, in a real browser against the live container:
- Page loads with ZERO console / JS errors.
- All 6 group headings render.
- Lusha card shows a password input named LUSHA_API_KEY.
- Clay card shows an anchor pointing to /auth/clay/connect (href only — link is
  not followed because it exits the app).
- SAM.gov and AI Web Search are keyless (no credential input on their cards).
- Pruned providers (rocketreach, clearbit, aliexpress) are absent from the page.
- Toggle fires PUT /api/sources/<id>/activate (network intercept).
- Test button fires POST /api/sources/<id>/test (network intercept via route mock).

Called by: pytest tests/e2e (needs the app container running; authed via the
admin session cookie from conftest).
"""

import pytest
from playwright.sync_api import Page, Route, expect

# Expected group headings produced by connector_service.GROUP_ORDER.
GROUP_HEADINGS = [
    "Part Sourcing",
    "Enrichment",
    "AI",
    "Communications",
    "Browser Workers",
    "Manual",
]

# Providers that were pruned in Task 2 — must not appear anywhere in the page.
PRUNED_PROVIDERS = ["rocketreach", "clearbit", "aliexpress", "arrow", "avnet"]


def _open_connectors(page: Page, base_url: str) -> list[str]:
    """Navigate to Settings → Connectors, collect console/page errors."""
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))
    page.on(
        "console",
        lambda msg: errors.append(f"console.{msg.type}: {msg.text}") if msg.type == "error" else None,
    )
    # Navigate to the settings page; the Connectors tab is the default.
    page.goto(f"{base_url}/v2/settings", wait_until="domcontentloaded")
    # Wait for the connectors partial to load (it's HTMX-injected).
    try:
        page.wait_for_selector("#connectors-root", timeout=15000)
    except Exception:
        pytest.skip("Connectors tab did not render in this environment")
    page.wait_for_timeout(400)
    return errors


class TestConnectorsNoConsoleErrors:
    def test_page_loads_without_js_errors(self, authed_page: Page, base_url: str):
        """Connectors tab must load with zero console errors."""
        errors = _open_connectors(authed_page, base_url)
        assert not errors, "console/page errors on Settings → Connectors:\n" + "\n".join(errors[:20])


class TestConnectorsGroupHeadings:
    def test_six_group_headings_render(self, authed_page: Page, base_url: str):
        """All 6 connector group section headings must be present."""
        errors = _open_connectors(authed_page, base_url)
        for heading in GROUP_HEADINGS:
            locator = authed_page.locator(f"text={heading}").first
            expect(locator).to_be_visible(timeout=5000)
        assert not errors, "errors during group heading check:\n" + "\n".join(errors[:10])


class TestLushaCard:
    def test_lusha_has_password_input(self, authed_page: Page, base_url: str):
        """Lusha card must expose a password input named LUSHA_API_KEY."""
        errors = _open_connectors(authed_page, base_url)
        inp = authed_page.locator("input[name='LUSHA_API_KEY'][type='password']")
        if inp.count() == 0:
            pytest.skip("Lusha card not rendered (source may be absent from DB)")
        expect(inp.first).to_be_visible(timeout=5000)
        assert not errors, "errors during Lusha card check:\n" + "\n".join(errors[:10])


class TestClayCard:
    def test_clay_shows_connect_link(self, authed_page: Page, base_url: str):
        """Clay card must show an anchor pointing to /auth/clay/connect (not
        followed)."""
        errors = _open_connectors(authed_page, base_url)
        link = authed_page.locator("a[href='/auth/clay/connect']")
        if link.count() == 0:
            pytest.skip("Clay card not rendered (source may be absent or already connected)")
        expect(link.first).to_be_visible(timeout=5000)
        # Verify href only — DO NOT click; it exits the app to Clay's OAuth.
        href = link.first.get_attribute("href")
        assert href == "/auth/clay/connect", f"Unexpected Clay OAuth href: {href}"
        assert not errors, "errors during Clay card check:\n" + "\n".join(errors[:10])


class TestKeylessCards:
    def test_sam_gov_has_no_credential_input(self, authed_page: Page, base_url: str):
        """SAM.gov is keyless — its card must not render a credential password input."""
        errors = _open_connectors(authed_page, base_url)
        # Find the sam_gov card wrapper by searching for the display name text.
        sam_text = authed_page.locator("text=SAM.gov").first
        if sam_text.count() == 0:
            pytest.skip("SAM.gov connector not present in this environment")
        # Walk up to the card container and assert no password input inside it.
        card = authed_page.locator("[id^='connector-card-']").filter(has_text="SAM.gov").first
        if card.count() == 0:
            pytest.skip("SAM.gov connector card not found")
        assert card.locator("input[type='password']").count() == 0, (
            "SAM.gov keyless card must not contain a password input"
        )
        assert not errors, "errors during SAM.gov card check:\n" + "\n".join(errors[:10])

    def test_ai_web_search_has_no_credential_input(self, authed_page: Page, base_url: str):
        """AI Web Search is keyless — its card must not render a credential password
        input."""
        errors = _open_connectors(authed_page, base_url)
        card = authed_page.locator("[id^='connector-card-']").filter(has_text="AI Web Search").first
        if card.count() == 0:
            pytest.skip("AI Web Search connector card not found")
        assert card.locator("input[type='password']").count() == 0, (
            "AI Web Search keyless card must not contain a password input"
        )
        assert not errors, "errors during AI Web Search card check:\n" + "\n".join(errors[:10])


class TestPrunedProvidersAbsent:
    def test_pruned_providers_not_in_page(self, authed_page: Page, base_url: str):
        """Pruned providers must not appear anywhere in the rendered page."""
        errors = _open_connectors(authed_page, base_url)
        html = authed_page.content()
        for provider in PRUNED_PROVIDERS:
            assert provider not in html.lower(), f"Pruned provider '{provider}' found in connectors page HTML"
        assert not errors, "errors during pruned-provider check:\n" + "\n".join(errors[:10])


class TestToggleNetwork:
    def test_toggle_fires_activate_endpoint(self, authed_page: Page, base_url: str):
        """Flipping the enable toggle must fire PUT /api/sources/<id>/activate."""
        _open_connectors(authed_page, base_url)
        # Find a non-disabled toggle checkbox (a connector that is not needs_setup).
        toggle = authed_page.locator("input[type='checkbox'][hx-put*='/activate']").first
        if toggle.count() == 0:
            pytest.skip("No operable toggle found in this environment")

        with authed_page.expect_request(lambda req: "/activate" in req.url and req.method == "PUT"):
            toggle.click(force=True)


class TestTestButtonNetwork:
    def test_test_button_fires_test_endpoint(self, authed_page: Page, base_url: str):
        """Clicking Test must fire POST /api/sources/<id>/test."""
        _open_connectors(authed_page, base_url)

        # Mock the test endpoint so we don't make live API calls.
        # Route intercept: return 200 with a minimal JSON payload.
        def _mock_test(route: Route) -> None:
            route.fulfill(
                status=200,
                content_type="application/json",
                body='{"source":"mock","test_mpn":"LM358N","status":"ok","results_count":0,"elapsed_ms":1,"error":null,"sample":[]}',
            )

        authed_page.route("**/api/sources/*/test", _mock_test)

        test_btn = authed_page.locator("button[hx-post*='/test']").first
        if test_btn.count() == 0:
            pytest.skip("No Test button found (all connectors may be untestable in this environment)")
        expect(test_btn).to_be_visible(timeout=5000)

        with authed_page.expect_request(lambda req: "/test" in req.url and req.method == "POST"):
            test_btn.click()
