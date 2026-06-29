"""test_sp3_po_receiving.py — Approvals SP-3: deal-level PO gate + receiving.

Covers the SP-3 contract (mirrors the C1 buy-plan-gate pattern):
  - a buy plan whose total clears settings.po_auto_approve_threshold opens a deal-level
    PURCHASE_ORDER ApprovalRequest (subject=plan, amount=plan total) when its BUY_PLAN gate
    is approved; a plan below the threshold auto-skips the PO gate;
  - approving the PURCHASE_ORDER gate moves the plan ACTIVE → INBOUND via decide() — and the
    BUY_PLAN side-effect block does NOT fire for it (gate_type discrimination);
  - receive_buy_plan completes an INBOUND plan (→ COMPLETED + case report) and rejects any
    other status; the /receive route drives it.

Called by: pytest
Depends on: conftest (db_session), app.services.buyplan_workflow,
            app.services.approvals.service, app.models.{approvals,buy_plan,auth,quotes,sourcing}.
"""

import contextlib
import uuid
from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.constants import (
    ApprovalGateType,
    ApprovalRequestStatus,
    ApprovalSubjectType,
    BuyPlanStatus,
)
from app.database import get_db
from app.dependencies import require_user
from app.models.approvals import ApprovalRequest
from app.models.auth import User
from app.models.buy_plan import BuyPlan
from app.models.quotes import Quote
from app.models.sourcing import Requisition
from app.services.approvals.service import decide as svc_decide
from app.services.buyplan_workflow import receive_buy_plan, submit_buy_plan

# ── Helpers ─────────────────────────────────────────────────────────────


def _make_approver(db: Session) -> User:
    """A user who can approve BOTH the buy-plan gate and the deal-level PO gate."""
    u = User(
        email=f"sp3-{uuid.uuid4().hex[:6]}@test.com",
        name="SP3 Approver",
        role="admin",
        azure_id=f"azure-sp3-{uuid.uuid4().hex[:8]}",
        can_approve_buy_plans=True,
        can_approve_purchase_orders=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(u)
    db.flush()
    return u


def _make_draft_plan(db: Session, user: User, *, total_cost: float) -> BuyPlan:
    req = Requisition(
        name=f"REQ-SP3-{uuid.uuid4().hex[:6]}",
        customer_name="SP3Co",
        status="active",
        created_by=user.id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(req)
    db.flush()
    quote = Quote(
        requisition_id=req.id,
        quote_number=f"QSP3-{uuid.uuid4().hex[:8]}",
        line_items=[],
        status="sent",
        created_by_id=user.id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(quote)
    db.flush()
    bp = BuyPlan(
        requisition_id=req.id,
        quote_id=quote.id,
        status=BuyPlanStatus.DRAFT.value,
        so_status="pending",
        total_cost=total_cost,
    )
    db.add(bp)
    db.flush()
    return bp


def _gate(db: Session, plan_id: int, gate_type: ApprovalGateType) -> list[ApprovalRequest]:
    return list(
        db.execute(
            select(ApprovalRequest).where(
                ApprovalRequest.subject_type == ApprovalSubjectType.BUY_PLAN,
                ApprovalRequest.subject_id == plan_id,
                ApprovalRequest.gate_type == gate_type,
            )
        ).scalars()
    )


def _approve_buy_plan_gate(db: Session, plan: BuyPlan, approver: User) -> None:
    """Submit + approve the BUY_PLAN gate so the plan reaches ACTIVE."""
    submit_buy_plan(plan.id, f"SO-{uuid.uuid4().hex[:6]}", approver, db)
    bp_req = _gate(db, plan.id, ApprovalGateType.BUY_PLAN)[0]
    svc_decide(db, bp_req.id, approver, "approve", comment="ok")


# ── PO gate opens / auto-skips on buy-plan approval ───────────────────────


def test_buyplan_approval_over_threshold_opens_po_gate(db_session: Session) -> None:
    """Approving a plan over po_auto_approve_threshold opens a PURCHASE_ORDER gate
    routed to the PO approver, with the plan total as the amount; the plan stays
    ACTIVE."""
    approver = _make_approver(db_session)
    plan = _make_draft_plan(db_session, approver, total_cost=10_000.0)

    _approve_buy_plan_gate(db_session, plan, approver)

    db_session.refresh(plan)
    assert plan.status == BuyPlanStatus.ACTIVE.value
    po_reqs = _gate(db_session, plan.id, ApprovalGateType.PURCHASE_ORDER)
    assert len(po_reqs) == 1
    assert po_reqs[0].status == ApprovalRequestStatus.REQUESTED
    assert float(po_reqs[0].amount) == 10_000.0


def test_buyplan_approval_under_threshold_skips_po_gate(db_session: Session) -> None:
    """A plan below the threshold auto-skips the PO gate: ACTIVE, no PURCHASE_ORDER request."""
    approver = _make_approver(db_session)
    plan = _make_draft_plan(db_session, approver, total_cost=1_000.0)

    _approve_buy_plan_gate(db_session, plan, approver)

    db_session.refresh(plan)
    assert plan.status == BuyPlanStatus.ACTIVE.value
    assert _gate(db_session, plan.id, ApprovalGateType.PURCHASE_ORDER) == []


# ── PO gate approval → INBOUND (gate_type discrimination) ──────────────────


def test_po_gate_approve_moves_plan_to_inbound(db_session: Session) -> None:
    """Approving the PURCHASE_ORDER gate moves the plan ACTIVE → INBOUND.

    Proves the BUY_PLAN side-effect block does NOT fire for a PO-gate decision (else it
    would raise on the non-PENDING plan).
    """
    approver = _make_approver(db_session)
    plan = _make_draft_plan(db_session, approver, total_cost=10_000.0)
    _approve_buy_plan_gate(db_session, plan, approver)
    po_req = _gate(db_session, plan.id, ApprovalGateType.PURCHASE_ORDER)[0]

    resolved = svc_decide(db_session, po_req.id, approver, "approve", comment="cut it")

    assert resolved.status == ApprovalRequestStatus.APPROVED
    db_session.refresh(plan)
    assert plan.status == BuyPlanStatus.INBOUND.value


# ── Receiving completes the plan ──────────────────────────────────────────


def test_receive_buy_plan_completes_from_inbound(db_session: Session) -> None:
    """receive_buy_plan moves an INBOUND plan to COMPLETED and writes a case report."""
    approver = _make_approver(db_session)
    plan = _make_draft_plan(db_session, approver, total_cost=10_000.0)
    plan.status = BuyPlanStatus.INBOUND.value
    db_session.flush()

    received = receive_buy_plan(plan.id, approver, db_session)

    assert received.status == BuyPlanStatus.COMPLETED.value
    assert received.completed_at is not None
    assert received.case_report  # the shared _complete_plan generated it


def test_receive_buy_plan_rejects_non_inbound(db_session: Session) -> None:
    """An ACTIVE (not INBOUND) plan cannot be marked received."""
    approver = _make_approver(db_session)
    plan = _make_draft_plan(db_session, approver, total_cost=1_000.0)
    plan.status = BuyPlanStatus.ACTIVE.value
    db_session.flush()

    raised = False
    try:
        receive_buy_plan(plan.id, approver, db_session)
    except ValueError:
        raised = True
    assert raised, "receiving a non-inbound plan must raise"


# ── Router: /receive completes an inbound plan ────────────────────────────


@contextlib.contextmanager
def _client(db: Session, user: User):
    from app.main import app

    def _db():
        yield db

    overrides = {get_db: _db, require_user: lambda: user}
    app.dependency_overrides.update(overrides)
    try:
        yield TestClient(app, raise_server_exceptions=True)
    finally:
        for key in overrides:
            app.dependency_overrides.pop(key, None)


def test_receive_route_completes_inbound_plan(db_session: Session) -> None:
    """POST /v2/partials/buy-plans/{id}/receive completes an INBOUND plan (200)."""
    approver = _make_approver(db_session)
    plan = _make_draft_plan(db_session, approver, total_cost=10_000.0)
    plan.status = BuyPlanStatus.INBOUND.value
    db_session.commit()

    with _client(db_session, approver) as client:
        resp = client.post(f"/v2/partials/buy-plans/{plan.id}/receive")

    assert resp.status_code == 200
    db_session.expire_all()
    assert db_session.get(BuyPlan, plan.id).status == BuyPlanStatus.COMPLETED.value
