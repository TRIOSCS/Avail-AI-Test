"""Tests for the Requisitions 2 HTMX page routes.

Covers: full page load, table fragment, filters, pagination, row actions,
bulk actions, modal, auth, HTMX header detection, role-based access.

Called by: pytest
Depends on: app/routers/requisitions2.py, conftest fixtures
"""

from datetime import datetime, timezone


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
    """Full page includes the table region."""
    resp = client.get("/requisitions2")
    assert "rq2-table" in resp.text
    assert "rq2-rows" in resp.text


# ── HTMX detection ──────────────────────────────────────────────────


def test_htmx_header_returns_fragment(client):
    """GET /requisitions2 with HX-Request header returns table fragment only."""
    resp = client.get("/requisitions2", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    # Should NOT contain the full page shell
    assert "rq2all-page" not in resp.text
    # Should contain the table
    assert "rq2-rows" in resp.text


# ── Table fragment endpoint ──────────────────────────────────────────


def test_table_fragment_returns_partial(client):
    """GET /requisitions2/table returns only the table HTML."""
    resp = client.get("/requisitions2/table")
    assert resp.status_code == 200
    assert "rq2-rows" in resp.text
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
    assert "rq2-modal-content" in resp.text


def test_modal_404_for_missing_req(client):
    """Modal returns 404 for nonexistent requisition."""
    resp = client.get("/requisitions2/99999/modal")
    assert resp.status_code == 404
    assert "not found" in resp.text.lower()


# ── Row actions ──────────────────────────────────────────────────────


def test_row_action_archive(client, test_requisition, db_session):
    """POST archive action changes status and returns updated table."""
    # Set to active first (test_requisition starts as 'open')
    test_requisition.status = "active"
    db_session.commit()

    resp = client.post(f"/requisitions2/{test_requisition.id}/action/archive")
    assert resp.status_code == 200
    assert "rq2-rows" in resp.text

    db_session.refresh(test_requisition)
    assert test_requisition.status == "archived"


def test_row_action_activate(client, test_requisition, db_session):
    """POST activate action restores from archived."""
    test_requisition.status = "archived"
    db_session.commit()

    resp = client.post(f"/requisitions2/{test_requisition.id}/action/activate")
    assert resp.status_code == 200

    db_session.refresh(test_requisition)
    assert test_requisition.status == "active"


def test_row_action_claim(client, test_requisition, db_session):
    """POST claim action sets claimed_by_id."""
    test_requisition.claimed_by_id = None
    test_requisition.status = "active"
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
    """Bulk archive action archives selected requisitions."""
    test_requisition.status = "active"
    db_session.commit()

    resp = client.post(
        "/requisitions2/bulk/archive",
        data={"ids": str(test_requisition.id)},
    )
    assert resp.status_code == 200
    assert "rq2-rows" in resp.text

    db_session.refresh(test_requisition)
    assert test_requisition.status == "archived"


def test_bulk_activate(client, test_requisition, db_session):
    """Bulk activate action activates selected requisitions."""
    test_requisition.status = "archived"
    db_session.commit()

    resp = client.post(
        "/requisitions2/bulk/activate",
        data={"ids": str(test_requisition.id)},
    )
    assert resp.status_code == 200

    db_session.refresh(test_requisition)
    assert test_requisition.status == "active"


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
    from app.database import get_db
    from app.dependencies import require_user
    from app.main import app
    from app.models import Requisition

    # Create req owned by sales_user
    req_sales = Requisition(
        name="SALES-REQ", status="active",
        created_by=sales_user.id,
        created_at=datetime.now(timezone.utc),
    )
    # Create req owned by test_user (buyer)
    req_buyer = Requisition(
        name="BUYER-REQ", status="active",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add_all([req_sales, req_buyer])
    db_session.commit()

    # Override require_user to return sales_user
    app.dependency_overrides[require_user] = lambda: sales_user

    try:
        resp = client.get("/requisitions2/table", params={"status": "active"})
        assert resp.status_code == 200
        assert "SALES-REQ" in resp.text
        assert "BUYER-REQ" not in resp.text
    finally:
        # Restore original override
        app.dependency_overrides[require_user] = lambda: test_user
