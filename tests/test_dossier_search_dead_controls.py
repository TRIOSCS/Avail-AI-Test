"""Regression tests for dead-control / dead-end bugs on the Search report.

Each bug was a silent no-op at runtime (a selection bar that never rendered, a toggle
payload keyed on the wrong field, an empty state with no way out), so these tests render
the actual templates through the configured Jinja env and assert the corrected structure.

Tests: app/templates/htmx/partials/search/{report.html, report_live.html, _live_row.html,
       lead_detail.html, requisition_picker_modal.html}
Depends on: app/template_env.py (configured Jinja env with custom filters + globals)
"""

from app.template_env import templates


def _render(rel: str, **ctx) -> str:
    return templates.env.get_template(rel).render(**ctx)


def test_report_shell_carries_the_selection_bar():
    # Bug 1: a cached/repeat market used to render without the selection bar, so ticking a
    # row did nothing visible. The bar now lives once in the report shell (shared by cached
    # and streamed rows through the one Alpine $store.shortlist) and offers the actions.
    html = _render("htmx/partials/search/report.html", mpn="ABC123", subs=[], subs_text="", qs="mpn=ABC123")
    assert "$store.shortlist.count > 0" in html
    assert "Send RFQ" in html
    assert "Add offer" in html
    assert "Add to requisition" in html


def test_cached_live_rows_bind_the_same_selection_store():
    html = _render(
        "htmx/partials/search/report_live.html",
        mpn="ABC123",
        qs="mpn=ABC123",
        runs=[
            {
                "display": "ABC123",
                "cached_search_id": "sid-1",
                "cached_rows": [{"vendor_name": "Acme Corp", "mpn_matched": "ABC123", "confidence_pct": 90}],
            }
        ],
        live_runs=0,
        cached_ids=["sid-1"],
        market_health=None,
        market_baseline=None,
    )
    assert "$store.shortlist.toggle(" in html
    assert 'id="live-sid-1-acme-corp"' in html


def test_lead_detail_toggle_payload_uses_mpn_key():
    # Bug 2: the drawer's select toggle sent `mpn_matched:` but the store keys on item.mpn,
    # so it stored "Vendor:undefined" and never matched the row checkbox. The payload now
    # reads data-mpn (the matched part) and sends it as `mpn:`.
    html = _render(
        "htmx/partials/search/lead_detail.html",
        lead={"vendor_name": "Acme Corp", "mpn_matched": "ABC123"},
        mpn="ABC123",
    )
    assert 'data-mpn="ABC123"' in html
    assert "mpn: $el.dataset.mpn" in html
    assert "mpn_matched: '" not in html


def test_requisition_picker_empty_state_has_create_affordance():
    # Bug 3: the empty state was a dead end ("Create one first." with no control). It must
    # offer a control that launches the existing create-requisition flow.
    html = _render(
        "htmx/partials/search/requisition_picker_modal.html",
        requisitions=[],
        mpn="ABC123",
        items_json="[]",
    )
    assert "/v2/partials/requisitions/create-form" in html
    assert "New requisition" in html
    # Uses the shared global-modal dispatch, not a dead link.
    assert "$dispatch('open-modal'" in html
