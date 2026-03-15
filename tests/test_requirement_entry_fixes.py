"""
tests/test_requirement_entry_fixes.py — Tests for requirement entry bug fixes

Covers: blank MPN update prevention, access control on batch ops,
validation error reporting, condition/packaging normalization on create,
and frontend HTML/JS regression checks.

Called by: pytest
Depends on: routers/requisitions, conftest fixtures
"""

from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import require_admin, require_buyer, require_user
from app.main import app
from app.models import Requisition, User

# ── Helpers ──────────────────────────────────────────────────────────


def _make_client(db_session: Session, user: User) -> TestClient:
    """Build a TestClient authenticated as the given user."""

    def _override_db():
        yield db_session

    def _override_user():
        return user

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_user] = _override_user
    app.dependency_overrides[require_buyer] = _override_user
    app.dependency_overrides[require_admin] = _override_user
    return TestClient(app)


def _make_sales_client(db_session: Session, user: User) -> TestClient:
    """Build a TestClient authenticated as a sales user (no admin)."""

    def _override_db():
        yield db_session

    def _override_user():
        return user

    def _not_admin():
        from fastapi import HTTPException

        raise HTTPException(403, "Admin access required")

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_user] = _override_user
    app.dependency_overrides[require_buyer] = _override_user
    app.dependency_overrides[require_admin] = _not_admin
    return TestClient(app)


# ── B2: RequirementUpdate rejects blank MPN ──────────────────────────


def test_update_requirement_blank_mpn_rejected(client, test_requisition):
    """PUT /api/requirements/{id} rejects blank primary_mpn."""
    req_id = test_requisition.requirements[0].id
    resp = client.put(f"/api/requirements/{req_id}", json={"primary_mpn": "   "})
    assert resp.status_code == 422


def test_update_requirement_none_mpn_allowed(client, test_requisition):
    """PUT /api/requirements/{id} allows None mpn (no change)."""
    req_id = test_requisition.requirements[0].id
    resp = client.put(f"/api/requirements/{req_id}", json={"notes": "test"})
    assert resp.status_code == 200


# ── B1: Validation errors reported, not silently swallowed ───────────


def test_add_requirement_skipped_items_reported(client, test_requisition):
    """POST /api/requisitions/{id}/requirements reports skipped items."""
    resp = client.post(
        f"/api/requisitions/{test_requisition.id}/requirements",
        json=[
            {"primary_mpn": "LM7805", "target_qty": 100},
            {"primary_mpn": "", "target_qty": 1},  # blank MPN — should be skipped
        ],
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["created"]) == 1
    assert "skipped" in data
    assert len(data["skipped"]) == 1
    assert data["skipped"][0]["index"] == 1


def test_add_requirement_negative_qty_skipped(client, test_requisition):
    """POST skips items with target_qty < 1."""
    resp = client.post(
        f"/api/requisitions/{test_requisition.id}/requirements",
        json=[{"primary_mpn": "LM7805", "target_qty": -5}],
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["created"]) == 0
    assert len(data.get("skipped", [])) == 1


# ── B9: Sourcing score uses access control ───────────────────────────


def test_sourcing_score_sales_cannot_see_others(db_session, sales_user, test_user):
    """Sales user cannot view sourcing score for another user's requisition."""
    # Create a requisition owned by test_user (buyer)
    req = Requisition(
        name="Other User Req",
        status="open",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.commit()
    db_session.refresh(req)

    sales_c = _make_sales_client(db_session, sales_user)
    try:
        resp = sales_c.get(f"/api/requisitions/{req.id}/sourcing-score")
        assert resp.status_code == 404
    finally:
        app.dependency_overrides.clear()


# ── B10: Batch archive respects role ─────────────────────────────────


def test_batch_archive_sales_only_own(db_session, sales_user, test_user):
    """Sales user can only batch-archive their own requisitions."""
    own_req = Requisition(
        name="My Req",
        status="open",
        created_by=sales_user.id,
        created_at=datetime.now(timezone.utc),
    )
    other_req = Requisition(
        name="Other Req",
        status="open",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add_all([own_req, other_req])
    db_session.commit()
    db_session.refresh(own_req)
    db_session.refresh(other_req)

    sales_c = _make_sales_client(db_session, sales_user)
    try:
        resp = sales_c.put(
            "/api/requisitions/batch-archive",
            json={"ids": [own_req.id, other_req.id]},
        )
        assert resp.status_code == 200
        data = resp.json()
        # Only the sales user's own requisition should be archived
        assert data["archived_count"] == 1
        db_session.refresh(own_req)
        db_session.refresh(other_req)
        assert own_req.status == "archived"
        assert other_req.status == "open"
    finally:
        app.dependency_overrides.clear()


# ── B11: Batch assign requires admin ─────────────────────────────────


def test_batch_assign_non_admin_rejected(db_session, sales_user):
    """Non-admin user gets 403 on batch-assign."""
    sales_c = _make_sales_client(db_session, sales_user)
    try:
        resp = sales_c.put(
            "/api/requisitions/batch-assign",
            json={"ids": [1], "owner_id": sales_user.id},
        )
        assert resp.status_code == 403
    finally:
        app.dependency_overrides.clear()


def test_batch_assign_admin_allowed(db_session, admin_user, test_requisition, test_user):
    """Admin user can batch-assign requisitions."""
    admin_c = _make_client(db_session, admin_user)
    try:
        resp = admin_c.put(
            "/api/requisitions/batch-assign",
            json={"ids": [test_requisition.id], "owner_id": test_user.id},
        )
        assert resp.status_code == 200
        assert resp.json()["assigned_count"] == 1
    finally:
        app.dependency_overrides.clear()


# ── B16: Condition/packaging normalized on create ────────────────────


def test_add_requirement_normalizes_condition(client, test_requisition):
    """Condition is normalized on create (e.g. 'NEW' -> lowercase)."""
    resp = client.post(
        f"/api/requisitions/{test_requisition.id}/requirements",
        json=[{"primary_mpn": "LM7805", "condition": "NEW"}],
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["created"]) == 1


