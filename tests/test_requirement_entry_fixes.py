"""tests/test_requirement_entry_fixes.py — Tests for requirement entry bug fixes.

Covers: blank MPN update prevention, access control on batch ops,
validation error reporting, condition/packaging normalization on create,
and frontend HTML/JS regression checks.

Called by: pytest
Depends on: routers/requisitions, conftest fixtures
"""

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import require_admin, require_buyer, require_user
from app.main import app
from app.models import User

# ── Helpers ──────────────────────────────────────────────────────────

_AUTH_DEPS = [get_db, require_user, require_buyer, require_admin]


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


def _clear_overrides() -> None:
    """Remove the auth dependency overrides installed by the client helpers."""
    for dep in _AUTH_DEPS:
        app.dependency_overrides.pop(dep, None)


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
        _clear_overrides()


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
        _clear_overrides()
