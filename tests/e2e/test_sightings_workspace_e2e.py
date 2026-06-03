"""E2E coverage for the /v2/sightings split-panel — the page whose root x-data was
broken by a double-quote inside an in-attribute JS comment (dead right panel, dead
slide-bar) and whose material-card modal had no close control.

Validates, in a real browser against the live container:
- the page loads with ZERO console errors / page errors (the bug emitted ~59),
- clicking a requirement row populates the right detail panel,
- dragging the split divider resizes the panels,
- clicking an MPN chip opens the material card modal AND the global chrome X
  closes it.

Called by: pytest tests/e2e (needs the app container running; authed via the
admin session cookie from conftest).
"""

import pytest
from playwright.sync_api import Page, expect


def _open_sightings(page: Page, base_url: str) -> list[str]:
    """Navigate to sightings, collect console/page errors, wait for rows."""
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))
    page.on(
        "console",
        lambda msg: errors.append(f"console.{msg.type}: {msg.text}") if msg.type == "error" else None,
    )
    page.goto(f"{base_url}/v2/sightings", wait_until="domcontentloaded")
    # The table lazy-loads into #sightings-table; wait for real rows.
    try:
        page.wait_for_selector("#sightings-table tr[data-req-id]", timeout=15000)
    except Exception:
        pytest.skip("no sightings rows available in this environment")
    page.wait_for_timeout(400)
    return errors


class TestSightingsNoConsoleErrors:
    def test_page_loads_without_js_errors(self, authed_page: Page, base_url: str):
        """A broken root x-data emitted dozens of 'is not defined' errors at load."""
        errors = _open_sightings(authed_page, base_url)
        assert not errors, "console/page errors on /v2/sightings:\n" + "\n".join(errors[:20])


class TestSightingsRightPanel:
    def test_row_click_populates_detail(self, authed_page: Page, base_url: str):
        """SelectReq() must fire and swap content into #sightings-detail."""
        errors = _open_sightings(authed_page, base_url)
        authed_page.locator("#sightings-table tr[data-req-id]").first.click()
        detail = authed_page.locator("#sightings-detail")
        # The panel becomes visible (x-show=selectedReqId) and receives content.
        expect(detail).to_be_visible(timeout=10000)
        authed_page.wait_for_function(
            "document.querySelector('#sightings-detail')?.children.length > 0",
            timeout=10000,
        )
        assert not errors, "errors during row click:\n" + "\n".join(errors[:20])


class TestSightingsSlideBar:
    def test_drag_divider_resizes(self, authed_page: Page, base_url: str):
        """Dragging the split handle changes the persisted split ratio."""
        _open_sightings(authed_page, base_url)
        handle = authed_page.locator(".cursor-col-resize").first
        if not handle.is_visible():
            pytest.skip("drag handle not visible at this viewport")
        box = handle.bounding_box()
        assert box is not None
        cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        authed_page.mouse.move(cx, cy)
        authed_page.mouse.down()
        authed_page.mouse.move(cx - 220, cy, steps=8)
        authed_page.mouse.up()
        authed_page.wait_for_timeout(200)
        ratio = authed_page.evaluate("localStorage.getItem('sightings-split')")
        assert ratio is not None, "drag did not persist a split ratio (handler not bound?)"
        assert abs(float(ratio) - 0.50) > 0.02, f"split ratio did not move from default: {ratio}"


class TestSightingsMaterialModal:
    def test_chip_opens_modal_and_chrome_x_closes(self, authed_page: Page, base_url: str):
        """MPN chip opens the material card; the global chrome X dismisses it."""
        _open_sightings(authed_page, base_url)
        chip = authed_page.locator("button[aria-label^='View material card']").first
        if chip.count() == 0 or not chip.is_visible():
            pytest.skip("no linked material chip available")
        chip.click()
        modal_content = authed_page.locator("#modal-content")
        expect(modal_content).to_be_visible(timeout=10000)
        authed_page.wait_for_function(
            "document.querySelector('#modal-content')?.children.length > 0",
            timeout=10000,
        )
        close_x = authed_page.locator("button[aria-label='Close']")
        expect(close_x).to_be_visible()
        close_x.click()
        # After close, the modal panel (and its X) is hidden via x-show=open.
        expect(close_x).to_be_hidden(timeout=5000)
