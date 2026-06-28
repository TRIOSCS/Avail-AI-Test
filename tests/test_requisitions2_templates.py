"""Tests for Requisitions 2 template rendering correctness.

Verifies HTMX attributes, Alpine.js bindings, DOM structure,
and conditional rendering in all 6 templates.

Called by: pytest
Depends on: app/routers/requisitions2.py, conftest fixtures
"""

from datetime import datetime, timezone

import pytest

# ── Page shell: substrings rendered on GET /requisitions2 ────────────


@pytest.mark.parametrize(
    "snippet",
    [
        pytest.param("htmx.org", id="htmx-script"),
        pytest.param("alpinejs", id="alpine-script"),
        pytest.param("requisitions2.js", id="local-component"),
        pytest.param('x-data="rq2Page()"', id="alpine-x-data"),
        pytest.param('hx-get="/requisitions2/table"', id="filter-form-hx-get"),
        pytest.param('hx-target="#rq2-table"', id="filter-form-hx-target"),
        pytest.param('id="rq2-search"', id="search-input-id"),
        pytest.param('name="q"', id="search-input-name"),
        pytest.param('name="sort"', id="hidden-sort"),
        pytest.param('name="order"', id="hidden-order"),
        pytest.param('name="page"', id="hidden-page"),
        pytest.param('name="per_page"', id="hidden-per-page"),
        pytest.param("x-show", id="bulk-bar-x-show"),
        pytest.param("selectedIds.size", id="bulk-bar-selected-size"),
        pytest.param("x-collapse", id="bulk-bar-collapse"),
        pytest.param("response-targets", id="ext-response-targets"),
        pytest.param("loading-states", id="ext-loading-states"),
        pytest.param("preload", id="ext-preload"),
        pytest.param("@alpinejs/focus", id="plugin-focus"),
        pytest.param("@alpinejs/collapse", id="plugin-collapse"),
        pytest.param("@alpinejs/persist", id="plugin-persist"),
        pytest.param('id="rq2-error"', id="error-target"),
        pytest.param('hx-target-error="#rq2-error"', id="filter-form-error-target"),
        pytest.param("htmx-indicator", id="filter-form-indicator"),
        pytest.param("htmx-settling", id="css-swap-settling"),
        pytest.param("htmx-added", id="css-swap-added"),
        pytest.param("rq2-editable", id="inline-edit-css-editable"),
        pytest.param("rq2-inline-input", id="inline-edit-css-input"),
        pytest.param("rq2-inline-select", id="inline-edit-css-select"),
        pytest.param("htmx-ext-sse", id="sse-extension"),
        pytest.param('sse-connect="/requisitions2/stream"', id="sse-source-element"),
        pytest.param("sse:table-refresh", id="sse-table-refresh"),
        pytest.param("animate-pulse", id="live-indicator"),
        pytest.param("All owners", id="buyer-owner-filter"),
    ],
)
def test_page_shell_contains(client, snippet):
    """Page shell renders the expected HTMX/Alpine/SSE markers."""
    resp = client.get("/requisitions2")
    assert snippet in resp.text


def test_filter_form_htmx_trigger(client):
    """Filter form has change trigger and debounced keyup."""
    resp = client.get("/requisitions2")
    assert "hx-trigger" in resp.text
    assert "delay:300ms" in resp.text


@pytest.mark.parametrize(
    "status",
    ["all", "open", "rfqs_sent", "offers", "quoted", "hotlist", "won", "lost"],
)
def test_filter_form_includes_status_option(client, status):
    """Filter form has status dropdown with all new pipeline options."""
    resp = client.get("/requisitions2")
    assert f'value="{status}"' in resp.text


def test_filter_form_drops_legacy_status_options(client):
    """Legacy pipeline values (active/sourcing) are gone from the status filter."""
    resp = client.get("/requisitions2")
    assert 'value="sourcing"' not in resp.text
    assert 'value="active"' not in resp.text


@pytest.mark.parametrize("urg", ["normal", "hot", "critical"])
def test_filter_form_includes_urgency_option(client, urg):
    """Filter form has urgency dropdown."""
    resp = client.get("/requisitions2")
    assert f'value="{urg}"' in resp.text


def test_page_enables_hx_ext(client):
    """Page container enables HTMX extensions via hx-ext."""
    resp = client.get("/requisitions2")
    assert "hx-ext=" in resp.text
    assert "response-targets" in resp.text
    assert "loading-states" in resp.text
    assert "preload" in resp.text


def test_sse_triggers_table_refresh(client):
    """SSE source element triggers table refresh on sse:table-refresh."""
    resp = client.get("/requisitions2")
    assert "sse:table-refresh" in resp.text
    assert 'hx-get="/requisitions2/table"' in resp.text


def test_bulk_bar_shows_selection_count(client):
    """Bulk bar surfaces the live selection count (archive/activate bulk actions were
    removed — a requisition ends Won/Lost, no hide capability)."""
    resp = client.get("/requisitions2")
    assert "selectedIds.size + ' selected'" in resp.text


def test_filter_form_routes_errors_to_rq2_error(client):
    """The requisitions2 controls route HTMX errors to #rq2-error."""
    resp = client.get("/requisitions2")
    assert 'hx-target-error="#rq2-error"' in resp.text


def test_sales_user_no_owner_filter(client, db_session, sales_user):
    """Sales role does NOT see owner filter."""
    from app.dependencies import require_user
    from app.main import app

    app.dependency_overrides[require_user] = lambda: sales_user
    try:
        resp = client.get("/requisitions2")
        assert "All owners" not in resp.text
    finally:
        test_user_row = (
            db_session.query(__import__("app.models", fromlist=["User"]).User).filter_by(role="buyer").first()
        )
        if test_user_row:
            app.dependency_overrides[require_user] = lambda: test_user_row


# ── Table structure (GET /requisitions2/table) ───────────────────────


def test_table_has_sortable_headers(client, test_requisition):
    """Table headers carry hx-get sort links.

    See test_v2_thead_name_status_customer_are_sortable for the per-column assertion.
    """
    resp = client.get("/requisitions2/table", params={"status": "all"})
    assert "hx-get" in resp.text
    assert "sort=" in resp.text


def test_table_row_has_checkbox(client, test_requisition):
    """Each row has a selection checkbox with Alpine binding."""
    resp = client.get("/requisitions2/table", params={"status": "all"})
    assert "toggleSelection" in resp.text
    assert "x-bind:checked" in resp.text


def test_table_row_has_detail_link(client, test_requisition):
    """Requisition row links to detail panel via HTMX."""
    resp = client.get("/requisitions2/table", params={"status": "all"})
    assert f'hx-get="/requisitions2/{test_requisition.id}/detail"' in resp.text
    assert 'hx-target="#rq2-detail"' in resp.text


def test_table_row_shows_status_badge(client, test_requisition, db_session):
    """Table rows display status as a badge."""
    test_requisition.status = "open"
    db_session.commit()

    resp = client.get("/requisitions2/table", params={"status": "open"})
    assert "Open" in resp.text


def test_table_row_shows_urgency_badge(client, test_requisition, db_session):
    """Critical urgency renders the v2 row urgency-accent class."""
    test_requisition.status = "open"
    test_requisition.urgency = "critical"
    db_session.commit()

    resp = client.get("/requisitions2/table", params={"status": "open"})
    assert "opp-row--urgent" in resp.text


def test_empty_table_shows_message(client):
    """Empty table shows 'No requisitions found' message."""
    resp = client.get("/requisitions2/table", params={"status": "open"})
    assert "No requisitions found" in resp.text


def test_table_row_action_rail_has_hotlist_button(client, test_requisition, db_session):
    """The row action rail offers a Hotlist toggle for a non-terminal, un-archived
    req."""
    test_requisition.status = "open"
    db_session.commit()
    resp = client.get("/requisitions2/table", params={"status": "open"})
    assert f'hx-post="/requisitions2/{test_requisition.id}/action/hotlist"' in resp.text
    assert f"Add {test_requisition.name} to Hotlist" in resp.text


def test_table_row_hotlist_hides_hotlist_button(client, test_requisition, db_session):
    """A req already on the Hotlist does not re-offer the Hotlist button."""
    test_requisition.status = "hotlist"
    db_session.commit()
    resp = client.get("/requisitions2/table", params={"status": "hotlist"})
    assert "REQ-TEST-001" in resp.text
    assert f'hx-post="/requisitions2/{test_requisition.id}/action/hotlist"' not in resp.text


def test_table_row_has_detail_click(client, test_requisition):
    """Table rows load detail panel on click."""
    resp = client.get("/requisitions2/table", params={"status": "all"})
    assert f'hx-get="/requisitions2/{test_requisition.id}/detail"' in resp.text


def test_compact_rows_have_detail_link(client, test_requisition):
    """Compact rows link to detail panel, not inline edit."""
    resp = client.get("/requisitions2/table/rows", params={"status": "all"})
    assert f'hx-get="/requisitions2/{test_requisition.id}/detail"' in resp.text
    assert 'hx-target="#rq2-detail"' in resp.text


# ── Detail panel (GET /requisitions2/{id}/detail) ────────────────────


def test_detail_panel_has_action_buttons(client, test_requisition, db_session):
    """Detail panel has action buttons with hx-post."""
    test_requisition.status = "open"
    db_session.commit()

    resp = client.get(f"/requisitions2/{test_requisition.id}/detail")
    assert f'hx-post="/requisitions2/{test_requisition.id}/action/won"' in resp.text


def test_detail_shows_claim_for_buyer(client, test_requisition, db_session):
    """Unclaimed detail panel shows Claim button for buyer users."""
    test_requisition.status = "open"
    test_requisition.claimed_by_id = None
    db_session.commit()

    resp = client.get(f"/requisitions2/{test_requisition.id}/detail")
    assert "action/claim" in resp.text


def test_detail_shows_unclaim_for_claimer(client, test_requisition, test_user, db_session):
    """Detail panel for req claimed by current user shows Unclaim button."""
    test_requisition.status = "open"
    test_requisition.claimed_by_id = test_user.id
    db_session.commit()

    resp = client.get(f"/requisitions2/{test_requisition.id}/detail")
    assert "action/unclaim" in resp.text


def test_detail_panel_action_buttons_exist(client, test_requisition, db_session):
    """Detail panel has action buttons."""
    test_requisition.status = "open"
    db_session.commit()

    resp = client.get(f"/requisitions2/{test_requisition.id}/detail")
    assert "action/hotlist" in resp.text


def test_detail_panel_has_action_buttons_for_active(client, test_requisition, db_session):
    """Detail panel for an open req shows won and hotlist buttons."""
    test_requisition.status = "open"
    db_session.commit()

    resp = client.get(f"/requisitions2/{test_requisition.id}/detail")
    assert "action/won" in resp.text
    assert "action/hotlist" in resp.text


# ── Modal structure (GET /requisitions2/{id}/modal) ──────────────────


@pytest.mark.parametrize(
    "snippet",
    [
        pytest.param("x-on:click", id="close-button-click"),
        pytest.param("Close", id="close-button-label"),
        pytest.param("keydown.escape", id="escape-handler"),
        pytest.param("x-on:click.self", id="backdrop-close"),
        pytest.param("LM317T", id="requirements-mpn"),
        pytest.param("1000", id="requirements-qty"),
        pytest.param("Customer", id="field-customer"),
        pytest.param("Owner", id="field-owner"),
        pytest.param("Urgency", id="field-urgency"),
        pytest.param("Deadline", id="field-deadline"),
        pytest.param("x-trap", id="focus-trap"),
    ],
)
def test_modal_contains(client, test_requisition, snippet):
    """Modal renders close controls, requirements, key fields, and focus trap."""
    resp = client.get(f"/requisitions2/{test_requisition.id}/modal")
    assert snippet in resp.text


# ── Inline editing cells (GET /requisitions2/{id}/edit/name) ─────────


@pytest.mark.parametrize(
    "snippet",
    [
        pytest.param("autofocus", id="autofocus"),
        pytest.param("keydown.escape", id="escape-handler"),
    ],
)
def test_inline_edit_name_cell_contains(client, test_requisition, snippet):
    """Inline name edit cell has autofocus and an escape key handler."""
    resp = client.get(f"/requisitions2/{test_requisition.id}/edit/name")
    assert snippet in resp.text


# ── Pagination controls ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("prefix", "expected"),
    [
        pytest.param("PAG", "Prev", id="page-2-shows-prev"),
        pytest.param("PRE", "preload", id="pagination-links-preload"),
    ],
)
def test_pagination_page_2(client, db_session, test_user, prefix, expected):
    """Page 2 shows a Prev link and preloaded pagination links."""
    from app.models import Requisition

    for i in range(30):
        db_session.add(
            Requisition(
                name=f"{prefix}-{i:03d}",
                status="open",
                created_by=test_user.id,
                created_at=datetime.now(timezone.utc),
            )
        )
    db_session.commit()

    resp = client.get("/requisitions2/table", params={"status": "open", "per_page": "10", "page": "2"})
    assert expected in resp.text


def test_pagination_no_controls_for_single_page(client, test_requisition):
    """Single page of results shows no pagination controls."""
    resp = client.get("/requisitions2/table", params={"status": "all"})
    # With just 1 requisition and 25 per page, no Next link
    assert "Next" not in resp.text


# ── v2 opportunity-table rendering ───────────────────────────────────


def test_v2_renders_opp_col_header(client, test_requisition):
    """The opportunity-table thead renders v2 opp-col-header cells."""
    resp = client.get("/requisitions2/table", params={"status": "all"})
    assert "opp-col-header" in resp.text


def test_v2_thead_name_status_customer_are_sortable(client, test_requisition):
    """V2 thead carries hx-get sort links on Name/Status/Customer for legacy UX parity.

    Coverage and Deal are derived/aggregate and intentionally not sortable.
    """
    resp = client.get("/requisitions2/table", params={"status": "all"})
    html = resp.text
    for col in ("name", "status", "customer_name"):
        assert f"sort={col}" in html, f"v2 thead missing sort link for {col}"
    assert "sort=coverage" not in html
    assert "sort=deal" not in html
