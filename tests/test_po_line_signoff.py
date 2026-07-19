"""test_po_line_signoff.py — Phase 3: per-PO (buy-plan line) sign-off replaces the deal-
level PURCHASE_ORDER engine gate.

Covers the rework contract (replaces the retired tests/test_sp3_po_receiving.py):
  - approving a buy plan NEVER opens a deal-level PURCHASE_ORDER ApprovalRequest,
    regardless of total cost (the gate is retired; the per-line trio is canonical);
  - _complete_plan refuses while any line is PENDING_VERIFY — via check_completion AND
    via a direct _complete_plan call (the stock-sale auto-complete job's path);
  - verify_po enforces the approver's purchase_order_approval_limit against THIS line's
    dollar amount (and can_verify_po_line mirrors the same check for the UI);
  - verify_po writes a durable ActivityLog row (PO_LINE_VERIFIED / PO_LINE_REJECTED);
  - plan_needs_approver_reason's "purchase_order" stall is per PENDING_VERIFY line, not
    the plan total (the workspace BP-tab stall warnings key off this predicate).

Called by: pytest
Depends on: conftest (db_session), app.services.buyplan_workflow,
            app.services.approvals.service, app.dependencies,
            app.models.{approvals,buy_plan,auth,quotes,sourcing,intelligence}.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.constants import (
    ActivityType,
    ApprovalGateType,
    ApprovalSubjectType,
    BuyPlanLineStatus,
    BuyPlanStatus,
    SOVerificationStatus,
)
from app.dependencies import can_verify_po_line
from app.models import ActivityLog
from app.models.approvals import ApprovalRequest
from app.models.auth import User
from app.models.buy_plan import BuyPlan, BuyPlanLine
from app.models.quotes import Quote
from app.models.sourcing import Requisition
from app.services.approvals.service import decide as svc_decide
from app.services.buyplan_workflow import (
    _complete_plan,
    check_completion,
    plan_needs_approver_reason,
    submit_buy_plan,
    verify_po,
)

# ── Helpers ─────────────────────────────────────────────────────────────


def _make_user(
    db: Session,
    *,
    can_approve_buy_plans: bool = False,
    can_approve_purchase_orders: bool = False,
    purchase_order_approval_limit: Decimal | None = None,
    role: str = "admin",
) -> User:
    u = User(
        email=f"po-line-{uuid.uuid4().hex[:8]}@test.com",
        name="PO Line User",
        role=role,
        azure_id=f"azure-po-line-{uuid.uuid4().hex[:8]}",
        can_approve_buy_plans=can_approve_buy_plans,
        can_approve_purchase_orders=can_approve_purchase_orders,
        purchase_order_approval_limit=purchase_order_approval_limit,
        created_at=datetime.now(UTC),
    )
    db.add(u)
    db.flush()
    return u


def _make_plan(
    db: Session,
    user: User,
    *,
    status: str = BuyPlanStatus.ACTIVE.value,
    so_status: str = SOVerificationStatus.APPROVED.value,
    total_cost: float = 100.0,
) -> BuyPlan:
    req = Requisition(
        name=f"REQ-POL-{uuid.uuid4().hex[:6]}",
        customer_name="POLCo",
        status="active",
        created_by=user.id,
        created_at=datetime.now(UTC),
    )
    db.add(req)
    db.flush()
    quote = Quote(
        requisition_id=req.id,
        quote_number=f"QPOL-{uuid.uuid4().hex[:8]}",
        line_items=[],
        status="sent",
        created_by_id=user.id,
        created_at=datetime.now(UTC),
    )
    db.add(quote)
    db.flush()
    bp = BuyPlan(
        requisition_id=req.id,
        quote_id=quote.id,
        status=status,
        so_status=so_status,
        total_cost=total_cost,
        submitted_by_id=user.id,
    )
    db.add(bp)
    db.flush()
    return bp


def _make_line(
    db: Session,
    plan: BuyPlan,
    *,
    status: str = BuyPlanLineStatus.PENDING_VERIFY.value,
    unit_cost: float = 10.0,
    quantity: int = 100,
    po_number: str | None = "PO-1234",
) -> BuyPlanLine:
    line = BuyPlanLine(
        buy_plan_id=plan.id,
        status=status,
        unit_cost=unit_cost,
        quantity=quantity,
        po_number=po_number,
        po_confirmed_at=datetime.now(UTC) if po_number else None,
    )
    db.add(line)
    db.flush()
    return line


def _po_gate_requests(db: Session, plan_id: int) -> list[ApprovalRequest]:
    return list(
        db.execute(
            select(ApprovalRequest).where(
                ApprovalRequest.subject_type == ApprovalSubjectType.BUY_PLAN,
                ApprovalRequest.subject_id == plan_id,
                ApprovalRequest.gate_type == ApprovalGateType.PURCHASE_ORDER,
            )
        ).scalars()
    )


def _po_activity(db: Session, plan_id: int, activity_type: ActivityType) -> list[ActivityLog]:
    return list(
        db.execute(
            select(ActivityLog).where(
                ActivityLog.activity_type == activity_type,
                ActivityLog.buy_plan_id == plan_id,
            )
        ).scalars()
    )


# ── The deal-level PURCHASE_ORDER gate is retired ──────────────────────────


def test_buyplan_approval_never_opens_deal_po_gate(db_session: Session) -> None:
    """Approving a buy plan opens NO PURCHASE_ORDER ApprovalRequest, no matter the total
    (the old >$5k deal-level gate is retired; the plan goes ACTIVE and stays there)."""
    approver = _make_user(db_session, can_approve_buy_plans=True, can_approve_purchase_orders=True)
    plan = _make_plan(
        db_session,
        approver,
        status=BuyPlanStatus.DRAFT.value,
        so_status=SOVerificationStatus.PENDING.value,
        total_cost=50_000.0,
    )

    submit_buy_plan(plan.id, f"SO-{uuid.uuid4().hex[:6]}", approver, db_session)
    bp_req = db_session.execute(
        select(ApprovalRequest).where(
            ApprovalRequest.subject_type == ApprovalSubjectType.BUY_PLAN,
            ApprovalRequest.subject_id == plan.id,
            ApprovalRequest.gate_type == ApprovalGateType.BUY_PLAN,
        )
    ).scalar_one()
    svc_decide(db_session, bp_req.id, approver, "approve", comment="ok")

    db_session.refresh(plan)
    assert plan.status == BuyPlanStatus.ACTIVE.value
    assert _po_gate_requests(db_session, plan.id) == []


# ── Completion refuses while a PO decision is open ─────────────────────────


def test_complete_plan_blocks_while_line_pending_verify(db_session: Session) -> None:
    """Neither completion path may pass an undecided PO: check_completion won't complete
    (line not terminal) AND a direct _complete_plan call (the stock-sale job's path,
    which bypasses check_completion's all-terminal check) refuses via _has_open_po_gate."""
    user = _make_user(db_session, can_approve_purchase_orders=True)
    plan = _make_plan(db_session, user)
    _make_line(db_session, plan, status=BuyPlanLineStatus.VERIFIED.value)
    pending = _make_line(db_session, plan, status=BuyPlanLineStatus.PENDING_VERIFY.value)

    # Path 1: normal auto-complete declines (a PENDING_VERIFY line is not terminal).
    result = check_completion(plan.id, db_session)
    assert result.status == BuyPlanStatus.ACTIVE.value

    # Path 2: direct _complete_plan (stock-sale auto-complete job) refuses too.
    _complete_plan(plan, db_session)
    db_session.flush()
    db_session.refresh(plan)
    assert plan.status == BuyPlanStatus.ACTIVE.value
    assert plan.completed_at is None
    assert plan.case_report is None

    # Once the PO decision resolves, completion proceeds normally.
    pending.status = BuyPlanLineStatus.VERIFIED.value
    db_session.flush()
    result = check_completion(plan.id, db_session)
    assert result.status == BuyPlanStatus.COMPLETED.value
    assert result.case_report


# ── verify_po enforces the per-user dollar limit per line ──────────────────


def test_verify_po_respects_dollar_limit(db_session: Session) -> None:
    """An approver whose purchase_order_approval_limit is below THIS line's amount is
    rejected (PermissionError); an unlimited/high-limit approver verifies fine.

    The can_verify_po_line UI predicate mirrors the same check.
    """
    capped = _make_user(db_session, can_approve_purchase_orders=True, purchase_order_approval_limit=Decimal("500"))
    unlimited = _make_user(db_session, can_approve_purchase_orders=True)
    plan = _make_plan(db_session, capped, so_status=SOVerificationStatus.PENDING.value)
    line = _make_line(db_session, plan, unit_cost=10.0, quantity=100)  # $1,000 > $500 cap

    assert can_verify_po_line(capped, line) is False
    assert can_verify_po_line(unlimited, line) is True

    with pytest.raises(PermissionError, match="approval limit"):
        verify_po(plan.id, line.id, "approve", capped, db_session)
    db_session.refresh(line)
    assert line.status == BuyPlanLineStatus.PENDING_VERIFY.value  # untouched

    verified = verify_po(plan.id, line.id, "approve", unlimited, db_session)
    assert verified.status == BuyPlanLineStatus.VERIFIED.value
    assert verified.po_verified_by_id == unlimited.id


# ── verify_po writes a durable audit trail ─────────────────────────────────


def test_verify_po_writes_activity_log(db_session: Session) -> None:
    """Approve writes a PO_LINE_VERIFIED ActivityLog row; reject writes PO_LINE_REJECTED
    (naming the PO number + note) — a timeline-visible record, not just a log line."""
    approver = _make_user(db_session, can_approve_purchase_orders=True)
    plan = _make_plan(db_session, approver, so_status=SOVerificationStatus.PENDING.value)
    approved_line = _make_line(db_session, plan, po_number="PO-OK-1")
    rejected_line = _make_line(db_session, plan, po_number="PO-BAD-2")

    verify_po(plan.id, approved_line.id, "approve", approver, db_session)
    verify_po(plan.id, rejected_line.id, "reject", approver, db_session, rejection_note="wrong SKU")

    verified_rows = _po_activity(db_session, plan.id, ActivityType.PO_LINE_VERIFIED)
    assert len(verified_rows) == 1
    assert "PO PO-OK-1" in (verified_rows[0].notes or "")
    assert verified_rows[0].user_id == approver.id

    rejected_rows = _po_activity(db_session, plan.id, ActivityType.PO_LINE_REJECTED)
    assert len(rejected_rows) == 1
    # Logged BEFORE the reset clears po_number, and carries the rejection note.
    assert "PO PO-BAD-2" in (rejected_rows[0].notes or "")
    assert "wrong SKU" in (rejected_rows[0].notes or "")


# ── Stall detectors are per-line, not plan-total ───────────────────────────


def test_plan_needs_approver_reason_purchase_order_is_per_line(db_session: Session) -> None:
    """An ACTIVE plan stalls with "purchase_order" when a PENDING_VERIFY line's amount
    has no eligible approver — independent of the plan total (tiny total, big line)."""
    capped = _make_user(db_session, can_approve_purchase_orders=True, purchase_order_approval_limit=Decimal("500"))
    plan = _make_plan(db_session, capped, total_cost=100.0)  # far below the old $5k threshold
    line = _make_line(db_session, plan, unit_cost=10.0, quantity=100)  # $1,000 line

    # Only the $500-capped approver exists → the $1,000 line has no eligible approver.
    assert plan_needs_approver_reason(plan, db_session) == "purchase_order"

    # An unlimited approver makes the line coverable → no stall.
    _make_user(db_session, can_approve_purchase_orders=True)
    assert plan_needs_approver_reason(plan, db_session) is None

    # No PENDING_VERIFY line → never a purchase_order stall, however large the plan.
    line.status = BuyPlanLineStatus.AWAITING_PO.value
    plan.total_cost = 1_000_000.0
    db_session.flush()
    assert plan_needs_approver_reason(plan, db_session) is None


def test_plan_needs_approver_reason_purchase_order_clears_with_unlimited_approver(db_session: Session) -> None:
    """The per-line PURCHASE_ORDER stall (the predicate behind the workspace BP-tab
    stall warnings) flags an ACTIVE plan when any PENDING_VERIFY line's amount has no
    eligible approver; an eligible approver (or no pending line) clears it."""
    # A buy-plan approver exists so the BUY_PLAN branch stays quiet.
    capped = _make_user(
        db_session,
        can_approve_buy_plans=True,
        can_approve_purchase_orders=True,
        purchase_order_approval_limit=Decimal("500"),
    )
    stuck = _make_plan(db_session, capped, total_cost=100.0)
    _make_line(db_session, stuck, unit_cost=10.0, quantity=100)  # $1,000 > $500 cap

    # ACTIVE plan with no pending-verify line: never flagged.
    quiet = _make_plan(db_session, capped, total_cost=1_000_000.0)
    _make_line(db_session, quiet, status=BuyPlanLineStatus.AWAITING_PO.value)

    assert plan_needs_approver_reason(stuck, db_session) == "purchase_order"
    assert plan_needs_approver_reason(quiet, db_session) is None

    # An unlimited approver covers the line → nothing is stuck.
    _make_user(db_session, can_approve_purchase_orders=True)
    assert plan_needs_approver_reason(stuck, db_session) is None
