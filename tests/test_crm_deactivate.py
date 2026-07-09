"""Tests for account deactivate/reactivate endpoints.

Verifies that:
- Owner or manager/admin can deactivate an active company → is_active=False
- Owner or manager/admin can reactivate an archived company → is_active=True
- Sales rep with no ownership relation → 403 on deactivate
- Re-renders company detail partial on success

Called by: pytest
Depends on: app.routers.htmx_views (deactivate_company, reactivate_company),
            app.dependencies.can_manage_account_team
"""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Company, User


@pytest.fixture()
def owned_company(db_session: Session, sales_user: User) -> Company:
    """A company whose account_owner_id is the sales_user."""
    co = Company(
        name="Owned Corp",
        is_active=True,
        account_owner_id=None,  # will be set after sales_user is created
        created_at=datetime.now(UTC),
    )
    db_session.add(co)
    db_session.flush()
    co.account_owner_id = sales_user.id
    db_session.commit()
    db_session.refresh(co)
    return co


@pytest.fixture()
def unowned_company(db_session: Session) -> Company:
    """A company with no owner — sales_user has no relation to it."""
    co = Company(
        name="Unowned Corp",
        is_active=True,
        created_at=datetime.now(UTC),
    )
    db_session.add(co)
    db_session.commit()
    db_session.refresh(co)
    return co


def _make_client_for_user(db_session: Session, user: User) -> TestClient:
    """Build a TestClient that authenticates as *user*."""
    from app.database import get_db
    from app.dependencies import require_admin, require_buyer, require_fresh_token, require_user
    from app.main import app

    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user] = lambda: user
    app.dependency_overrides[require_admin] = lambda: user
    app.dependency_overrides[require_buyer] = lambda: user
    app.dependency_overrides[require_fresh_token] = lambda: "mock-token"

    client = TestClient(app, raise_server_exceptions=False)
    return client


class TestDeactivateCompany:
    def test_owner_can_deactivate(self, db_session: Session, sales_user: User, owned_company: Company):
        """Account owner can deactivate their own account."""
        c = _make_client_for_user(db_session, sales_user)
        resp = c.post(f"/v2/partials/customers/{owned_company.id}/deactivate")
        assert resp.status_code == 200
        db_session.expire(owned_company)
        assert owned_company.is_active is False

    def test_admin_can_deactivate(self, db_session: Session, admin_user: User, unowned_company: Company):
        """Admin can deactivate any account."""
        c = _make_client_for_user(db_session, admin_user)
        resp = c.post(f"/v2/partials/customers/{unowned_company.id}/deactivate")
        assert resp.status_code == 200
        db_session.expire(unowned_company)
        assert unowned_company.is_active is False

    def test_unrelated_sales_rep_gets_403(self, db_session: Session, sales_user: User, unowned_company: Company):
        """Sales rep with no ownership relation → 403."""
        c = _make_client_for_user(db_session, sales_user)
        resp = c.post(f"/v2/partials/customers/{unowned_company.id}/deactivate")
        assert resp.status_code == 403
        # Company must remain active
        db_session.expire(unowned_company)
        assert unowned_company.is_active is True

    def test_deactivate_nonexistent_company_404(self, db_session: Session, admin_user: User):
        """Deactivating a non-existent company → 404."""
        c = _make_client_for_user(db_session, admin_user)
        resp = c.post("/v2/partials/customers/999999/deactivate")
        assert resp.status_code == 404


class TestReactivateCompany:
    def test_owner_cannot_reactivate(self, db_session: Session, sales_user: User, owned_company: Company):
        """Account owner (non-manager) cannot reactivate — only manager/admin may
        (archive-DNC policy)."""
        owned_company.is_active = False
        db_session.commit()

        c = _make_client_for_user(db_session, sales_user)
        resp = c.post(f"/v2/partials/customers/{owned_company.id}/reactivate")
        assert resp.status_code == 403
        db_session.expire(owned_company)
        assert owned_company.is_active is False

    def test_manager_can_reactivate(self, db_session: Session, manager_user: User, unowned_company: Company):
        """Manager can reactivate any archived account."""
        unowned_company.is_active = False
        db_session.commit()

        c = _make_client_for_user(db_session, manager_user)
        resp = c.post(f"/v2/partials/customers/{unowned_company.id}/reactivate")
        assert resp.status_code == 200
        db_session.expire(unowned_company)
        assert unowned_company.is_active is True

    def test_unrelated_rep_cannot_reactivate(self, db_session: Session, sales_user: User, unowned_company: Company):
        """Sales rep with no ownership → 403 on reactivate."""
        unowned_company.is_active = False
        db_session.commit()

        c = _make_client_for_user(db_session, sales_user)
        resp = c.post(f"/v2/partials/customers/{unowned_company.id}/reactivate")
        assert resp.status_code == 403
        db_session.expire(unowned_company)
        assert unowned_company.is_active is False
