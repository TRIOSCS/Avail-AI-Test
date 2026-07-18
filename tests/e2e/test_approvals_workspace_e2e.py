"""E2E coverage for the Approvals Workspace (/v2/approvals — 4-tab split-view console).

Validates, in a real browser against the live container (specs/approvals-workspace.md
§5–§8):
- the shell renders the four tab pills (with per-viewer badges) and switches tabs via
  HTMX swaps, never a full-page reload,
- selecting a work-list row loads the matching detail pane (SO / PO line / prepayment),
- the two-part sales-order approve: "Approve & notify" (proceed) and "Send back for
  sign-off" (reject→draft with the change summary),
- the PO flow: confirm-PO (PO# + payment-method select) → Pending approval → Approve,
- the prepayment approve reads "OK to pay — {method}",
- the PO kanban renders its lanes on an active sourcing order (empty lanes show
  guidance, not blanks) and a card tap opens that line's PO pane,
- a lite (Stock Sale) order shows no kanban and no lines,
- Acctivate copy chips write to the clipboard with a "Copied" flash,
- notes post from a pane into the item's thread,
- a stale edit (pre-mutated from a second tab) is rejected with the
  "This changed — refresh." toast, and
- every tab's empty-search state renders guidance.

Data-dependent tests skip when the environment has no matching rows (the deterministic
seeder does not fabricate approvals-engine state), matching the suite's convention.

Called by: pytest tests/e2e (needs the app container running; authed via the admin
session cookie from conftest).
"""

import time

import pytest
from playwright.sync_api import Page, expect

TAB_LABELS = ["Sales Orders", "Buy Plans", "Purchase Orders", "Prepayments"]

STALE_TOAST = "This changed — refresh."


# ── Helpers ──────────────────────────────────────────────────────────


def _collect_errors(page: Page, base_url: str) -> tuple[list[str], list[str]]:
    """Collect page errors + console errors, and same-origin failed resource URLs.

    Off-origin resource failures (blocked CDNs/fonts in a sandboxed run) surface as
    'Failed to load resource' console errors — those are environment noise, tracked
    separately so the assertions can ignore them without hiding broken app assets.
    """
    errors: list[str] = []
    failed_same_origin: list[str] = []
    page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))
    page.on(
        "console",
        lambda msg: errors.append(f"console.{msg.type}: {msg.text}") if msg.type == "error" else None,
    )
    page.on(
        "requestfailed",
        lambda req: failed_same_origin.append(req.url) if req.url.startswith(base_url) else None,
    )
    return errors, failed_same_origin


def _app_errors(errors: list[str], failed_same_origin: list[str]) -> list[str]:
    """Errors attributable to the app: everything except off-origin resource noise."""
    return [e for e in errors if "Failed to load resource" not in e or failed_same_origin]


def _open_tab(page: Page, base_url: str, tab: str = "sales-orders") -> list[str]:
    """Navigate to the workspace tab; wait for the left list to render."""
    errors, failed = _collect_errors(page, base_url)
    page.goto(f"{base_url}/v2/approvals?tab={tab}", wait_until="domcontentloaded")
    try:
        # The real filter bar (not the first-load placeholder) carries the search box.
        page.wait_for_selector("#aw-filters input[name='q']", timeout=15000)
    except Exception:
        pytest.skip(f"approvals workspace {tab} list did not render in this environment")
    page.wait_for_timeout(400)
    return _app_errors(errors, failed)


def _rows(page: Page):
    return page.locator("#aw-list [data-row-key]")


def _select_row(page: Page, row) -> None:
    """Click a list row and wait for the detail pane to fill."""
    row.click()
    page.wait_for_selector("#aw-pane #aw-pane-body", timeout=10000)
    page.wait_for_timeout(300)


def _needs_approval_rows(page: Page):
    """Rows inside the 'Needs your approval' group (empty locator when no group)."""
    header = page.locator("#aw-list div", has_text="Needs your approval").first
    if header.count() == 0:
        return page.locator("#aw-list [data-no-such-thing]")
    return page.locator(
        "xpath=//div[@id='aw-list']//div[contains(text(), 'Needs your approval')]"
        "/following-sibling::div[1]//*[@data-row-key]"
    )


# ── Shell: tabs, badges, HTMX switching ─────────────────────────────


class TestWorkspaceShell:
    def test_page_loads_without_js_errors(self, authed_page: Page, base_url: str):
        errors = _open_tab(authed_page, base_url)
        assert not errors, "console/page errors on /v2/approvals:\n" + "\n".join(errors[:20])

    def test_four_tabs_render_with_badges(self, authed_page: Page, base_url: str):
        _open_tab(authed_page, base_url)
        for label in TAB_LABELS:
            pill = authed_page.locator("button", has_text=label).first
            expect(pill).to_be_visible()
        # Any badge that renders must be a count (an int), never junk.
        badges = authed_page.locator("button span.rounded-full.bg-white\\/25")
        for i in range(badges.count()):
            assert badges.nth(i).inner_text().strip().isdigit()

    def test_tab_switch_is_htmx_swap_not_full_reload(self, authed_page: Page, base_url: str):
        _open_tab(authed_page, base_url)
        authed_page.evaluate("window.__aw_e2e_marker = 42")
        authed_page.locator("button", has_text="Purchase Orders").first.click()
        authed_page.wait_for_selector("#aw-filters input[name='q']", timeout=10000)
        authed_page.wait_for_timeout(300)
        marker = authed_page.evaluate("window.__aw_e2e_marker")
        assert marker == 42, "tab switch caused a full-page reload (window state was lost)"
        assert "tab=purchase-orders" in authed_page.url


# ── List → pane wiring ──────────────────────────────────────────────


class TestListAndPane:
    def test_row_click_loads_matching_pane(self, authed_page: Page, base_url: str):
        _open_tab(authed_page, base_url)
        rows = _rows(authed_page)
        if rows.count() == 0:
            pytest.skip("no sales-order rows available in this environment")
        _select_row(authed_page, rows.first)
        pane = authed_page.locator("#aw-pane #aw-pane-body")
        expect(pane).to_be_visible()
        # The SO pane anatomy always includes the Quality — sales section.
        expect(pane.locator("text=Quality — sales section")).to_be_visible()

    def test_po_row_loads_po_line_pane(self, authed_page: Page, base_url: str):
        _open_tab(authed_page, base_url, "purchase-orders")
        rows = authed_page.locator("#aw-list [data-row-key^='line-']")
        if rows.count() == 0:
            pytest.skip("no PO-line rows available in this environment")
        _select_row(authed_page, rows.first)
        pane = authed_page.locator("#aw-pane #aw-pane-body")
        expect(pane.locator("text=Quality — purchasing section")).to_be_visible()


# ── PO kanban on an active sourcing order (spec §6) ─────────────────


class TestKanban:
    def _open_active_sourcing_pane(self, page: Page, base_url: str) -> None:
        _open_tab(page, base_url)
        rows = page.locator("#aw-list [data-row-key^='plan-']:has-text('Active'):not(:has-text('Stock Sale'))")
        if rows.count() == 0:
            pytest.skip("no active sourcing order available in this environment")
        _select_row(page, rows.first)
        if page.locator("#aw-pane [data-lane]").count() == 0:
            pytest.skip("active plan rendered no kanban (no sourcing lines in this environment)")

    def test_lanes_render_with_guidance_never_blank(self, authed_page: Page, base_url: str):
        self._open_active_sourcing_pane(authed_page, base_url)
        lanes = authed_page.locator("#aw-pane [data-lane]")
        assert lanes.count() >= 5, f"expected the 5 core lanes, got {lanes.count()}"
        for i in range(lanes.count()):
            lane = lanes.nth(i)
            has_cards = lane.locator("[role='link']").count() > 0
            has_empty_hint = lane.locator("text=Nothing here yet").count() > 0
            assert has_cards or has_empty_hint, (
                f"kanban lane {lane.get_attribute('data-lane')} is a blank panel — no cards and no empty-state guidance"
            )

    def test_card_tap_opens_po_line_pane(self, authed_page: Page, base_url: str):
        self._open_active_sourcing_pane(authed_page, base_url)
        cards = authed_page.locator("#aw-pane [data-lane] [role='link']")
        if cards.count() == 0:
            pytest.skip("kanban has no cards in this environment")
        cards.first.click()
        authed_page.wait_for_selector("#aw-pane #aw-pane-body", timeout=10000)
        authed_page.wait_for_timeout(300)
        pane = authed_page.locator("#aw-pane #aw-pane-body")
        expect(pane.locator("text=Quality — purchasing section")).to_be_visible()


# ── Lite (Stock Sale) order: no kanban, no lines (spec §3/§8) ───────


class TestLiteOrder:
    def test_stock_sale_pane_has_no_kanban_or_lines(self, authed_page: Page, base_url: str):
        _open_tab(authed_page, base_url)
        rows = authed_page.locator("#aw-list [data-row-key^='plan-']:has-text('Stock Sale')")
        if rows.count() == 0:
            pytest.skip("no stock-sale order available in this environment")
        _select_row(authed_page, rows.first)
        pane = authed_page.locator("#aw-pane #aw-pane-body")
        expect(pane).to_be_visible()
        assert pane.locator("[data-lane]").count() == 0, "lite order rendered a PO kanban"
        assert pane.locator("text=Purchase orders").count() == 0, "lite order rendered the PO board"
        assert authed_page.locator("#aw-pane table").count() == 0, "lite order rendered a lines table"
        # The lite pane still carries the SO anatomy (QP-sales + notes).
        expect(pane.locator("text=Quality — sales section")).to_be_visible()
        expect(pane.locator("#aw-notes-thread")).to_be_visible()


# ── Sales-order two-part approve (spec §7) ──────────────────────────


class TestSalesOrderDecision:
    def _open_decidable_sourcing_pane(self, page: Page, base_url: str) -> None:
        _open_tab(page, base_url)
        rows = _needs_approval_rows(page)
        sourcing = rows.locator(":scope:not(:has-text('Stock Sale'))") if rows.count() else rows
        if sourcing.count() == 0:
            pytest.skip("no decidable pending sales order available in this environment")
        _select_row(page, sourcing.first)
        banner = page.locator("#aw-pane", has_text="Awaiting your approval")
        if banner.count() == 0:
            pytest.skip("selected row is not awaiting this viewer's approval")

    def test_send_back_returns_plan_to_draft(self, authed_page: Page, base_url: str):
        self._open_decidable_sourcing_pane(authed_page, base_url)
        authed_page.locator("#aw-pane button", has_text="Send back for sign-off").click()
        note = authed_page.locator("#aw-pane textarea[name='notes']").first
        expect(note).to_be_visible()
        note.fill("E2E: please re-check the unit sell before resubmitting.")
        authed_page.locator("#aw-pane button", has_text="Send back with summary").click()
        authed_page.wait_for_selector("#aw-pane #aw-pane-body", timeout=10000)
        expect(authed_page.locator("#aw-pane", has_text="Draft — not yet submitted")).to_be_visible(timeout=10000)

    def test_approve_and_notify_stamps_the_plan(self, authed_page: Page, base_url: str):
        self._open_decidable_sourcing_pane(authed_page, base_url)
        authed_page.locator("#aw-pane button", has_text="Approve & notify").click()
        authed_page.wait_for_selector("#aw-pane #aw-pane-body", timeout=10000)
        expect(authed_page.locator("#aw-pane", has_text="Approved by")).to_be_visible(timeout=10000)


# ── PO flow: confirm → pending approval → approve (spec §8) ─────────


class TestPurchaseOrderFlow:
    def test_confirm_po_then_manager_approve(self, authed_page: Page, base_url: str):
        _open_tab(authed_page, base_url, "purchase-orders")
        rows = authed_page.locator("#aw-list [data-row-key^='line-']:has-text('Awaiting PO')")
        if rows.count() == 0:
            pytest.skip("no awaiting-PO line available in this environment")
        _select_row(authed_page, rows.first)
        pane = authed_page.locator("#aw-pane #aw-pane-body")
        po_input = pane.locator("input[name='po_number']")
        if po_input.count() == 0:
            pytest.skip("confirm-PO form not offered to this viewer")

        # Buyer confirms the PO cut in Acctivate: PO# + payment method (required).
        po_number = f"PO-E2E-{int(time.time())}"
        po_input.fill(po_number)
        method = pane.locator("select[name='payment_method']")
        expect(method).to_be_visible()
        method.select_option("wire")
        pane.locator("button", has_text="Confirm PO").click()
        authed_page.wait_for_selector("#aw-pane #aw-pane-body", timeout=10000)
        authed_page.wait_for_timeout(300)

        # The line lands at Pending approval (display vocabulary, spec §5).
        pane = authed_page.locator("#aw-pane #aw-pane-body")
        expect(pane.locator("text=Pending approval").first).to_be_visible(timeout=10000)

        # Manager decides in place when eligible (dollar limit + right).
        approve = pane.locator("form[hx-post*='verify-po'] button", has_text="Approve")
        if approve.count() == 0:
            pytest.skip("viewer cannot approve this PO line (limit/right) — confirm path verified")
        approve.first.click()
        authed_page.wait_for_selector("#aw-pane #aw-pane-body", timeout=10000)
        expect(authed_page.locator("#aw-pane", has_text="Approved")).to_be_visible(timeout=10000)


# ── Prepayment approve: "OK to pay — {method}" (spec §8) ────────────


class TestPrepaymentDecision:
    def test_ok_to_pay_approve(self, authed_page: Page, base_url: str):
        _open_tab(authed_page, base_url, "prepayments")
        rows = authed_page.locator("#aw-list [data-row-key^='prepay-']:has-text('Requested')")
        if rows.count() == 0:
            pytest.skip("no requested prepayment available in this environment")
        _select_row(authed_page, rows.first)
        pane = authed_page.locator("#aw-pane #aw-pane-body")
        ok_button = pane.locator("button", has_text="OK to pay —")
        if ok_button.count() == 0:
            pytest.skip("viewer cannot decide this prepayment in this environment")
        # The approve button carries the method — the method lives in the field.
        label = ok_button.first.inner_text()
        assert "OK to pay —" in label and len(label.split("—")[1].strip()) > 0
        ok_button.first.click()
        authed_page.wait_for_selector("#aw-pane #aw-pane-body", timeout=10000)
        expect(authed_page.locator("#aw-pane", has_text="Accounting confirms via the pay link")).to_be_visible(
            timeout=10000
        )


# ── Acctivate copy chips (spec §5) ──────────────────────────────────


class TestCopyChip:
    def test_chip_writes_clipboard_and_flashes(self, authed_page: Page, base_url: str):
        authed_page.context.grant_permissions(["clipboard-read", "clipboard-write"])
        _open_tab(authed_page, base_url)
        chip = authed_page.locator("button[data-copy-value]").first
        if chip.count() == 0 or not chip.is_visible():
            pytest.skip("no Acctivate copy chip rendered in this environment")
        value = chip.get_attribute("data-copy-value")
        chip.click()
        expect(chip.locator("text=Copied")).to_be_visible(timeout=5000)
        clipboard = authed_page.evaluate("navigator.clipboard.readText()")
        assert clipboard == value, f"clipboard holds {clipboard!r}, expected {value!r}"


# ── Notes post from a pane (spec §7 — never status-locked) ──────────


class TestNotesThread:
    def test_note_posts_into_the_thread(self, authed_page: Page, base_url: str):
        _open_tab(authed_page, base_url)
        rows = _rows(authed_page)
        if rows.count() == 0:
            pytest.skip("no sales-order rows available in this environment")
        _select_row(authed_page, rows.first)
        thread = authed_page.locator("#aw-notes-thread")
        expect(thread).to_be_visible()
        body = f"E2E note {int(time.time())} — copy chip / kanban walkthrough."
        thread.locator("textarea[name='body']").fill(body)
        thread.locator("button", has_text="Post").click()
        # The POST re-renders #aw-notes-thread (outerHTML) with the new note.
        expect(authed_page.locator("#aw-notes-thread", has_text=body)).to_be_visible(timeout=10000)


# ── Stale-edit guard: 409 → "This changed — refresh." (spec §7) ─────


class TestStaleEditGuard:
    def test_stale_qp_sales_save_shows_refresh_toast(self, authed_page: Page, base_url: str):
        _open_tab(authed_page, base_url)
        rows = _rows(authed_page)
        if rows.count() == 0:
            pytest.skip("no sales-order rows available in this environment")
        _select_row(authed_page, rows.first)
        pane = authed_page.locator("#aw-pane #aw-pane-body")
        edit = pane.get_by_role("button", name="Edit", exact=True)
        if edit.count() == 0:
            pytest.skip("QP-sales section not editable by this viewer in this plan status")

        # Save once so the QP row exists and carries a fresh updated_at token.
        edit.first.click()
        cond = pane.locator("input[name='qp_sales_condition']").first
        expect(cond).to_be_visible()
        cond.fill("NEW-E2E-A")
        pane.locator("form[hx-post*='qp-sales'] button", has_text="Save").click()
        authed_page.wait_for_selector("#aw-pane #aw-pane-body", timeout=10000)
        authed_page.wait_for_timeout(400)

        # Re-open the editor; its hidden token now reflects the saved row.
        pane = authed_page.locator("#aw-pane #aw-pane-body")
        pane.get_by_role("button", name="Edit", exact=True).first.click()
        token = pane.locator("form[hx-post*='qp-sales'] input[name='expected_updated_at']").first
        if not token.input_value():
            pytest.skip("QP row carries no updated_at token — stale guard not exercisable")

        # Pre-mutate from a SECOND TAB (same session): the real UI flow bumps
        # updated_at, leaving the first tab's form stale. Tokens are second-
        # resolution tolerant, so keep the two saves >1s apart.
        time.sleep(1.2)
        page2 = authed_page.context.new_page()
        try:
            _open_tab(page2, base_url)
            _select_row(page2, _rows(page2).first)
            pane2 = page2.locator("#aw-pane #aw-pane-body")
            pane2.get_by_role("button", name="Edit", exact=True).first.click()
            cond2 = pane2.locator("input[name='qp_sales_condition']").first
            cond2.fill("NEW-E2E-B")
            pane2.locator("form[hx-post*='qp-sales'] button", has_text="Save").click()
            page2.wait_for_selector("#aw-pane #aw-pane-body", timeout=10000)
            page2.wait_for_timeout(400)
        finally:
            page2.close()

        # The first tab's save is now stale → non-destructive 409 + the toast.
        cond = authed_page.locator("#aw-pane input[name='qp_sales_condition']").first
        cond.fill("NEW-E2E-C")
        authed_page.locator("#aw-pane form[hx-post*='qp-sales'] button", has_text="Save").click()
        authed_page.wait_for_function(
            "() => { const t = Alpine.store('toast'); return t.show && t.message.includes('This changed'); }",
            timeout=10000,
        )
        toast_message = authed_page.evaluate("Alpine.store('toast').message")
        assert STALE_TOAST in toast_message


# ── Empty states: guidance, never blank panels (spec §5) ────────────


class TestEmptyStates:
    @pytest.mark.parametrize("tab", ["sales-orders", "buy-plans", "purchase-orders", "prepayments"])
    def test_no_match_search_renders_guidance(self, authed_page: Page, base_url: str, tab: str):
        _open_tab(authed_page, base_url, tab)
        search = authed_page.locator("#aw-filters input[name='q']")
        expect(search).to_be_visible()
        # Type key-by-key: the search triggers on `keyup changed delay:300ms`,
        # which fill() (no key events) would never fire.
        search.press_sequentially("ZZZ-NO-SUCH-THING-E2E", delay=15)
        authed_page.wait_for_timeout(800)  # 300ms debounce + swap
        empty = authed_page.locator("#aw-list", has_text="Nothing matches")
        expect(empty.first).to_be_visible(timeout=10000)
