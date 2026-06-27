"""test_c2a_gates.py — QP Phase C2a: Sales-Order and Purchase-Order section gates.

Covers the C2a contract on the shared approvals engine (engine + buy-plan gate already
live in C1):
  - route_request for the SALES_ORDER gate routes to can_approve_qp_sales holders;
    for the PURCHASE_ORDER gate to can_approve_pos holders (step rule=ANY, recipients
    PENDING), with no amount check.
  - no eligible approver raises NoEligibleApproverError, and submit_section surfaces it as
    NoSectionApproverError (the router → inline banner, never a 500) leaving NO orphan
    engine request.
  - submit_section opens the right gate request (subject_type=QUALITY_PLAN, the matching
    gate_type), routed to the approver.
  - decide() on a resolved section request dispatches _on_section_approved (logs an
    activity) — same session, before commit.
  - the two admin toggle endpoints flip the respective column and write an audit row.

Called by: pytest
Depends on: conftest (db_session), app.services.approvals.{routing,service},
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
    ApprovalRequestStatus,
    ApprovalStepRule,
    ApprovalSubjectType,
    BuyPlanStatus,
    UserAuditAction,
)
from app.models import ActivityLog, UserAdminAudit
from app.models.approvals import ApprovalRequest, ApprovalStep, ApprovalStepRecipient
from app.models.auth import User
from app.models.buy_plan import BuyPlan
from app.models.quality_plan import QualityPlan
from app.models.quotes import Quote
from app.models.sourcing import Requisition
from app.services.approvals.routing import NoEligibleApproverError, route_request
from app.services.approvals.service import decide as svc_decide
from app.services.quality_plan_service import (
    NoSectionApproverError,
    _on_section_approved,
    submit_section,
)

# ── Helpers ─────────────────────────────────────────────────────────────


def _make_user(
    db: Session,
    *,
    can_approve_qp_sales: bool = False,
    can_approve_pos: bool = False,
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
        can_approve_pos=can_approve_pos,
        created_at=datetime.now(timezone.utc),
    )
    db.add(u)
    db.flush()
    return u


def _make_qp(db: Session, owner: User) -> QualityPlan:
    """A QP linked to a draft buy plan (so buy_plan_id is set for activity logging)."""
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
    )
    db.add(bp)
    db.flush()

    # C2b adds a per-section completeness gate to submit_section: the Sales section needs
    # SO# + condition + quantity + product commodity + testing-required, and Purchasing
    # needs PO# + condition + product commodity + testing-required, before a gate opens.
    # Fill both so these C2a routing/gate tests exercise the gate-passing path.
    qp = QualityPlan(
        buy_plan_id=bp.id,
        created_by_id=owner.id,
        order_type="new",
        status="draft",
        sales_so_number="TSO0190738",
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


def _section_requests(db: Session, qp_id: int, gate: ApprovalGateType) -> list[ApprovalRequest]:
    return list(
        db.execute(
            select(ApprovalRequest).where(
                ApprovalRequest.subject_type == ApprovalSubjectType.QUALITY_PLAN,
                ApprovalRequest.subject_id == qp_id,
                ApprovalRequest.gate_type == gate,
            )
        ).scalars()
    )


# ── route_request: SALES_ORDER ───────────────────────────────────────────


def test_route_sales_order_routes_to_sales_approvers(db_session: Session) -> None:
    """SALES_ORDER routes to every active user with can_approve_qp_sales=True."""
    alice = _make_user(db_session, can_approve_qp_sales=True)
    bob = _make_user(db_session, can_approve_qp_sales=True)
    _make_user(db_session, can_approve_qp_sales=False)  # not routed
    _make_user(db_session, can_approve_pos=True)  # wrong gate toggle — not routed

    req = _make_request(db_session, ApprovalGateType.SALES_ORDER)
    step = route_request(db_session, req)

    assert step.rule == ApprovalStepRule.ANY
    assert {r.user_id for r in step.recipients} == {alice.id, bob.id}
    assert all(r.status == ApprovalRecipientStatus.PENDING for r in step.recipients)


def test_route_sales_order_ignores_inactive(db_session: Session) -> None:
    """An inactive sales approver is not routed."""
    active = _make_user(db_session, can_approve_qp_sales=True)
    _make_user(db_session, can_approve_qp_sales=True, is_active=False)

    step = route_request(db_session, _make_request(db_session, ApprovalGateType.SALES_ORDER))
    assert {r.user_id for r in step.recipients} == {active.id}


# ── route_request: PURCHASE_ORDER ────────────────────────────────────────


def test_route_purchase_order_routes_to_po_approvers(db_session: Session) -> None:
    """PURCHASE_ORDER routes to every active user with can_approve_pos=True."""
    carol = _make_user(db_session, can_approve_pos=True)
    _make_user(db_session, can_approve_pos=False)  # not routed
    _make_user(db_session, can_approve_qp_sales=True)  # wrong gate toggle — not routed

    req = _make_request(db_session, ApprovalGateType.PURCHASE_ORDER)
    step = route_request(db_session, req)

    assert {r.user_id for r in step.recipients} == {carol.id}
    assert all(r.status == ApprovalRecipientStatus.PENDING for r in step.recipients)


# ── No eligible approver raises ──────────────────────────────────────────


def test_route_sales_order_no_approver_raises(db_session: Session) -> None:
    """No can_approve_qp_sales holder → NoEligibleApproverError."""
    _make_user(db_session, can_approve_qp_sales=False)
    with pytest.raises(NoEligibleApproverError):
        route_request(db_session, _make_request(db_session, ApprovalGateType.SALES_ORDER))


def test_route_purchase_order_no_approver_raises(db_session: Session) -> None:
    """No can_approve_pos holder → NoEligibleApproverError."""
    _make_user(db_session, can_approve_pos=False)
    with pytest.raises(NoEligibleApproverError):
        route_request(db_session, _make_request(db_session, ApprovalGateType.PURCHASE_ORDER))


# ── submit_section: opens the right gate request ─────────────────────────


def test_submit_sales_section_creates_sales_order_request(db_session: Session) -> None:
    """submit_section(SALES_ORDER) opens ONE request on the QP, routed to the
    approver."""
    approver = _make_user(db_session, can_approve_qp_sales=True)
    qp = _make_qp(db_session, approver)

    req = submit_section(db_session, qp.id, ApprovalGateType.SALES_ORDER, approver)

    assert req.gate_type == ApprovalGateType.SALES_ORDER
    assert req.subject_type == ApprovalSubjectType.QUALITY_PLAN
    assert req.subject_id == qp.id
    assert req.status == ApprovalRequestStatus.REQUESTED
    recip = db_session.execute(
        select(ApprovalStepRecipient)
        .join(ApprovalStep, ApprovalStepRecipient.step_id == ApprovalStep.id)
        .where(ApprovalStep.request_id == req.id, ApprovalStepRecipient.user_id == approver.id)
    ).scalar_one()
    assert recip.status == ApprovalRecipientStatus.PENDING


def test_submit_purchasing_section_creates_purchase_order_request(db_session: Session) -> None:
    """submit_section(PURCHASE_ORDER) opens the PURCHASE_ORDER request on the QP."""
    approver = _make_user(db_session, can_approve_pos=True)
    qp = _make_qp(db_session, approver)

    req = submit_section(db_session, qp.id, ApprovalGateType.PURCHASE_ORDER, approver)

    assert req.gate_type == ApprovalGateType.PURCHASE_ORDER
    assert req.subject_type == ApprovalSubjectType.QUALITY_PLAN
    assert req.subject_id == qp.id


def test_submit_section_no_approver_raises_section_error_no_orphan(db_session: Session) -> None:
    """No eligible approver → NoSectionApproverError (router shows banner, not 500), and
    NO orphan ApprovalRequest is left behind."""
    submitter = _make_user(db_session, can_approve_qp_sales=False)
    qp = _make_qp(db_session, submitter)

    with pytest.raises(NoSectionApproverError) as exc:
        submit_section(db_session, qp.id, ApprovalGateType.SALES_ORDER, submitter)
    assert exc.value.section == "Sales"

    # create_request removed the half-built request — no orphan engine state.
    assert _section_requests(db_session, qp.id, ApprovalGateType.SALES_ORDER) == []


# ── decide() dispatches the section on-resolve hook ──────────────────────


def test_decide_sales_section_logs_activity_same_session(db_session: Session) -> None:
    """Decide(approve) on a SALES_ORDER request resolves it AND logs the section
    activity in the same session before commit (via _on_section_approved)."""
    approver = _make_user(db_session, can_approve_qp_sales=True)
    qp = _make_qp(db_session, approver)
    req = submit_section(db_session, qp.id, ApprovalGateType.SALES_ORDER, approver)

    resolved = svc_decide(db_session, req.id, approver, "approve", comment="ok")

    assert resolved.status == ApprovalRequestStatus.APPROVED
    logs = (
        db_session.execute(
            select(ActivityLog).where(
                ActivityLog.activity_type == ActivityType.APPROVAL_APPROVED,
                ActivityLog.buy_plan_id == qp.buy_plan_id,
            )
        )
        .scalars()
        .all()
    )
    assert any("Sales section approved" in (lg.notes or "") for lg in logs)


def test_on_section_approved_missing_qp_is_noop(db_session: Session) -> None:
    """_on_section_approved on a deleted QP is a no-op warning, not an error."""
    _on_section_approved(db_session, 999_999, ApprovalGateType.PURCHASE_ORDER, True)  # must not raise


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
    """The po-approver endpoint flips can_approve_pos + audits."""
    client, _admin = admin_client
    target = _make_user(db_session)

    r = client.post(f"/api/admin/users/{target.id}/po-approver", data={"can_approve": "true"})
    assert r.status_code == 200
    db_session.refresh(target)
    assert target.can_approve_pos is True
    assert len(_audit_rows(db_session, UserAuditAction.APPROVAL_GRANT)) == 1


# ── Submit-section endpoints create the right gate request ───────────────


@pytest.fixture()
def qp_client(db_session: Session):
    """TestClient authenticated as a user who is also a sales+PO approver, with a QP."""
    from app.database import get_db
    from app.dependencies import require_user
    from app.main import app

    user = _make_user(db_session, role="admin", can_approve_qp_sales=True, can_approve_pos=True)
    qp = _make_qp(db_session, user)
    db_session.commit()

    def _db():
        yield db_session

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[require_user] = lambda: user
    try:
        yield TestClient(app), user, qp
    finally:
        for dep in (get_db, require_user):
            app.dependency_overrides.pop(dep, None)


def test_submit_sales_endpoint_creates_request(qp_client, db_session: Session) -> None:
    """POST /v2/qp/{id}/submit-sales opens a SALES_ORDER request and returns 200."""
    client, _user, qp = qp_client

    r = client.post(f"/v2/qp/{qp.id}/submit-sales")
    assert r.status_code == 200
    reqs = _section_requests(db_session, qp.id, ApprovalGateType.SALES_ORDER)
    assert len(reqs) == 1
    assert reqs[0].status == ApprovalRequestStatus.REQUESTED


def test_submit_purchasing_endpoint_creates_request(qp_client, db_session: Session) -> None:
    """POST /v2/qp/{id}/submit-purchasing opens a PURCHASE_ORDER request and returns
    200."""
    client, _user, qp = qp_client

    r = client.post(f"/v2/qp/{qp.id}/submit-purchasing")
    assert r.status_code == 200
    reqs = _section_requests(db_session, qp.id, ApprovalGateType.PURCHASE_ORDER)
    assert len(reqs) == 1


def test_submit_sales_endpoint_no_approver_shows_banner_not_500(db_session: Session) -> None:
    """With no eligible approver, the submit endpoint returns 200 with an inline banner
    (NOT a 500), and leaves no orphan request."""
    from app.database import get_db
    from app.dependencies import require_user
    from app.main import app

    user = _make_user(db_session, role="admin", can_approve_qp_sales=False, can_approve_pos=False)
    qp = _make_qp(db_session, user)
    db_session.commit()

    def _db():
        yield db_session

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[require_user] = lambda: user
    try:
        client = TestClient(app, raise_server_exceptions=True)
        r = client.post(f"/v2/qp/{qp.id}/submit-sales")
    finally:
        for dep in (get_db, require_user):
            app.dependency_overrides.pop(dep, None)

    assert r.status_code == 200
    assert "No approver configured" in r.text
    assert _section_requests(db_session, qp.id, ApprovalGateType.SALES_ORDER) == []
