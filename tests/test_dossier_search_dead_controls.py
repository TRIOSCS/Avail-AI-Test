"""Regression tests for three dead-control / dead-end bugs on the part-dossier / search
surface (UX audit).

Each bug was a silent no-op at runtime (a shortlist bar that never rendered, a toggle
payload keyed on the wrong field, an empty state with no way out), so these tests render
the actual templates through the configured Jinja env and assert the corrected structure.

Tests: app/templates/htmx/partials/search/{dossier_market.html, lead_detail.html,
       requisition_picker_modal.html}
Depends on: app/template_env.py (configured Jinja env with custom filters + globals)
"""

from app.template_env import templates


def _render(rel: str, **ctx) -> str:
    return templates.env.get_template(rel).render(**ctx)


def test_cache_hit_dossier_market_includes_shortlist_bar():
    # Bug 1: on a cached/repeat market the sticky shortlist bar was absent, so ticking a
    # cached row checkbox did nothing visible. The cache-hit branch must now render the
    # SAME shortlist bar (backed by the SAME Alpine $store.shortlist) as a fresh search.
    html = _render(
        "htmx/partials/search/dossier_market.html",
        mpn="ABC123",
        cached_search_id="sid-1",
        cached_rows=[{"vendor_name": "Acme Corp", "mpn_matched": "ABC123", "confidence_pct": 90}],
        market_health=None,
    )
    # Shortlist bar markers (from shortlist_bar.html).
    assert "$store.shortlist.count > 0" in html
    assert "Add to Requisition" in html
    assert "Create RFQ" in html
    # The cached row checkbox binds to the same store.
    assert "$store.shortlist.toggle(" in html


def test_lead_detail_toggle_payload_uses_mpn_key():
    # Bug 2: the drawer's Add-to-Shortlist toggle sent `mpn_matched:` but the store keys on
    # item.mpn, so it stored "Vendor:undefined" and never matched the row checkbox. The
    # payload must send `mpn:` to align with the row checkboxes + the store.
    html = _render(
        "htmx/partials/search/lead_detail.html",
        lead={"vendor_name": "Acme Corp", "mpn_matched": "ABC123"},
        mpn="ABC123",
        positive_signals=[],
        caution_signals=[],
    )
    assert "mpn: 'ABC123'" in html
    # The wrong key must be gone from the toggle payload (as a JS object key).
    assert "mpn_matched: '" not in html


def test_requisition_picker_empty_state_has_create_affordance():
    # Bug 3: the empty state was a dead end ("Create one first." with no control). It must
    # now offer a control that launches the existing create-requisition flow.
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
