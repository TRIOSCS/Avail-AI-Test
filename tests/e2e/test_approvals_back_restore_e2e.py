"""E2E: instant Back-restore of the Approvals Workspace (nav audit 2026-08-20, F5/F6).

The owner's "no way back" report ended with the workspace restoring on Back via a full
page reload. This proves the fix: after selecting a deal and navigating away, Back
restores the exact tab + selected pane from htmx's history cache INSTANTLY — no document
request, no full reload — with the tab pill highlight and pane content intact (the stale
baked-loader race that used to reset the tab is gone).

Mechanism (browser-verified): select() drives the URL through htmx (replace:), so htmx
snapshots the workspace under the select URL; #ap-hub-body's one-shot load trigger is
stripped after it settles so restore uses the cached content; the tab-pill Alpine state
seeds from the URL so it matches the restored tab.

Skips when the environment has no selectable rows (matches the suite convention).

Called by: pytest tests/e2e (needs the app container running; authed via conftest).
"""

import pytest
from playwright.sync_api import Page

from tests.e2e.test_approvals_workspace_e2e import _open_tab, _select_row


def test_back_restores_workspace_instantly(authed_page: Page, base_url: str):
    page = authed_page

    # Open Purchase Orders (line rows) and select the first line.
    _open_tab(page, base_url, "purchase-orders")
    rows = page.locator("#aw-list [data-row-key^='line-']")
    if rows.count() == 0:
        pytest.skip("no PO line rows in this environment")
    row_key = rows.first.get_attribute("data-row-key")
    _select_row(page, rows.first)
    assert f"select={row_key}" in page.url, f"select URL not set: {page.url}"
    select_url = page.url
    pane_before = page.locator("#aw-pane #aw-pane-body").inner_text()[:120]

    # Navigate away (Sales Hub is a primary nav destination on every screen size).
    page.locator("a[href='/v2/requisitions']").first.click()
    page.wait_for_function("() => !location.pathname.startsWith('/v2/approvals')", timeout=10000)

    # Watch for a document (navigation) request during Back — its absence proves an
    # instant htmx cache restore rather than a full reload.
    doc_requests: list[str] = []
    page.on("request", lambda r: doc_requests.append(r.url) if r.resource_type == "document" else None)

    page.go_back()
    try:
        page.wait_for_function("k => location.search.includes('select=' + k)", arg=row_key, timeout=10000)
    except Exception:
        pass

    assert select_url.split("?", 1)[-1] in page.url, f"Back did not restore select URL: {page.url}"
    assert doc_requests == [], f"Back triggered a full reload (document requests): {doc_requests}"

    # Tab pill highlight matches the restored tab (not reset to Sales Orders).
    active_is_po = page.evaluate(
        "() => { const b = document.querySelector('.bg-accent-600');"
        " return !!b && b.textContent.includes('Purchase Orders'); }"
    )
    assert active_is_po, "active tab pill reset instead of restoring Purchase Orders"

    # Body rows + the selected pane are intact (no stale-loader wipe).
    assert page.locator("#aw-list [data-row-key^='line-']").count() > 0, "PO rows wiped after Back"
    pane_after = page.locator("#aw-pane #aw-pane-body").inner_text()[:120]
    assert pane_after[:60] == pane_before[:60], "selected pane not restored (reset to a different deal)"
