"""Tests for Requisitions 2 template rendering correctness.

Verifies HTMX attributes, Alpine.js bindings, DOM structure,
and conditional rendering in all 6 templates.

Called by: pytest
Depends on: app/routers/requisitions2.py, conftest fixtures
"""

from datetime import datetime, timezone


# ── HTMX attributes on page shell ────────────────────────────────────


def test_page_includes_htmx_script(client):
    """Page shell loads HTMX library."""
    resp = client.get("/requisitions2")
    assert "htmx.org" in resp.text


def test_page_includes_alpine_script(client):
    """Page shell loads Alpine.js library."""
    resp = client.get("/requisitions2")
    assert "alpinejs" in resp.text


def test_page_includes_requisitions2_js(client):
    """Page shell loads the local Alpine component."""
    resp = client.get("/requisitions2")
    assert "requisitions2.js" in resp.text


def test_page_has_alpine_x_data(client):
    """Page shell initializes Alpine.js component."""
    resp = client.get("/requisitions2")
    assert 'x-data="rq2Page()"' in resp.text


# ── Filter form HTMX attributes ─────────────────────────────────────


def test_filter_form_htmx_get(client):
    """Filter form has hx-get pointing to /requisitions2/table."""
    resp = client.get("/requisitions2")
    assert 'hx-get="/requisitions2/table"' in resp.text


def test_filter_form_htmx_target(client):
    """Filter form targets #rq2-table."""
    resp = client.get("/requisitions2")
    assert 'hx-target="#rq2-table"' in resp.text


def test_filter_form_htmx_trigger(client):
    """Filter form has change trigger and debounced keyup."""
    resp = client.get("/requisitions2")
    assert "hx-trigger" in resp.text
    assert "delay:300ms" in resp.text


def test_filter_form_includes_search_input(client):
    """Filter form has search input."""
    resp = client.get("/requisitions2")
    assert 'id="rq2-search"' in resp.text
    assert 'name="q"' in resp.text


def test_filter_form_includes_status_select(client):
    """Filter form has status dropdown with all options."""
    resp = client.get("/requisitions2")
    for status in ["all", "active", "draft", "sourcing", "archived", "won", "lost"]:
        assert f'value="{status}"' in resp.text


def test_filter_form_includes_urgency_select(client):
    """Filter form has urgency dropdown."""
    resp = client.get("/requisitions2")
    for urg in ["normal", "hot", "critical"]:
        assert f'value="{urg}"' in resp.text


def test_filter_form_includes_hidden_sort_fields(client):
    """Filter form has hidden fields for sort state."""
    resp = client.get("/requisitions2")
    assert 'name="sort"' in resp.text
    assert 'name="order"' in resp.text
    assert 'name="page"' in resp.text
    assert 'name="per_page"' in resp.text


# ── Table structure ──────────────────────────────────────────────────


def test_table_has_sortable_headers(client, test_requisition):
    """Table headers have hx-get for sorting."""
    resp = client.get("/requisitions2/table", params={"status": "all"})
    assert "hx-get" in resp.text
    assert "sort=" in resp.text


def test_table_has_select_all_checkbox(client, test_requisition):
    """Table has a select-all checkbox in the header."""
    resp = client.get("/requisitions2/table", params={"status": "all"})
    assert "toggleAll" in resp.text


def test_table_row_has_checkbox(client, test_requisition):
    """Each row has a selection checkbox with Alpine binding."""
    resp = client.get("/requisitions2/table", params={"status": "all"})
    assert "toggleSelection" in resp.text
    assert "x-bind:checked" in resp.text


def test_table_row_has_modal_link(client, test_requisition):
    """Requisition name links to modal via HTMX."""
    resp = client.get("/requisitions2/table", params={"status": "all"})
    assert f'hx-get="/requisitions2/{test_requisition.id}/modal"' in resp.text
    assert 'hx-target="#rq2-modal"' in resp.text


def test_table_row_has_action_buttons(client, test_requisition, db_session):
    """Table rows have action buttons with hx-post."""
    test_requisition.status = "active"
    db_session.commit()

    resp = client.get("/requisitions2/table", params={"status": "active"})
    assert f'hx-post="/requisitions2/{test_requisition.id}/action/archive"' in resp.text


def test_table_row_shows_status_badge(client, test_requisition, db_session):
    """Table rows display status as a badge."""
    test_requisition.status = "active"
    db_session.commit()

    resp = client.get("/requisitions2/table", params={"status": "active"})
    assert "badge-active" in resp.text


def test_table_row_shows_urgency_badge(client, test_requisition, db_session):
    """Critical urgency shows a badge."""
    test_requisition.status = "active"
    test_requisition.urgency = "critical"
    db_session.commit()

    resp = client.get("/requisitions2/table", params={"status": "active"})
    assert "badge-critical" in resp.text
    assert "rq2-urgent" in resp.text


def test_table_archived_shows_activate_button(client, test_requisition, db_session):
    """Archived rows show Activate button instead of Archive."""
    test_requisition.status = "archived"
    db_session.commit()

    resp = client.get("/requisitions2/table", params={"status": "archived"})
    assert "action/activate" in resp.text


def test_table_shows_claim_for_buyer(client, test_requisition, db_session):
    """Unclaimed rows show Claim button for buyer users."""
    test_requisition.status = "active"
    test_requisition.claimed_by_id = None
    db_session.commit()

    resp = client.get("/requisitions2/table", params={"status": "active"})
    assert "action/claim" in resp.text


def test_table_shows_unclaim_for_claimer(client, test_requisition, test_user, db_session):
    """Rows claimed by current user show Unclaim button."""
    test_requisition.status = "active"
    test_requisition.claimed_by_id = test_user.id
    db_session.commit()

    resp = client.get("/requisitions2/table", params={"status": "active"})
    assert "action/unclaim" in resp.text


def test_empty_table_shows_message(client):
    """Empty table shows 'No requisitions found' message."""
    resp = client.get("/requisitions2/table", params={"status": "active"})
    assert "No requisitions found" in resp.text


# ── Bulk bar ─────────────────────────────────────────────────────────


def test_bulk_bar_has_alpine_show(client):
    """Bulk bar uses x-show for conditional display."""
    resp = client.get("/requisitions2")
    assert "x-show" in resp.text
    assert "selectedIds.size" in resp.text


def test_bulk_bar_has_archive_form(client):
    """Bulk bar has archive form with hx-post."""
    resp = client.get("/requisitions2")
    assert 'hx-post="/requisitions2/bulk/archive"' in resp.text


def test_bulk_bar_has_activate_form(client):
    """Bulk bar has activate form."""
    resp = client.get("/requisitions2")
    assert 'hx-post="/requisitions2/bulk/activate"' in resp.text


def test_bulk_bar_has_ids_binding(client):
    """Bulk bar forms bind ids from Alpine selectedIds."""
    resp = client.get("/requisitions2")
    assert "getSelectedIdsString()" in resp.text


# ── Modal structure ──────────────────────────────────────────────────


def test_modal_has_close_button(client, test_requisition):
    """Modal has close button with Alpine binding."""
    resp = client.get(f"/requisitions2/{test_requisition.id}/modal")
    assert "btn-close" in resp.text
    assert "x-on:click" in resp.text


def test_modal_has_escape_handler(client, test_requisition):
    """Modal closes on Escape key."""
    resp = client.get(f"/requisitions2/{test_requisition.id}/modal")
    assert "keydown.escape" in resp.text


def test_modal_has_backdrop_close(client, test_requisition):
    """Modal closes on backdrop click."""
    resp = client.get(f"/requisitions2/{test_requisition.id}/modal")
    assert "x-on:click.self" in resp.text


def test_modal_shows_requirements_table(client, test_requisition):
    """Modal includes requirements table with MPN."""
    resp = client.get(f"/requisitions2/{test_requisition.id}/modal")
    assert "LM317T" in resp.text
    assert "1000" in resp.text


def test_modal_shows_requisition_fields(client, test_requisition):
    """Modal shows key requisition fields."""
    resp = client.get(f"/requisitions2/{test_requisition.id}/modal")
    assert "Customer" in resp.text
    assert "Owner" in resp.text
    assert "Urgency" in resp.text
    assert "Deadline" in resp.text


# ── Pagination controls ──────────────────────────────────────────────


def test_pagination_prev_link(client, db_session, test_user):
    """Page 2 shows Prev link."""
    from app.models import Requisition
    for i in range(30):
        db_session.add(Requisition(
            name=f"PAG-{i:03d}", status="active",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        ))
    db_session.commit()

    resp = client.get("/requisitions2/table", params={"status": "active", "per_page": "10", "page": "2"})
    assert "Prev" in resp.text


def test_pagination_no_controls_for_single_page(client, test_requisition):
    """Single page of results shows no pagination controls."""
    resp = client.get("/requisitions2/table", params={"status": "all"})
    # With just 1 requisition and 25 per page, no Next link
    assert "Next" not in resp.text


# ── Owner filter visibility ──────────────────────────────────────────


def test_buyer_sees_owner_filter(client):
    """Buyer role sees the owner filter dropdown."""
    resp = client.get("/requisitions2")
    assert "All owners" in resp.text


def test_sales_user_no_owner_filter(client, db_session, sales_user):
    """Sales role does NOT see owner filter."""
    from app.dependencies import require_user
    from app.main import app

    app.dependency_overrides[require_user] = lambda: sales_user
    try:
        resp = client.get("/requisitions2")
        assert "All owners" not in resp.text
    finally:
        from tests.conftest import TestSessionLocal
        test_user_row = db_session.query(
            __import__("app.models", fromlist=["User"]).User
        ).filter_by(role="buyer").first()
        if test_user_row:
            app.dependency_overrides[require_user] = lambda: test_user_row
