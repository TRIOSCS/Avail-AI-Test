"""test_c2a_gates.py — QP section gate routing + admin grants + Mark-Reviewed fold.

Covers:
  - route_request for the QP_SALES gate routes to can_approve_qp_sales holders; for the
    QP_PURCHASING gate to can_approve_qp_purchasing holders (step rule=ANY, recipients
    PENDING), with no amount check. No eligible approver raises NoEligibleApproverError.
    (These gates still exist on the engine for any future routed use; the QP UI no longer
    submits to them — decision C folded section sign-off into an instant Mark-Reviewed
    toggle.)
  - the deal-level PURCHASE_ORDER gate routes to can_approve_purchase_orders holders,
    filtered by their optional dollar limit.
  - the admin toggle endpoints flip the respective can_approve_* column + write an audit.
  - toggle_section_reviewed (decision C): mark stamps reviewed_at/by + logs an activity,
    unmark clears both, an incomplete section blocks the mark, and the section review
    right is required.

Called by: pytest
Depends on: conftest (db_session), app.services.approvals.routing,
            app.services.quality_plan_service, app.models.{approvals,auth,quality_plan,
            buy_plan,quotes,sourcing}, app.constants.
"""

import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.constants import (
    ActivityType,
    ApprovalGateType,
    ApprovalRecipientStatus,
    ApprovalStepRule,
    BuyPlanStatus,
    UserAuditAction,
)
from app.models import ActivityLog, UserAdminAudit
from app.models.approvals import ApprovalRequest
from app.models.auth import User
from app.models.buy_plan import BuyPlan
from app.models.quality_plan import QualityPlan
from app.models.quotes import Quote
from app.models.sourcing import Requisition
from app.services.approvals.routing import NoEligibleApproverError, route_request
from app.services.quality_plan_service import IncompleteQPError, toggle_section_reviewed

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
        created_at=datetime.now(timezone.utc),
    )
    db.add(u)
    db.flush()
    return u


def _make_qp(db: Session, owner: User) -> QualityPlan:
    """A QP linked to a draft buy plan (so buy_plan_id is set for activity logging).

    Both the Sales and Purchasing sections are filled to their completeness gate so the
    Mark-Reviewed path validates clean; individual tests blank a field to exercise the
    incomplete path.
    """
    req = Requisition(
        name=f"REQ-C2A-{uuid.uuid4().hex[:6]}",
        customer_name="C2ACo",
        status="active",
        created_by=owner.id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(req)
    db.flush()

    quote = Quote(
        requisition_id=req.id,
        quote_number=f"QC2A-{uuid.uuid4().hex[:8]}",
        line_items=[],
        status="sent",
        created_by_id=owner.id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(quote)
    db.flush()

    bp = BuyPlan(
        requisition_id=req.id,
        quote_id=quote.id,
        status=BuyPlanStatus.DRAFT.value,
        so_status="pending",
        total_cost=1000.0,
        sales_order_number="TSO0190738",  # canonical SO# lives on the buy plan (SP-2)
    )
    db.add(bp)
    db.flush()

    qp = QualityPlan(
        buy_plan_id=bp.id,
        created_by_id=owner.id,
        order_type="new",
        status="draft",
        sales_condition="New",
        sales_quantity=10,
        sales_product_commodity="HDD",
        sales_testing_required=True,
        purchasing_po_number="PO-12345",
        purchasing_condition="New",
        purchasing_product_commodity="HDD",
        purchasing_testing_required=True,
    )
    db.add(qp)
    db.flush()
    return qp


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


# ── toggle_section_reviewed: the decision-C lightweight fold ──────────────


def test_mark_reviewed_stamps_reviewed_by_and_at(db_session: Session) -> None:
    """Mark stamps reviewed_at + reviewed_by_id and logs one QP_SECTION_REVIEWED row."""
    reviewer = _make_user(db_session, can_approve_qp_sales=True)
    qp = _make_qp(db_session, reviewer)

    toggle_section_reviewed(db_session, qp.id, ApprovalGateType.QP_SALES, "mark", reviewer)

    db_session.refresh(qp)
    assert qp.sales_section_reviewed_at is not None
    assert qp.sales_section_reviewed_by_id == reviewer.id
    assert qp.purchasing_section_reviewed_at is None  # unaffected
    logs = (
        db_session.execute(
            select(ActivityLog).where(
                ActivityLog.activity_type == ActivityType.QP_SECTION_REVIEWED,
                ActivityLog.buy_plan_id == qp.buy_plan_id,
            )
        )
        .scalars()
        .all()
    )
    assert any("marked reviewed" in (lg.notes or "") for lg in logs)


def test_unmark_reviewed_clears_stamp_and_reopens_form(db_session: Session) -> None:
    """Unmark clears both reviewed stamps (re-opening the section for editing)."""
    reviewer = _make_user(db_session, can_approve_qp_sales=True)
    qp = _make_qp(db_session, reviewer)
    toggle_section_reviewed(db_session, qp.id, ApprovalGateType.QP_SALES, "mark", reviewer)

    toggle_section_reviewed(db_session, qp.id, ApprovalGateType.QP_SALES, "unmark", reviewer)

    db_session.refresh(qp)
    assert qp.sales_section_reviewed_at is None
    assert qp.sales_section_reviewed_by_id is None


def test_mark_reviewed_blocked_by_incomplete_section(db_session: Session) -> None:
    """An incomplete section raises IncompleteQPError and stamps nothing."""
    reviewer = _make_user(db_session, can_approve_qp_sales=True)
    qp = _make_qp(db_session, reviewer)
    qp.sales_condition = None  # blank a required Sales field
    db_session.flush()

    with pytest.raises(IncompleteQPError):
        toggle_section_reviewed(db_session, qp.id, ApprovalGateType.QP_SALES, "mark", reviewer)

    db_session.refresh(qp)
    assert qp.sales_section_reviewed_at is None


def test_mark_reviewed_requires_review_right(db_session: Session) -> None:
    """A user without the section review right → PermissionError, nothing stamped."""
    no_right = _make_user(db_session, can_approve_qp_sales=False)
    qp = _make_qp(db_session, no_right)

    with pytest.raises(PermissionError):
        toggle_section_reviewed(db_session, qp.id, ApprovalGateType.QP_SALES, "mark", no_right)

    db_session.refresh(qp)
    assert qp.sales_section_reviewed_at is None


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
