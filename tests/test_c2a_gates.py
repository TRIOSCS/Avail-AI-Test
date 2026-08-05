"""test_c2a_gates.py — QP section gate routing + admin grants.

Covers:
  - route_request for the QP_SALES gate routes to can_approve_qp_sales holders; for the
    QP_PURCHASING gate to can_approve_qp_purchasing holders (step rule=ANY, recipients
    PENDING), with no amount check. No eligible approver raises NoEligibleApproverError.
    (These gates still exist on the engine for any future routed use; the QP UI no
    longer submits to them — W3.7 dropped the Mark-Reviewed ceremony outright, and
    section locking now rides qp_workspace.can_edit_qp_section, pinned in
    tests/test_qp_lock_matrix.py.)
  - the deal-level PURCHASE_ORDER gate routes to can_approve_purchase_orders holders,
    filtered by their optional dollar limit.
  - the admin toggle endpoints flip the respective can_approve_* column + write an audit.

Called by: pytest
Depends on: conftest (db_session), app.services.approvals.routing,
            app.models.{approvals,auth}, app.constants.
"""

import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.constants import (
    ApprovalGateType,
    ApprovalRecipientStatus,
    ApprovalStepRule,
    UserAuditAction,
)
from app.models import UserAdminAudit
from app.models.approvals import ApprovalRequest
from app.models.auth import User
from app.services.approvals.routing import NoEligibleApproverError, route_request

# ── Helpers ─────────────────────────────────────────────────────────────


def _make_user(
    db: Session,
    *,
    can_approve_qp_sales: bool = False,
    can_approve_qp_purchasing: bool = False,
    can_approve_purchase_orders: bool = False,
    is_active: bool = True,
    role: str = "buyer",
) -> User:
    u = User(
        email=f"c2a-{uuid.uuid4().hex[:8]}@test.com",
        name="C2a User",
        role=role,
        azure_id=f"azure-c2a-{uuid.uuid4().hex[:8]}",
        is_active=is_active,
        can_approve_qp_sales=can_approve_qp_sales,
        can_approve_qp_purchasing=can_approve_qp_purchasing,
        can_approve_purchase_orders=can_approve_purchase_orders,
        created_at=datetime.now(UTC),
    )
    db.add(u)
    db.flush()
    return u


def _make_request(db: Session, gate: ApprovalGateType) -> ApprovalRequest:
    req = ApprovalRequest(gate_type=gate, amount=None)
    db.add(req)
    db.flush()
    return req


# ── route_request: QP_SALES ───────────────────────────────────────────


def test_route_sales_order_routes_to_sales_approvers(db_session: Session) -> None:
    """QP_SALES routes to every active user with can_approve_qp_sales=True."""
    alice = _make_user(db_session, can_approve_qp_sales=True)
    bob = _make_user(db_session, can_approve_qp_sales=True)
    _make_user(db_session, can_approve_qp_sales=False)  # not routed
    _make_user(db_session, can_approve_qp_purchasing=True)  # wrong gate toggle — not routed

    req = _make_request(db_session, ApprovalGateType.QP_SALES)
    step = route_request(db_session, req)

    assert step.rule == ApprovalStepRule.ANY
    assert {r.user_id for r in step.recipients} == {alice.id, bob.id}
    assert all(r.status == ApprovalRecipientStatus.PENDING for r in step.recipients)


def test_route_sales_order_ignores_inactive(db_session: Session) -> None:
    """An inactive sales approver is not routed."""
    active = _make_user(db_session, can_approve_qp_sales=True)
    _make_user(db_session, can_approve_qp_sales=True, is_active=False)

    step = route_request(db_session, _make_request(db_session, ApprovalGateType.QP_SALES))
    assert {r.user_id for r in step.recipients} == {active.id}


# ── route_request: QP_PURCHASING ────────────────────────────────────────


def test_route_qp_purchasing_routes_to_purchasing_approvers(db_session: Session) -> None:
    """QP_PURCHASING routes to every active user with can_approve_qp_purchasing=True."""
    carol = _make_user(db_session, can_approve_qp_purchasing=True)
    _make_user(db_session, can_approve_qp_purchasing=False)  # not routed
    _make_user(db_session, can_approve_qp_sales=True)  # wrong gate toggle — not routed

    req = _make_request(db_session, ApprovalGateType.QP_PURCHASING)
    step = route_request(db_session, req)

    assert {r.user_id for r in step.recipients} == {carol.id}
    assert all(r.status == ApprovalRecipientStatus.PENDING for r in step.recipients)


# ── No eligible approver raises ──────────────────────────────────────────


def test_route_sales_order_no_approver_raises(db_session: Session) -> None:
    """No can_approve_qp_sales holder → NoEligibleApproverError."""
    _make_user(db_session, can_approve_qp_sales=False)
    with pytest.raises(NoEligibleApproverError):
        route_request(db_session, _make_request(db_session, ApprovalGateType.QP_SALES))


def test_route_qp_purchasing_no_approver_raises(db_session: Session) -> None:
    """No can_approve_qp_purchasing holder → NoEligibleApproverError."""
    _make_user(db_session, can_approve_qp_purchasing=False)
    with pytest.raises(NoEligibleApproverError):
        route_request(db_session, _make_request(db_session, ApprovalGateType.QP_PURCHASING))


# ── route_request: deal-level PURCHASE_ORDER gate ─────────────────────────


def test_route_purchase_order_routes_within_dollar_limit(db_session: Session) -> None:
    """The deal-level PURCHASE_ORDER gate routes to can_approve_purchase_orders holders,
    filtered by their optional dollar limit (mirrors the prepayment amount-filter)."""
    from decimal import Decimal

    unlimited = _make_user(db_session, can_approve_purchase_orders=True)  # limit NULL
    small = _make_user(db_session, can_approve_purchase_orders=True)
    small.purchase_order_approval_limit = Decimal("1000")
    _make_user(db_session, can_approve_purchase_orders=False)  # not routed
    _make_user(db_session, can_approve_qp_purchasing=True)  # wrong gate toggle — not routed
    db_session.flush()

    req = ApprovalRequest(gate_type=ApprovalGateType.PURCHASE_ORDER, amount=Decimal("2500"))
    db_session.add(req)
    db_session.flush()
    step = route_request(db_session, req)

    # $2,500 > small's $1,000 cap → only the unlimited approver is eligible.
    assert {r.user_id for r in step.recipients} == {unlimited.id}


def test_route_purchase_order_no_approver_raises(db_session: Session) -> None:
    """No can_approve_purchase_orders holder → NoEligibleApproverError."""
    _make_user(db_session, can_approve_purchase_orders=False)
    with pytest.raises(NoEligibleApproverError):
        route_request(db_session, _make_request(db_session, ApprovalGateType.PURCHASE_ORDER))


# ── Admin toggle endpoints flip the column ───────────────────────────────


@pytest.fixture()
def admin_client(db_session: Session):
    """TestClient authenticated as an admin (require_admin satisfied)."""
    from app.database import get_db
    from app.dependencies import require_admin, require_user
    from app.main import app

    admin = _make_user(db_session, role="admin")

    def _db():
        yield db_session

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[require_user] = lambda: admin
    app.dependency_overrides[require_admin] = lambda: admin
    try:
        yield TestClient(app), admin
    finally:
        for dep in (get_db, require_user, require_admin):
            app.dependency_overrides.pop(dep, None)


def _audit_rows(db: Session, action) -> list[UserAdminAudit]:
    return db.query(UserAdminAudit).filter_by(action=str(action)).all()


def test_set_sales_order_approver_grants_and_audits(admin_client, db_session: Session) -> None:
    """The sales-order-approver endpoint flips can_approve_qp_sales + audits."""
    client, _admin = admin_client
    target = _make_user(db_session)

    r = client.post(f"/api/admin/users/{target.id}/sales-order-approver", data={"can_approve": "true"})
    assert r.status_code == 200
    db_session.refresh(target)
    assert target.can_approve_qp_sales is True
    assert len(_audit_rows(db_session, UserAuditAction.APPROVAL_GRANT)) == 1


def test_set_sales_order_approver_revokes(admin_client, db_session: Session) -> None:
    """Revoking flips the column back and writes a revoke audit row."""
    client, _admin = admin_client
    target = _make_user(db_session, can_approve_qp_sales=True)

    r = client.post(f"/api/admin/users/{target.id}/sales-order-approver", data={"can_approve": "false"})
    assert r.status_code == 200
    db_session.refresh(target)
    assert target.can_approve_qp_sales is False
    assert len(_audit_rows(db_session, UserAuditAction.APPROVAL_REVOKE)) == 1


def test_set_po_approver_grants_and_audits(admin_client, db_session: Session) -> None:
    """The po-approver endpoint flips can_approve_qp_purchasing + audits."""
    client, _admin = admin_client
    target = _make_user(db_session)

    r = client.post(f"/api/admin/users/{target.id}/po-approver", data={"can_approve": "true"})
    assert r.status_code == 200
    db_session.refresh(target)
    assert target.can_approve_qp_purchasing is True
    assert len(_audit_rows(db_session, UserAuditAction.APPROVAL_GRANT)) == 1
