"""test_buyplan_approver.py — Per-user buy-plan approval right (feat/roles-manager-
approval).

Covers the two halves of the contract a separate buy-plan-approval-UI task depends on:

1. The admin toggle route POST /api/admin/users/{id}/buyplan-approver — grant/revoke the
   User.can_approve_buy_plans column, the APPROVAL_GRANT/APPROVAL_REVOKE audit row, the
   no-op (state unchanged) path, admin-only gating (buyer AND manager get 403), and the
   agent service account being uneditable (404).

2. The dependency dependencies.require_buyplan_approver — 403 when the flag is false,
   pass-through (returns the user) when true — plus the plain predicate
   dependencies.can_approve_buy_plans.

The admin-auth pattern mirrors tests/test_user_management.py: a TestClient whose
require_user/require_admin/get_db are overridden to the admin + test session; for the 403
path we monkeypatch app.dependencies.require_user so the REAL require_admin role check runs.

Called by: pytest
Depends on: app.routers.admin.users, app.dependencies, app.models (User, UserAdminAudit), conftest
"""

from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.constants import UserAuditAction
from app.dependencies import (
    can_approve_buy_plans,
    can_approve_purchase_orders,
    require_buyplan_approver,
    require_buyplan_po_approver,
)
from app.models import User, UserAdminAudit

_AGENT_EMAIL = "agent@availai.local"


# ── Helpers / fixtures ───────────────────────────────────────────────


def _make_user(db, *, email, role="buyer", can_approve=False, is_active=True):
    u = User(
        email=email,
        name=email.split("@")[0],
        role=role,
        is_active=is_active,
        can_approve_buy_plans=can_approve,
        created_at=datetime.now(timezone.utc),
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


@pytest.fixture()
def admin_client(db_session, admin_user):
    """TestClient authenticated as the admin user (require_admin satisfied)."""
    from app.database import get_db
    from app.dependencies import require_admin, require_user
    from app.main import app

    def _db():
        yield db_session

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[require_user] = lambda: admin_user
    app.dependency_overrides[require_admin] = lambda: admin_user
    try:
        yield TestClient(app)
    finally:
        for dep in (get_db, require_user, require_admin):
            app.dependency_overrides.pop(dep, None)


def _non_admin_client(db_session, user, monkeypatch):
    """TestClient where the REAL require_admin runs against *user* (non-admin → 403)."""
    from app.database import get_db
    from app.main import app

    monkeypatch.setattr("app.dependencies.require_user", lambda request, db: user)
    app.dependency_overrides[get_db] = lambda: db_session
    return TestClient(app), get_db


def _audit_rows(db, action):
    return db.query(UserAdminAudit).filter_by(action=str(action)).all()


# ── Toggle route ─────────────────────────────────────────────────────


class TestBuyplanApproverToggle:
    def test_grant_sets_flag_and_audits(self, admin_client, db_session):
        target = _make_user(db_session, email="grant@trioscs.com", can_approve=False)

        resp = admin_client.post(f"/api/admin/users/{target.id}/buyplan-approver", data={"can_approve": "true"})

        assert resp.status_code == 200
        db_session.refresh(target)
        assert target.can_approve_buy_plans is True
        rows = _audit_rows(db_session, UserAuditAction.APPROVAL_GRANT)
        assert len(rows) == 1 and rows[0].target_user_id == target.id

    def test_revoke_clears_flag_and_audits(self, admin_client, db_session):
        target = _make_user(db_session, email="revoke@trioscs.com", can_approve=True)

        resp = admin_client.post(f"/api/admin/users/{target.id}/buyplan-approver", data={"can_approve": "false"})

        assert resp.status_code == 200
        db_session.refresh(target)
        assert target.can_approve_buy_plans is False
        assert len(_audit_rows(db_session, UserAuditAction.APPROVAL_REVOKE)) == 1

    def test_no_op_when_state_unchanged_writes_no_audit(self, admin_client, db_session):
        target = _make_user(db_session, email="noop@trioscs.com", can_approve=True)

        resp = admin_client.post(f"/api/admin/users/{target.id}/buyplan-approver", data={"can_approve": "true"})

        assert resp.status_code == 200
        db_session.refresh(target)
        assert target.can_approve_buy_plans is True
        assert _audit_rows(db_session, UserAuditAction.APPROVAL_GRANT) == []

    def test_agent_account_is_uneditable_404(self, admin_client, db_session):
        agent = _make_user(db_session, email=_AGENT_EMAIL, role="agent")

        resp = admin_client.post(f"/api/admin/users/{agent.id}/buyplan-approver", data={"can_approve": "true"})

        assert resp.status_code == 404
        db_session.refresh(agent)
        assert agent.can_approve_buy_plans is False

    def test_refreshed_partial_reflects_grant(self, admin_client, db_session):
        target = _make_user(db_session, email="render@trioscs.com", can_approve=False)

        resp = admin_client.post(f"/api/admin/users/{target.id}/buyplan-approver", data={"can_approve": "true"})

        # The returned Users partial carries the toggle for the target user.
        assert f"/api/admin/users/{target.id}/buyplan-approver" in resp.text

    @pytest.mark.parametrize("role", ["buyer", "manager"])
    def test_non_admin_gets_403(self, db_session, monkeypatch, role):
        actor = _make_user(db_session, email=f"{role}@trioscs.com", role=role)
        target = _make_user(db_session, email=f"t-{role}@trioscs.com")
        client, get_db = _non_admin_client(db_session, actor, monkeypatch)
        try:
            resp = client.post(f"/api/admin/users/{target.id}/buyplan-approver", data={"can_approve": "true"})
            assert resp.status_code == 403
            db_session.refresh(target)
            assert target.can_approve_buy_plans is False
        finally:
            from app.main import app

            app.dependency_overrides.pop(get_db, None)


# ── Dependency + predicate ───────────────────────────────────────────


class TestRequireBuyplanApprover:
    def test_predicate_true_when_flag_set(self, db_session):
        u = _make_user(db_session, email="pred-true@trioscs.com", can_approve=True)
        assert can_approve_buy_plans(u) is True

    def test_predicate_false_when_flag_unset(self, db_session):
        u = _make_user(db_session, email="pred-false@trioscs.com", can_approve=False)
        assert can_approve_buy_plans(u) is False

    def test_predicate_false_for_none(self):
        assert can_approve_buy_plans(None) is False

    def test_predicate_false_for_admin_without_flag(self, db_session):
        # Role does NOT auto-qualify: the column is the single source of truth.
        u = _make_user(db_session, email="admin-noflag@trioscs.com", role="admin", can_approve=False)
        assert can_approve_buy_plans(u) is False

    def test_dependency_passes_when_flag_set(self, db_session, monkeypatch):
        u = _make_user(db_session, email="dep-ok@trioscs.com", can_approve=True)
        monkeypatch.setattr("app.dependencies.require_user", lambda request, db: u)
        # request/db are unused once require_user is patched.
        assert require_buyplan_approver(request=None, db=db_session) is u

    def test_dependency_403_when_flag_unset(self, db_session, monkeypatch):
        u = _make_user(db_session, email="dep-403@trioscs.com", can_approve=False)
        monkeypatch.setattr("app.dependencies.require_user", lambda request, db: u)
        with pytest.raises(HTTPException) as exc:
            require_buyplan_approver(request=None, db=db_session)
        assert exc.value.status_code == 403


# ── Phase D: purchase-order approval right (verify-PO gate) ───────────


class TestPurchaseOrderApprovalRight:
    """can_approve_purchase_orders predicate + require_buyplan_po_approver
    dependency."""

    def test_predicate_true_when_flag_set(self, db_session):
        u = _make_user(db_session, email="po-true@trioscs.com")
        u.can_approve_purchase_orders = True
        db_session.commit()
        assert can_approve_purchase_orders(u) is True

    def test_predicate_false_when_flag_unset(self, db_session):
        u = _make_user(db_session, email="po-false@trioscs.com")
        assert can_approve_purchase_orders(u) is False

    def test_predicate_false_for_none(self):
        assert can_approve_purchase_orders(None) is False

    def test_predicate_false_for_admin_without_flag(self, db_session):
        # Role does NOT auto-qualify: the column is the single source of truth.
        u = _make_user(db_session, email="po-admin-noflag@trioscs.com", role="admin")
        assert can_approve_purchase_orders(u) is False

    def test_dependency_passes_when_flag_set(self, db_session, monkeypatch):
        u = _make_user(db_session, email="po-dep-ok@trioscs.com")
        u.can_approve_purchase_orders = True
        db_session.commit()
        monkeypatch.setattr("app.dependencies.require_user", lambda request, db: u)
        assert require_buyplan_po_approver(request=None, db=db_session) is u

    def test_dependency_403_when_flag_unset(self, db_session, monkeypatch):
        u = _make_user(db_session, email="po-dep-403@trioscs.com")
        monkeypatch.setattr("app.dependencies.require_user", lambda request, db: u)
        with pytest.raises(HTTPException) as exc:
            require_buyplan_po_approver(request=None, db=db_session)
        assert exc.value.status_code == 403
