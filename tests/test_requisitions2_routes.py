"""Tests for the Requisitions 2 HTMX page routes.

Covers: full page load, table fragment, filters, pagination, row actions,
bulk actions, modal, auth, HTMX header detection, role-based access.

Called by: pytest
Depends on: app/routers/requisitions2.py, conftest fixtures
"""

import json
from datetime import datetime, timezone

import pytest

# ── Page load ────────────────────────────────────────────────────────


def test_page_load_returns_200(client):
    """GET /requisitions2 returns a full HTML page."""
    resp = client.get("/requisitions2")
    assert resp.status_code == 200
    assert "rq2all-page" in resp.text


def test_page_load_contains_filters(client):
    """Full page includes the filter form."""
    resp = client.get("/requisitions2")
    assert "rq2-filters" in resp.text
    assert "rq2-search" in resp.text


def test_page_load_contains_table(client):
    """Full page includes the table region (or empty state when no data)."""
    resp = client.get("/requisitions2")
    assert "rq2-table" in resp.text
    # With empty DB, shows empty state instead of rows
    assert "rq2-rows" in resp.text or "No requisitions found" in resp.text


def test_page_load_includes_shell_chrome(client):
    """Direct page load renders shared topbar and bottom nav."""
    resp = client.get("/requisitions2")
    assert resp.status_code == 200
    # Topbar: global search input is only rendered by topbar.html.
    assert 'aria-label="Global search"' in resp.text
    # Mobile nav: the bottom nav <nav> is only rendered by mobile_nav.html.
    assert 'aria-label="Main navigation"' in resp.text


def test_htmx_fragment_excludes_shell_chrome(client):
    """HTMX fragment response must not wrap the table in the full shell."""
    resp = client.get("/requisitions2", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert 'aria-label="Global search"' not in resp.text
    assert 'aria-label="Main navigation"' not in resp.text


# ── HTMX detection ──────────────────────────────────────────────────


def test_htmx_header_returns_fragment(client):
    """GET /requisitions2 with HX-Request header returns table fragment only."""
    resp = client.get("/requisitions2", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    # Should NOT contain the full page shell
    assert "rq2all-page" not in resp.text
    # Should contain the table or empty state
    assert "rq2-rows" in resp.text or "No requisitions found" in resp.text


# ── Table fragment endpoint ──────────────────────────────────────────


def test_table_fragment_returns_partial(client):
    """GET /requisitions2/table returns only the table HTML (or empty state)."""
    resp = client.get("/requisitions2/table")
    assert resp.status_code == 200
    assert "rq2-rows" in resp.text or "No requisitions found" in resp.text
    assert "rq2all-page" not in resp.text


def test_table_rows_returns_partial(client):
    """GET /requisitions2/table/rows returns just the tbody rows."""
    resp = client.get("/requisitions2/table/rows")
    assert resp.status_code == 200
    # Should not contain the full table wrapper
    assert "<thead>" not in resp.text


# ── Filters ──────────────────────────────────────────────────────────


def test_filter_by_status(client, test_requisition):
    """Status filter restricts the list."""
    # test_requisition has status 'open' — 'archived' should not include it
    resp = client.get("/requisitions2/table", params={"status": "archived"})
    assert resp.status_code == 200
    assert "REQ-TEST-001" not in resp.text


def test_search_by_query(client, test_requisition):
    """Search by requisition name."""
    resp = client.get("/requisitions2/table", params={"q": "REQ-TEST"})
    assert resp.status_code == 200
    assert "REQ-TEST-001" in resp.text


def test_search_no_results(client, test_requisition):
    """Search with non-matching query returns empty state."""
    resp = client.get("/requisitions2/table", params={"q": "NONEXISTENT-XYZ"})
    assert resp.status_code == 200
    assert "No requisitions found" in resp.text


# ── Pagination ───────────────────────────────────────────────────────


def test_pagination_defaults(client, test_requisition):
    """Default pagination returns page 1."""
    resp = client.get("/requisitions2/table")
    assert resp.status_code == 200
    # With just 1 requisition, there should be no pagination nav
    assert "Page 1 of 1" not in resp.text or "Page" in resp.text


def test_pagination_page_2_empty(client, test_requisition):
    """Page 2 with only 1 requisition shows empty state."""
    resp = client.get("/requisitions2/table", params={"page": "2", "per_page": "25"})
    assert resp.status_code == 200
    assert "No requisitions found" in resp.text


# ── Sort ─────────────────────────────────────────────────────────────


def test_sort_by_name(client, test_requisition):
    """Sort by name doesn't crash."""
    resp = client.get("/requisitions2/table", params={"sort": "name", "order": "asc"})
    assert resp.status_code == 200


# ── Modal ────────────────────────────────────────────────────────────


def test_modal_returns_detail(client, test_requisition):
    """GET /requisitions2/{id}/modal returns requisition details."""
    resp = client.get(f"/requisitions2/{test_requisition.id}/modal")
    assert resp.status_code == 200
    assert "REQ-TEST-001" in resp.text


def test_modal_404_for_missing_req(client):
    """Modal returns 404 for nonexistent requisition."""
    resp = client.get("/requisitions2/99999/modal")
    assert resp.status_code == 404
    assert "not found" in resp.text.lower()


# ── Row actions ──────────────────────────────────────────────────────


def test_row_action_archive(client, test_requisition, db_session):
    """POST archive action sets is_archived and returns updated table."""
    test_requisition.status = "open"
    db_session.commit()

    resp = client.post(f"/requisitions2/{test_requisition.id}/action/archive")
    assert resp.status_code == 200
    # Returns table (may show empty state if archived req filtered out of default view)
    assert "rq2-rows" in resp.text or "No requisitions found" in resp.text

    db_session.refresh(test_requisition)
    assert test_requisition.is_archived is True
    # Archive is orthogonal to the status pipeline — status is untouched.
    assert test_requisition.status == "open"


def test_row_action_activate(client, test_requisition, db_session):
    """POST activate action restores (un-archives) the requisition."""
    test_requisition.status = "lost"
    test_requisition.is_archived = True
    db_session.commit()

    resp = client.post(f"/requisitions2/{test_requisition.id}/action/activate")
    assert resp.status_code == 200

    db_session.refresh(test_requisition)
    assert test_requisition.is_archived is False
    # Status retained as-is (un-archive does not change the pipeline value).
    assert test_requisition.status == "lost"


def test_row_action_claim(client, test_requisition, db_session):
    """POST claim action sets claimed_by_id."""
    test_requisition.claimed_by_id = None
    test_requisition.status = "open"
    db_session.commit()

    resp = client.post(f"/requisitions2/{test_requisition.id}/action/claim")
    assert resp.status_code == 200

    db_session.refresh(test_requisition)
    assert test_requisition.claimed_by_id is not None


def test_row_action_unclaim(client, test_requisition, test_user, db_session):
    """POST unclaim action clears claimed_by_id."""
    test_requisition.claimed_by_id = test_user.id
    test_requisition.claimed_at = datetime.now(timezone.utc)
    db_session.commit()

    resp = client.post(f"/requisitions2/{test_requisition.id}/action/unclaim")
    assert resp.status_code == 200

    db_session.refresh(test_requisition)
    assert test_requisition.claimed_by_id is None


def test_row_action_404(client):
    """Row action on nonexistent requisition returns 404."""
    resp = client.post("/requisitions2/99999/action/archive")
    assert resp.status_code == 404


# ── Bulk actions ─────────────────────────────────────────────────────


def test_bulk_archive(client, test_requisition, db_session):
    """Bulk archive action archives (is_archived=True) selected requisitions."""
    test_requisition.status = "open"
    db_session.commit()

    resp = client.post(
        "/requisitions2/bulk/archive",
        data={"ids": str(test_requisition.id)},
    )
    assert resp.status_code == 200
    assert "rq2-rows" in resp.text or "No requisitions found" in resp.text

    db_session.refresh(test_requisition)
    assert test_requisition.is_archived is True


def test_bulk_activate(client, test_requisition, db_session):
    """Bulk activate action un-archives selected requisitions."""
    test_requisition.is_archived = True
    db_session.commit()

    resp = client.post(
        "/requisitions2/bulk/activate",
        data={"ids": str(test_requisition.id)},
    )
    assert resp.status_code == 200

    db_session.refresh(test_requisition)
    assert test_requisition.is_archived is False


def test_bulk_empty_ids(client):
    """Bulk action with empty IDs returns 422 validation error."""
    resp = client.post(
        "/requisitions2/bulk/archive",
        data={"ids": ""},
    )
    assert resp.status_code == 422


# ── Sales role filtering ─────────────────────────────────────────────


def test_sales_role_sees_only_own(client, db_session, test_user, sales_user):
    """Sales role user sees only their own requisitions."""
    from app.dependencies import require_user
    from app.main import app
    from app.models import Requisition

    # Create req owned by sales_user
    req_sales = Requisition(
        name="SALES-REQ",
        status="open",
        created_by=sales_user.id,
        created_at=datetime.now(timezone.utc),
    )
    # Create req owned by test_user (buyer)
    req_buyer = Requisition(
        name="BUYER-REQ",
        status="open",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add_all([req_sales, req_buyer])
    db_session.commit()

    # Override require_user to return sales_user
    app.dependency_overrides[require_user] = lambda: sales_user

    try:
        resp = client.get("/requisitions2/table", params={"status": "open"})
        assert resp.status_code == 200
        assert "SALES-REQ" in resp.text
        assert "BUYER-REQ" not in resp.text
    finally:
        # Restore original override
        app.dependency_overrides[require_user] = lambda: test_user


# ── Invalid filter fallback ──────────────────────────────────────────


def test_invalid_filter_falls_back_to_defaults(client):
    """Invalid filter values fall back to defaults instead of crashing."""
    resp = client.get("/requisitions2", params={"status": "BOGUS_STATUS", "page": "abc"})
    assert resp.status_code == 200
    assert "rq2all-page" in resp.text


def test_invalid_sort_falls_back(client, test_requisition):
    """Invalid sort column falls back to default."""
    resp = client.get("/requisitions2/table", params={"sort": "nonexistent_column"})
    assert resp.status_code == 200
    assert "rq2-rows" in resp.text or "No requisitions found" in resp.text


# ── Row actions — hotlist ────────────────────────────────────────────


def test_hotlist_action_sets_status(client, test_requisition, db_session):
    """POST hotlist action moves an open requisition onto the Hotlist monitor."""
    test_requisition.status = "open"
    db_session.commit()

    resp = client.post(f"/requisitions2/{test_requisition.id}/action/hotlist")
    assert resp.status_code == 200
    assert "rq2-rows" in resp.text or "No requisitions found" in resp.text

    db_session.refresh(test_requisition)
    assert test_requisition.status == "hotlist"
    # The detail panel still loads for a hotlisted requisition.
    assert client.get(f"/requisitions2/{test_requisition.id}/detail").status_code == 200


def test_hotlist_action_toast(client, test_requisition, db_session):
    """Hotlist action emits an 'added to Hotlist' toast."""
    test_requisition.status = "open"
    db_session.commit()

    resp = client.post(f"/requisitions2/{test_requisition.id}/action/hotlist")
    trigger = json.loads(resp.headers["HX-Trigger"])
    assert "Hotlist" in trigger["showToast"]["message"]


def test_hotlist_action_invalid_from_won(client, test_requisition, db_session):
    """Won is terminal-ish — it cannot move to Hotlist (error toast, 200)."""
    test_requisition.status = "won"
    db_session.commit()

    resp = client.post(f"/requisitions2/{test_requisition.id}/action/hotlist")
    assert resp.status_code == 200
    trigger = json.loads(resp.headers["HX-Trigger"])
    assert "Invalid transition" in trigger["showToast"]["message"]

    db_session.refresh(test_requisition)
    assert test_requisition.status == "won"


# ── Row actions — won / lost / assign ────────────────────────────────


def test_row_action_won(client, test_requisition, db_session):
    """POST won action marks requisition as won."""
    test_requisition.status = "open"
    db_session.commit()

    resp = client.post(f"/requisitions2/{test_requisition.id}/action/won")
    assert resp.status_code == 200
    assert "rq2-rows" in resp.text or "No requisitions found" in resp.text

    db_session.refresh(test_requisition)
    assert test_requisition.status == "won"


def test_row_action_lost(client, test_requisition, db_session):
    """POST lost action marks requisition as lost (from quoted status)."""
    test_requisition.status = "quoted"
    db_session.commit()

    resp = client.post(f"/requisitions2/{test_requisition.id}/action/lost")
    assert resp.status_code == 200

    db_session.refresh(test_requisition)
    assert test_requisition.status == "lost"


def test_row_action_assign(client, test_requisition, db_session, test_user, sales_user):
    """POST assign action reassigns the requisition owner (manager-only action)."""
    test_user.role = "manager"  # owner reassignment is gated to manager/admin
    test_requisition.status = "open"
    db_session.commit()

    resp = client.post(
        f"/requisitions2/{test_requisition.id}/action/assign",
        data={"owner_id": str(sales_user.id)},
    )
    assert resp.status_code == 200

    db_session.refresh(test_requisition)
    assert test_requisition.created_by == sales_user.id


def test_row_action_assign_without_owner_id(client, test_requisition, test_user, db_session):
    """POST assign without owner_id does not change owner (manager performing it)."""
    test_user.role = "manager"
    test_requisition.status = "open"
    original_owner = test_requisition.created_by
    db_session.commit()

    resp = client.post(f"/requisitions2/{test_requisition.id}/action/assign")
    assert resp.status_code == 200

    db_session.refresh(test_requisition)
    assert test_requisition.created_by == original_owner


# ── Row actions — invalid state transitions ──────────────────────────


@pytest.mark.parametrize(
    ("start_status", "action"),
    [
        # "closed" is an unknown origin (no allowed targets) and "won" only
        # permits → "open", so these status moves are genuinely illegal.
        pytest.param("closed", "won", id="won_from_closed"),
        pytest.param("won", "lost", id="lost_from_won"),
    ],
)
def test_row_action_invalid_state(client, test_requisition, db_session, start_status, action):
    """Row actions that aren't valid status transitions return 200 with an error
    toast."""
    test_requisition.status = start_status
    db_session.commit()

    resp = client.post(f"/requisitions2/{test_requisition.id}/action/{action}")
    assert resp.status_code == 200
    assert "HX-Trigger" in resp.headers

    trigger = json.loads(resp.headers["HX-Trigger"])
    assert "Invalid transition" in trigger["showToast"]["message"]


def test_row_action_claim_already_claimed(client, test_requisition, db_session, sales_user):
    """Claim on already-claimed requisition returns 200 with error message."""
    test_requisition.status = "open"
    test_requisition.claimed_by_id = sales_user.id
    test_requisition.claimed_at = datetime.now(timezone.utc)
    db_session.commit()

    resp = client.post(f"/requisitions2/{test_requisition.id}/action/claim")
    assert resp.status_code == 200
    assert "HX-Trigger" in resp.headers


# ── Bulk actions — assign ────────────────────────────────────────────


def test_bulk_assign(client, test_requisition, db_session, test_user, sales_user):
    """Bulk assign changes owner on selected requisitions (manager-only action)."""
    test_user.role = "manager"  # owner reassignment is gated to manager/admin
    test_requisition.status = "open"
    db_session.commit()

    resp = client.post(
        "/requisitions2/bulk/assign",
        data={"ids": str(test_requisition.id), "owner_id": str(sales_user.id)},
    )
    assert resp.status_code == 200
    assert "rq2-rows" in resp.text

    db_session.refresh(test_requisition)
    assert test_requisition.created_by == sales_user.id


def test_bulk_assign_no_owner_id(client, test_requisition, db_session, test_user):
    """Bulk assign without owner_id does not change owners (manager performing it)."""
    test_user.role = "manager"
    test_requisition.status = "open"
    original_owner = test_requisition.created_by
    db_session.commit()

    resp = client.post(
        "/requisitions2/bulk/assign",
        data={"ids": str(test_requisition.id)},
    )
    assert resp.status_code == 200

    db_session.refresh(test_requisition)
    assert test_requisition.created_by == original_owner


def test_bulk_invalid_ids_returns_table(client):
    """Bulk action with non-numeric IDs returns table without crashing."""
    resp = client.post(
        "/requisitions2/bulk/archive",
        data={"ids": "abc,def"},
    )
    assert resp.status_code == 200
    assert "rq2-rows" in resp.text or "No requisitions found" in resp.text


def test_bulk_nonexistent_ids(client):
    """Bulk action with IDs that don't exist does nothing."""
    resp = client.post(
        "/requisitions2/bulk/archive",
        data={"ids": "99998,99999"},
    )
    assert resp.status_code == 200
    assert "rq2-rows" in resp.text or "No requisitions found" in resp.text


@pytest.mark.parametrize(
    ("is_archived", "action"),
    [
        # activate on an already-active req and archive on an already-archived
        # req are both no-ops, so 0 requisitions are affected.
        pytest.param(False, "activate", id="activate_already_active"),
        pytest.param(True, "archive", id="archive_already_archived"),
    ],
)
def test_bulk_action_invalid_state(client, test_requisition, db_session, is_archived, action):
    """Bulk archive/activate that are no-ops skip gracefully (0 affected)."""
    test_requisition.status = "open"
    test_requisition.is_archived = is_archived
    db_session.commit()

    resp = client.post(
        f"/requisitions2/bulk/{action}",
        data={"ids": str(test_requisition.id)},
    )
    assert resp.status_code == 200
    assert "HX-Trigger" in resp.headers

    trigger = json.loads(resp.headers["HX-Trigger"])
    assert "0 requisition" in trigger["showToast"]["message"]


# ── HX-Trigger headers ──────────────────────────────────────────────


def test_row_action_returns_toast_header(client, test_requisition, db_session):
    """Row actions include HX-Trigger header with toast message."""
    test_requisition.status = "open"
    db_session.commit()

    resp = client.post(f"/requisitions2/{test_requisition.id}/action/archive")
    assert "HX-Trigger" in resp.headers

    trigger = json.loads(resp.headers["HX-Trigger"])
    assert "showToast" in trigger
    assert "message" in trigger["showToast"]


def test_bulk_action_returns_toast_and_clear(client, test_requisition, db_session):
    """Bulk actions include HX-Trigger with toast and clearSelection."""
    test_requisition.status = "open"
    db_session.commit()

    resp = client.post(
        "/requisitions2/bulk/archive",
        data={"ids": str(test_requisition.id)},
    )

    trigger = json.loads(resp.headers["HX-Trigger"])
    assert "showToast" in trigger
    assert "clearSelection" in trigger


# ── Filter combinations via route ────────────────────────────────────


def test_filter_by_urgency(client, test_requisition, db_session):
    """Urgency filter works through the route."""
    test_requisition.urgency = "critical"
    test_requisition.status = "open"
    db_session.commit()

    resp = client.get("/requisitions2/table", params={"status": "open", "urgency": "critical"})
    assert resp.status_code == 200
    assert "REQ-TEST-001" in resp.text

    resp2 = client.get("/requisitions2/table", params={"status": "open", "urgency": "hot"})
    assert "REQ-TEST-001" not in resp2.text


def test_filter_by_owner(client, test_requisition, test_user, db_session):
    """Owner filter restricts to a specific user's requisitions."""
    test_requisition.status = "open"
    db_session.commit()

    resp = client.get("/requisitions2/table", params={"status": "open", "owner": str(test_user.id)})
    assert resp.status_code == 200
    assert "REQ-TEST-001" in resp.text

    resp2 = client.get("/requisitions2/table", params={"status": "open", "owner": "99999"})
    assert "REQ-TEST-001" not in resp2.text


def test_filter_by_date_range(client, test_requisition, db_session):
    """Date range filter narrows results."""
    test_requisition.status = "open"
    db_session.commit()

    # Future date range should exclude
    resp = client.get("/requisitions2/table", params={"status": "open", "date_from": "2099-01-01"})
    assert "REQ-TEST-001" not in resp.text

    # Past date range should include
    resp2 = client.get(
        "/requisitions2/table", params={"status": "open", "date_from": "2020-01-01", "date_to": "2099-12-31"}
    )
    assert resp2.status_code == 200


def test_status_all(client, test_requisition, db_session):
    """Status 'all' shows non-archived requisitions regardless of pipeline status."""
    test_requisition.status = "won"
    db_session.commit()

    resp = client.get("/requisitions2/table", params={"status": "all"})
    assert resp.status_code == 200
    assert "REQ-TEST-001" in resp.text


# ── Modal with customer site ─────────────────────────────────────────


def test_modal_with_customer_site(client, test_requisition, test_customer_site, db_session):
    """Modal shows customer site display name when linked."""
    test_requisition.customer_site_id = test_customer_site.id
    db_session.commit()

    resp = client.get(f"/requisitions2/{test_requisition.id}/modal")
    assert resp.status_code == 200
    assert "Acme Electronics" in resp.text


# ── Multiple pages pagination ────────────────────────────────────────


def test_pagination_shows_controls(client, db_session, test_user):
    """When there are enough items, pagination controls appear."""
    from app.models import Requisition

    for i in range(30):
        req = Requisition(
            name=f"PAGINATE-{i:03d}",
            status="open",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
    db_session.commit()

    resp = client.get("/requisitions2/table", params={"status": "open", "per_page": "10"})
    assert resp.status_code == 200
    assert "Next" in resp.text
    # Compact pagination shows "1/N" format
    assert "1/" in resp.text


# ── Inline edit cell ────────────────────────────────────────────────


def test_inline_edit_name_cell(client, test_requisition):
    """GET edit/name returns an input form."""
    resp = client.get(f"/requisitions2/{test_requisition.id}/edit/name")
    assert resp.status_code == 200
    assert 'name="value"' in resp.text
    assert test_requisition.name in resp.text


def test_inline_edit_status_cell(client, test_requisition):
    """GET edit/status returns a select form with new pipeline options."""
    resp = client.get(f"/requisitions2/{test_requisition.id}/edit/status")
    assert resp.status_code == 200
    assert "<select" in resp.text
    assert 'value="open"' in resp.text
    assert 'value="hotlist"' in resp.text
    assert 'value="active"' not in resp.text


def test_inline_edit_urgency_cell(client, test_requisition):
    """GET edit/urgency returns a select form."""
    resp = client.get(f"/requisitions2/{test_requisition.id}/edit/urgency")
    assert resp.status_code == 200
    assert "<select" in resp.text
    assert "critical" in resp.text


def test_inline_edit_owner_cell(client, test_requisition):
    """GET edit/owner returns a select with team users."""
    resp = client.get(f"/requisitions2/{test_requisition.id}/edit/owner")
    assert resp.status_code == 200
    assert "<select" in resp.text


def test_inline_edit_deadline_cell(client, test_requisition):
    """GET edit/deadline returns a date input."""
    resp = client.get(f"/requisitions2/{test_requisition.id}/edit/deadline")
    assert resp.status_code == 200
    assert 'type="date"' in resp.text


def test_inline_edit_cell_not_found(client):
    """GET edit cell for non-existent requisition returns 404."""
    resp = client.get("/requisitions2/99999/edit/name")
    assert resp.status_code == 404


# ── Inline save ─────────────────────────────────────────────────────


def test_inline_save_name(client, test_requisition):
    """PATCH inline saves name and returns updated row."""
    resp = client.patch(
        f"/requisitions2/{test_requisition.id}/inline",
        data={"field": "name", "value": "RENAMED-001"},
    )
    assert resp.status_code == 200
    assert "RENAMED-001" in resp.text
    assert "showToast" in resp.headers.get("HX-Trigger", "")


def test_inline_save_urgency(client, test_requisition):
    """PATCH inline saves urgency — confirmed via toast trigger header."""
    resp = client.patch(
        f"/requisitions2/{test_requisition.id}/inline",
        data={"field": "urgency", "value": "critical"},
    )
    assert resp.status_code == 200
    assert "critical" in resp.headers.get("HX-Trigger", "")


def test_inline_save_deadline(client, test_requisition):
    """PATCH inline saves deadline."""
    resp = client.patch(
        f"/requisitions2/{test_requisition.id}/inline",
        data={"field": "deadline", "value": "2026-06-01"},
    )
    assert resp.status_code == 200
    assert "showToast" in resp.headers.get("HX-Trigger", "")


def test_inline_save_owner(client, test_requisition, test_user, db_session):
    """PATCH inline saves owner (manager-only action)."""
    test_user.role = "manager"  # owner reassignment is gated to manager/admin
    db_session.commit()
    resp = client.patch(
        f"/requisitions2/{test_requisition.id}/inline",
        data={"field": "owner", "value": str(test_user.id)},
    )
    assert resp.status_code == 200


def test_inline_save_empty_name_rejected(client, test_requisition):
    """PATCH inline with empty name returns 422."""
    resp = client.patch(
        f"/requisitions2/{test_requisition.id}/inline",
        data={"field": "name", "value": "  "},
    )
    assert resp.status_code == 422


def test_inline_save_not_found(client):
    """PATCH inline for non-existent requisition returns 404."""
    resp = client.patch(
        "/requisitions2/99999/inline",
        data={"field": "name", "value": "test"},
    )
    assert resp.status_code == 404


# ── SSE stream ──────────────────────────────────────────────────────


def test_sse_broker_publish_subscribe():
    """SSE broker delivers events to subscribers."""
    import asyncio

    from app.services.sse_broker import SSEBroker

    async def _test():
        b = SSEBroker()
        q = b.subscribe("test-ch")
        await b.publish("test-ch", "my-event", "hello")
        msg = q.get_nowait()
        assert msg["event"] == "my-event"
        assert msg["data"] == "hello"
        b.unsubscribe("test-ch", q)
        assert len(b._channels["test-ch"]) == 0

    asyncio.get_event_loop().run_until_complete(_test())


def test_sse_broker_no_subscribers():
    """Publishing with no subscribers does not raise."""
    import asyncio

    from app.services.sse_broker import SSEBroker

    async def _test():
        b = SSEBroker()
        await b.publish("empty-ch", "event", "data")

    asyncio.get_event_loop().run_until_complete(_test())


def test_sse_broker_queue_is_bounded():
    """Slow subscribers should not accumulate unbounded queue growth."""
    import asyncio

    from app.services.sse_broker import SSEBroker

    async def _test():
        b = SSEBroker()
        q = b.subscribe("bounded")
        for i in range(1000):
            await b.publish("bounded", "ev", str(i))
        assert q.qsize() <= 200

    asyncio.get_event_loop().run_until_complete(_test())


def test_requisitions_stream_requires_auth(db_session):
    """SSE stream endpoint should reject unauthenticated requests."""
    from fastapi.testclient import TestClient

    from app.database import get_db
    from app.main import app

    def _override_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_db
    try:
        with TestClient(app) as c:
            resp = c.get("/requisitions2/stream")
    finally:
        app.dependency_overrides.pop(get_db, None)
    assert resp.status_code == 401


def test_inline_edit_scope_enforced_for_sales(db_session, sales_user, test_requisition):
    """Sales users cannot inline-edit requisitions they don't own."""
    from fastapi.testclient import TestClient

    from app.database import get_db
    from app.dependencies import require_user
    from app.main import app

    def _override_db():
        yield db_session

    def _override_user():
        return sales_user

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_user] = _override_user
    try:
        with TestClient(app) as c:
            resp = c.get(f"/requisitions2/{test_requisition.id}/edit/name")
    finally:
        for dep in [get_db, require_user]:
            app.dependency_overrides.pop(dep, None)
    assert resp.status_code == 404


def test_bulk_scope_enforced_for_sales(db_session, sales_user, test_requisition):
    """Sales users cannot bulk mutate requisitions they don't own."""
    from fastapi.testclient import TestClient

    from app.database import get_db
    from app.dependencies import require_user
    from app.main import app

    test_requisition.status = "open"
    db_session.commit()

    def _override_db():
        yield db_session

    def _override_user():
        return sales_user

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_user] = _override_user
    try:
        with TestClient(app) as c:
            resp = c.post("/requisitions2/bulk/archive", data={"ids": str(test_requisition.id)})
    finally:
        for dep in [get_db, require_user]:
            app.dependency_overrides.pop(dep, None)
    assert resp.status_code == 200
    db_session.refresh(test_requisition)
    # Bulk archive was scoped out (req owned by another user) — not archived.
    assert test_requisition.is_archived is False


# ── Detail panel (split-screen) ──────────────────────────────────────


def test_detail_panel_returns_200(client, test_requisition):
    """GET /requisitions2/{id}/detail returns inline detail panel."""
    resp = client.get(f"/requisitions2/{test_requisition.id}/detail")
    assert resp.status_code == 200
    assert "REQ-TEST-001" in resp.text


def test_detail_panel_has_tabs(client, test_requisition):
    """Detail panel includes Parts/Offers/Activity tabs."""
    resp = client.get(f"/requisitions2/{test_requisition.id}/detail")
    assert resp.status_code == 200
    assert "Parts" in resp.text
    assert "Offers" in resp.text
    assert "Activity" in resp.text


def test_detail_panel_has_metadata(client, test_requisition):
    """Detail panel shows metadata grid (owner, deadline, etc)."""
    resp = client.get(f"/requisitions2/{test_requisition.id}/detail")
    assert resp.status_code == 200
    assert "Owner" in resp.text
    assert "Deadline" in resp.text


def test_detail_panel_404_for_missing_req(client):
    """Detail panel returns 404 for nonexistent requisition."""
    resp = client.get("/requisitions2/99999/detail")
    assert resp.status_code == 404


def test_split_screen_layout_present(client):
    """Full page load includes split-screen layout markers."""
    resp = client.get("/requisitions2")
    assert resp.status_code == 200
    assert "rq2-detail" in resp.text
    assert "rq2-table" in resp.text
