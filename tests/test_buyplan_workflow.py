"""Tests for app/services/buyplan_workflow.py — Buy Plan workflow operations.

Covers: submit, approve, reject, SO verification, PO confirmation, PO verification,
issue flagging, completion, reset-to-draft, resubmit, favoritism detection,
case report generation, auto-approval logic, line edits, line overrides,
financials recalculation, stock sale detection, buyer task generation,
and async PO verification scanning (v1 and v3).

Called by: pytest
Depends on: conftest fixtures, buyplan_workflow module
"""

import asyncio
from collections.abc import Awaitable
from datetime import datetime, timedelta, timezone
from typing import TypeVar
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.constants import BuyPlanLineStatus, BuyPlanStatus, SOVerificationStatus
from app.models import Offer, Quote, Requirement, Requisition, User
from app.models.buy_plan import BuyPlan, BuyPlanLine, VerificationGroupMember
from app.services.buyplan_workflow import (
    RESUBMITTABLE_STATUSES,
    _apply_line_overrides,
    _generate_buyer_tasks,
    _is_stock_sale,
    _recalculate_financials,
    approve_buy_plan,
    cancel_buy_plan,
    check_completion,
    confirm_po,
    detect_favoritism,
    flag_line_issue,
    generate_case_report,
    halt_plan,
    plan_needs_approver_reason,
    reset_buy_plan_to_draft,
    resolve_line_issue,
    resubmit_buy_plan,
    submit_buy_plan,
    verify_po,
    verify_po_sent,
)

# ── Helpers ──────────────────────────────────────────────────────────

_T = TypeVar("_T")


def _run(coro: Awaitable[_T]) -> _T:
    """Run an async coroutine to completion on a fresh event loop."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_plan(db: Session, user: User, quote: Quote, requisition: Requisition, **overrides) -> BuyPlan:
    """Create a BuyPlan with defaults."""
    defaults = dict(
        quote_id=quote.id,
        requisition_id=requisition.id,
        status=BuyPlanStatus.DRAFT.value,
        so_status=SOVerificationStatus.PENDING.value,
        total_cost=100.00,
        total_revenue=200.00,
        total_margin_pct=50.00,
        ai_flags=[],
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(overrides)
    plan = BuyPlan(**defaults)
    db.add(plan)
    db.flush()
    return plan


def _make_line(db: Session, plan: BuyPlan, **overrides) -> BuyPlanLine:
    """Create a BuyPlanLine with defaults."""
    defaults = dict(
        buy_plan_id=plan.id,
        quantity=100,
        unit_cost=1.00,
        unit_sell=2.00,
        status=BuyPlanLineStatus.AWAITING_PO.value,
    )
    defaults.update(overrides)
    line = BuyPlanLine(**defaults)
    db.add(line)
    db.flush()
    return line


def _make_verification_member(db: Session, user: User) -> VerificationGroupMember:
    """Add user to the ops verification group."""
    member = VerificationGroupMember(user_id=user.id, is_active=True)
    db.add(member)
    db.flush()
    return member


# ── Submit Buy Plan ──────────────────────────────────────────────────


class TestSubmitBuyPlan:
    """Tests for submit_buy_plan()."""

    def test_submit_low_cost_goes_to_manager(
        self, db_session: Session, test_user: User, test_quote: Quote, test_requisition: Requisition
    ):
        """Frozen scope: every plan goes to the one manager approval — no auto-approve,
        regardless of cost. A low-cost plan that used to auto-approve now lands PENDING."""
        plan = _make_plan(db_session, test_user, test_quote, test_requisition, total_cost=100.00)
        _make_line(db_session, plan)
        db_session.refresh(plan)

        result = submit_buy_plan(plan.id, "SO-001", test_user, db_session)

        assert result.status == BuyPlanStatus.PENDING.value
        assert result.auto_approved is not True
        assert result.sales_order_number == "SO-001"
        assert result.submitted_by_id == test_user.id
        assert result.submitted_at is not None

    def test_submit_pending_approval_high_cost(
        self, db_session: Session, test_user: User, test_quote: Quote, test_requisition: Requisition
    ):
        """Plans over threshold go to pending."""
        plan = _make_plan(db_session, test_user, test_quote, test_requisition, total_cost=10000.00)
        _make_line(db_session, plan)
        db_session.refresh(plan)

        result = submit_buy_plan(plan.id, "SO-002", test_user, db_session)

        assert result.status == BuyPlanStatus.PENDING.value
        assert result.auto_approved is not True

    def test_submit_pending_critical_flags(
        self, db_session: Session, test_user: User, test_quote: Quote, test_requisition: Requisition
    ):
        """Plans with critical AI flags go to pending regardless of cost."""
        plan = _make_plan(
            db_session,
            test_user,
            test_quote,
            test_requisition,
            total_cost=100.00,
            ai_flags=[{"severity": "critical", "type": "test", "message": "critical flag"}],
        )
        _make_line(db_session, plan)
        db_session.refresh(plan)

        result = submit_buy_plan(plan.id, "SO-003", test_user, db_session)

        assert result.status == BuyPlanStatus.PENDING.value

    def test_submit_with_customer_po_and_notes(
        self, db_session: Session, test_user: User, test_quote: Quote, test_requisition: Requisition
    ):
        """Submit with optional fields."""
        plan = _make_plan(db_session, test_user, test_quote, test_requisition, total_cost=50.00)
        _make_line(db_session, plan)
        db_session.refresh(plan)

        with patch("app.services.buyplan_workflow._generate_buyer_tasks"):
            result = submit_buy_plan(
                plan.id,
                "SO-004",
                test_user,
                db_session,
                customer_po_number="PO-CUST-001",
                salesperson_notes="Rush order",
            )

        assert result.customer_po_number == "PO-CUST-001"
        assert result.salesperson_notes == "Rush order"

    def test_submit_not_found(self, db_session: Session, test_user: User):
        """Submit nonexistent plan raises ValueError."""
        with pytest.raises(ValueError, match="not found"):
            submit_buy_plan(9999, "SO-X", test_user, db_session)

    def test_submit_not_draft(
        self, db_session: Session, test_user: User, test_quote: Quote, test_requisition: Requisition
    ):
        """Submit non-draft plan raises ValueError."""
        plan = _make_plan(db_session, test_user, test_quote, test_requisition, status=BuyPlanStatus.ACTIVE.value)
        _make_line(db_session, plan)
        db_session.refresh(plan)

        with pytest.raises(ValueError, match="Can only submit draft"):
            submit_buy_plan(plan.id, "SO-X", test_user, db_session)

    def test_submit_with_line_edits(
        self, db_session: Session, test_user: User, test_quote: Quote, test_requisition: Requisition, test_offer: Offer
    ):
        """Submit with line edits applies them."""
        plan = _make_plan(db_session, test_user, test_quote, test_requisition, total_cost=50.00)
        req = db_session.query(Requirement).filter_by(requisition_id=test_requisition.id).first()
        _make_line(db_session, plan, requirement_id=req.id)
        db_session.refresh(plan)

        edits = [{"requirement_id": req.id, "offer_id": test_offer.id, "quantity": 500}]

        with patch("app.services.buyplan_workflow._generate_buyer_tasks"):
            with patch("app.services.buyplan_workflow.assign_buyer", return_value=(test_user, "test")):
                with patch("app.services.buyplan_workflow.score_offer", return_value=85.0):
                    result = submit_buy_plan(plan.id, "SO-005", test_user, db_session, line_edits=edits)

        assert result.status == BuyPlanStatus.PENDING.value


# ── Approve Buy Plan ─────────────────────────────────────────────────


def _grant_approver(db: Session, user: User) -> User:
    """Grant the per-user buy-plan approval right (the canonical gate is the column, not
    the role — see app.dependencies.can_approve_buy_plans)."""
    user.can_approve_buy_plans = True
    db.add(user)
    db.flush()
    return user


def _grant_po_approver(db: Session, user: User) -> User:
    """Grant the per-user purchase-order approval right (Phase D: verify-PO gates on the
    can_approve_purchase_orders column, not ops verification-group membership)."""
    user.can_approve_purchase_orders = True
    db.add(user)
    db.flush()
    return user


class TestApproveBuyPlan:
    """Tests for approve_buy_plan().

    Approval is gated by the per-user ``can_approve_buy_plans`` right (admin-toggled),
    NOT by role — so each authorising test grants the flag explicitly.
    """

    def test_approve(
        self, db_session: Session, manager_user: User, test_user: User, test_quote: Quote, test_requisition: Requisition
    ):
        """A user holding the approval right can approve a pending plan."""
        _grant_approver(db_session, manager_user)
        plan = _make_plan(db_session, test_user, test_quote, test_requisition, status=BuyPlanStatus.PENDING.value)
        _make_line(db_session, plan)
        db_session.refresh(plan)

        with patch("app.services.buyplan_workflow._generate_buyer_tasks"):
            result = approve_buy_plan(plan.id, "approve", manager_user, db_session, notes="LGTM")

        assert result.status == BuyPlanStatus.ACTIVE.value
        assert result.approved_by_id == manager_user.id
        assert result.approved_at is not None
        assert result.approval_notes == "LGTM"
        # Phase D — the single approval absorbs SO verification (no separate verify-SO step).
        assert result.so_status == SOVerificationStatus.APPROVED.value
        assert result.so_verified_by_id == manager_user.id
        assert result.so_verified_at is not None

    def test_approve_folds_so_and_completes_without_verify_so(
        self, db_session: Session, manager_user: User, test_user: User, test_quote: Quote, test_requisition: Requisition
    ):
        """A plan reaches ACTIVE via exactly ONE manager approval (so_status auto-
        approved), and once its lines are all verified it auto-completes with NO
        separate verify-SO."""
        _grant_approver(db_session, manager_user)
        plan = _make_plan(db_session, test_user, test_quote, test_requisition, status=BuyPlanStatus.PENDING.value)
        line = _make_line(db_session, plan, status=BuyPlanLineStatus.PENDING_VERIFY.value)
        db_session.refresh(plan)

        with patch("app.services.buyplan_workflow._generate_buyer_tasks"):
            approve_buy_plan(plan.id, "approve", manager_user, db_session)

        # The fold already cleared the SO gate — completion needs no verify_so call.
        line.status = BuyPlanLineStatus.VERIFIED.value
        db_session.flush()
        result = check_completion(plan.id, db_session)
        assert result.status == BuyPlanStatus.COMPLETED.value

    def test_reject(
        self, db_session: Session, manager_user: User, test_user: User, test_quote: Quote, test_requisition: Requisition
    ):
        """An approver can reject a pending plan back to draft (reason recorded)."""
        _grant_approver(db_session, manager_user)
        plan = _make_plan(db_session, test_user, test_quote, test_requisition, status=BuyPlanStatus.PENDING.value)
        _make_line(db_session, plan)
        db_session.refresh(plan)

        result = approve_buy_plan(plan.id, "reject", manager_user, db_session, notes="Needs changes")

        assert result.status == BuyPlanStatus.DRAFT.value
        assert result.approval_notes == "Needs changes"
        assert result.approved_by_id == manager_user.id
        assert result.approved_at is not None

    def test_reject_requires_reason(
        self, db_session: Session, manager_user: User, test_user: User, test_quote: Quote, test_requisition: Requisition
    ):
        """Reject with no reason (None or whitespace) is refused; plan stays pending."""
        _grant_approver(db_session, manager_user)
        plan = _make_plan(db_session, test_user, test_quote, test_requisition, status=BuyPlanStatus.PENDING.value)
        _make_line(db_session, plan)
        db_session.refresh(plan)

        for bad in (None, "", "   "):
            with pytest.raises(ValueError, match="rejection reason is required"):
                approve_buy_plan(plan.id, "reject", manager_user, db_session, notes=bad)
        db_session.refresh(plan)
        assert plan.status == BuyPlanStatus.PENDING.value

    def test_approve_records_activity_log(
        self, db_session: Session, manager_user: User, test_user: User, test_quote: Quote, test_requisition: Requisition
    ):
        """Approve writes a BUYPLAN_APPROVED ActivityLog scoped to the plan; reject
        writes a BUYPLAN_REJECTED row."""
        from app.constants import ActivityType
        from app.models.intelligence import ActivityLog

        _grant_approver(db_session, manager_user)
        plan = _make_plan(db_session, test_user, test_quote, test_requisition, status=BuyPlanStatus.PENDING.value)
        _make_line(db_session, plan)
        db_session.refresh(plan)

        with patch("app.services.buyplan_workflow._generate_buyer_tasks"):
            approve_buy_plan(plan.id, "approve", manager_user, db_session, notes="ok")

        row = (
            db_session.query(ActivityLog)
            .filter(ActivityLog.buy_plan_id == plan.id, ActivityLog.activity_type == str(ActivityType.BUYPLAN_APPROVED))
            .one()
        )
        assert row.user_id == manager_user.id
        assert row.requisition_id == plan.requisition_id

    def test_reject_records_activity_log(
        self, db_session: Session, manager_user: User, test_user: User, test_quote: Quote, test_requisition: Requisition
    ):
        from app.constants import ActivityType
        from app.models.intelligence import ActivityLog

        _grant_approver(db_session, manager_user)
        plan = _make_plan(db_session, test_user, test_quote, test_requisition, status=BuyPlanStatus.PENDING.value)
        _make_line(db_session, plan)
        db_session.refresh(plan)

        approve_buy_plan(plan.id, "reject", manager_user, db_session, notes="too expensive")

        row = (
            db_session.query(ActivityLog)
            .filter(ActivityLog.buy_plan_id == plan.id, ActivityLog.activity_type == str(ActivityType.BUYPLAN_REJECTED))
            .one()
        )
        assert row.user_id == manager_user.id

    def test_approve_with_line_overrides(
        self,
        db_session: Session,
        manager_user: User,
        test_user: User,
        test_quote: Quote,
        test_requisition: Requisition,
        test_offer: Offer,
    ):
        """An approver can approve with line overrides."""
        _grant_approver(db_session, manager_user)
        plan = _make_plan(db_session, test_user, test_quote, test_requisition, status=BuyPlanStatus.PENDING.value)
        line = _make_line(db_session, plan, unit_sell=3.00)
        db_session.refresh(plan)

        overrides = [{"line_id": line.id, "offer_id": test_offer.id, "quantity": 200, "manager_note": "Swap vendor"}]

        with patch("app.services.buyplan_workflow._generate_buyer_tasks"):
            result = approve_buy_plan(plan.id, "approve", manager_user, db_session, line_overrides=overrides)

        assert result.status == BuyPlanStatus.ACTIVE.value

    def test_approve_not_found(self, db_session: Session, manager_user: User):
        _grant_approver(db_session, manager_user)
        with pytest.raises(ValueError, match="not found"):
            approve_buy_plan(9999, "approve", manager_user, db_session)

    def test_approve_not_pending(
        self, db_session: Session, manager_user: User, test_user: User, test_quote: Quote, test_requisition: Requisition
    ):
        _grant_approver(db_session, manager_user)
        plan = _make_plan(db_session, test_user, test_quote, test_requisition, status=BuyPlanStatus.ACTIVE.value)
        with pytest.raises(ValueError, match="Can only approve/reject pending"):
            approve_buy_plan(plan.id, "approve", manager_user, db_session)

    def test_approve_without_right(
        self, db_session: Session, manager_user: User, test_user: User, test_quote: Quote, test_requisition: Requisition
    ):
        """Even a manager WITHOUT the can_approve_buy_plans right is refused — the
        column, not the role, is the single source of truth."""
        plan = _make_plan(db_session, test_user, test_quote, test_requisition, status=BuyPlanStatus.PENDING.value)
        for actor in (test_user, manager_user):  # neither has the flag granted
            with pytest.raises(PermissionError, match="approval right required"):
                approve_buy_plan(plan.id, "approve", actor, db_session)

    def test_approve_invalid_action(
        self, db_session: Session, manager_user: User, test_user: User, test_quote: Quote, test_requisition: Requisition
    ):
        _grant_approver(db_session, manager_user)
        plan = _make_plan(db_session, test_user, test_quote, test_requisition, status=BuyPlanStatus.PENDING.value)
        with pytest.raises(ValueError, match="Invalid action"):
            approve_buy_plan(plan.id, "bogus", manager_user, db_session)

    def test_right_grants_access_regardless_of_role(
        self, db_session: Session, test_user: User, test_quote: Quote, test_requisition: Requisition
    ):
        """A buyer-role user holding the right CAN approve (right is role-
        independent)."""
        _grant_approver(db_session, test_user)  # test_user is role="buyer"
        plan = _make_plan(db_session, test_user, test_quote, test_requisition, status=BuyPlanStatus.PENDING.value)
        _make_line(db_session, plan)
        db_session.refresh(plan)

        with patch("app.services.buyplan_workflow._generate_buyer_tasks"):
            result = approve_buy_plan(plan.id, "approve", test_user, db_session)

        assert result.status == BuyPlanStatus.ACTIVE.value


# ── Halt (the single off-ramp) ───────────────────────────────────────


class TestHaltPlan:
    """Tests for halt_plan() — the standalone halt extracted from the retired
    verify_so."""

    def test_halt_active_plan(
        self, db_session: Session, manager_user: User, test_user: User, test_quote: Quote, test_requisition: Requisition
    ):
        """A supervisor halts an ACTIVE plan → HALTED, reason stamped, halted_by/at
        set."""
        plan = _make_plan(db_session, test_user, test_quote, test_requisition, status=BuyPlanStatus.ACTIVE.value)

        result = halt_plan(plan.id, manager_user, db_session, reason="Stop everything")

        assert result.status == BuyPlanStatus.HALTED.value
        assert result.so_status == SOVerificationStatus.REJECTED.value
        assert result.so_rejection_note == "Stop everything"
        assert result.halted_by_id == manager_user.id
        assert result.halted_at is not None

    def test_halt_pending_plan_cancels_open_engine_request(
        self, db_session: Session, manager_user: User, test_user: User, test_quote: Quote, test_requisition: Requisition
    ):
        """Halting a PENDING plan cancels its open engine request first (no orphan)."""
        plan = _make_plan(db_session, test_user, test_quote, test_requisition, status=BuyPlanStatus.PENDING.value)

        with patch("app.services.buyplan_workflow._cancel_open_engine_requests_for_plan") as cancel_mock:
            result = halt_plan(plan.id, manager_user, db_session, reason="halt it")

        cancel_mock.assert_called_once()
        assert result.status == BuyPlanStatus.HALTED.value

    def test_halt_active_plan_cancels_open_engine_request(
        self, db_session: Session, manager_user: User, test_user: User, test_quote: Quote, test_requisition: Requisition
    ):
        """Halting an ACTIVE plan ALSO cancels its open engine request (the deal-level
        PURCHASE_ORDER gate opened at approval for over-threshold plans).

        Halt is the single off-ramp and must not orphan a REQUESTED row — the cancel is
        unconditional, matching cancel_buy_plan (regression guard for the ACTIVE-halt
        orphan).
        """
        plan = _make_plan(db_session, test_user, test_quote, test_requisition, status=BuyPlanStatus.ACTIVE.value)

        with patch("app.services.buyplan_workflow._cancel_open_engine_requests_for_plan") as cancel_mock:
            result = halt_plan(plan.id, manager_user, db_session, reason="halt it")

        cancel_mock.assert_called_once()
        assert result.status == BuyPlanStatus.HALTED.value

    def test_halt_by_ops_member(
        self, db_session: Session, test_user: User, test_quote: Quote, test_requisition: Requisition
    ):
        """An active ops verification-group member (non-manager role) may halt."""
        _make_verification_member(db_session, test_user)  # test_user is role="buyer"
        plan = _make_plan(db_session, test_user, test_quote, test_requisition, status=BuyPlanStatus.ACTIVE.value)

        result = halt_plan(plan.id, test_user, db_session, reason="ops halt")
        assert result.status == BuyPlanStatus.HALTED.value

    def test_halt_requires_supervisor_or_ops(
        self, db_session: Session, test_user: User, test_quote: Quote, test_requisition: Requisition
    ):
        """A plain buyer (no manager role, no ops membership) cannot halt →
        PermissionError."""
        plan = _make_plan(db_session, test_user, test_quote, test_requisition, status=BuyPlanStatus.ACTIVE.value)
        with pytest.raises(PermissionError, match="supervisor or ops"):
            halt_plan(plan.id, test_user, db_session, reason="nope")
        db_session.refresh(plan)
        assert plan.status == BuyPlanStatus.ACTIVE.value

    def test_halt_not_found(self, db_session: Session, manager_user: User):
        with pytest.raises(ValueError, match="not found"):
            halt_plan(9999, manager_user, db_session, reason="x")

    def test_halt_completed_plan_rejected(
        self, db_session: Session, manager_user: User, test_user: User, test_quote: Quote, test_requisition: Requisition
    ):
        """A COMPLETED (terminal) plan is not haltable."""
        plan = _make_plan(db_session, test_user, test_quote, test_requisition, status=BuyPlanStatus.COMPLETED.value)
        with pytest.raises(ValueError, match="Cannot halt a completed plan"):
            halt_plan(plan.id, manager_user, db_session, reason="too late")

    def test_resubmit_from_halted_still_works(
        self, db_session: Session, manager_user: User, test_user: User, test_quote: Quote, test_requisition: Requisition
    ):
        """A halted plan can be reset to draft and resubmitted (off-ramp is
        reversible)."""
        plan = _make_plan(db_session, test_user, test_quote, test_requisition, status=BuyPlanStatus.ACTIVE.value)
        halt_plan(plan.id, manager_user, db_session, reason="halt")
        reset_buy_plan_to_draft(plan.id, test_user, db_session)
        db_session.refresh(plan)
        assert plan.status == BuyPlanStatus.DRAFT.value
        assert plan.so_status == SOVerificationStatus.PENDING.value
        assert plan.halted_by_id is None


# ── PO Confirmation ──────────────────────────────────────────────────


class TestConfirmPO:
    """Tests for confirm_po()."""

    def test_confirm_po_success(
        self, db_session: Session, test_user: User, test_quote: Quote, test_requisition: Requisition
    ):
        plan = _make_plan(db_session, test_user, test_quote, test_requisition, status=BuyPlanStatus.ACTIVE.value)
        line = _make_line(db_session, plan, status=BuyPlanLineStatus.AWAITING_PO.value)
        db_session.refresh(plan)

        ship_date = datetime.now(timezone.utc) + timedelta(days=7)
        result = confirm_po(plan.id, line.id, "PO-123", ship_date, test_user, db_session)

        assert result.po_number == "PO-123"
        assert result.status == BuyPlanLineStatus.PENDING_VERIFY.value
        assert result.po_confirmed_at is not None
        assert result.estimated_ship_date == ship_date

    def test_confirm_po_plan_not_found(self, db_session: Session, test_user: User):
        with pytest.raises(ValueError, match="not found"):
            confirm_po(9999, 1, "PO-X", datetime.now(timezone.utc), test_user, db_session)

    def test_confirm_po_plan_not_active(
        self, db_session: Session, test_user: User, test_quote: Quote, test_requisition: Requisition
    ):
        plan = _make_plan(db_session, test_user, test_quote, test_requisition, status=BuyPlanStatus.DRAFT.value)
        line = _make_line(db_session, plan)
        with pytest.raises(ValueError, match="Plan must be active"):
            confirm_po(plan.id, line.id, "PO-X", datetime.now(timezone.utc), test_user, db_session)

    def test_confirm_po_line_not_found(
        self, db_session: Session, test_user: User, test_quote: Quote, test_requisition: Requisition
    ):
        plan = _make_plan(db_session, test_user, test_quote, test_requisition, status=BuyPlanStatus.ACTIVE.value)
        with pytest.raises(ValueError, match="Line .* not found"):
            confirm_po(plan.id, 9999, "PO-X", datetime.now(timezone.utc), test_user, db_session)

    def test_confirm_po_wrong_status(
        self, db_session: Session, test_user: User, test_quote: Quote, test_requisition: Requisition
    ):
        plan = _make_plan(db_session, test_user, test_quote, test_requisition, status=BuyPlanStatus.ACTIVE.value)
        line = _make_line(db_session, plan, status=BuyPlanLineStatus.VERIFIED.value)
        with pytest.raises(ValueError, match="Line must be awaiting PO"):
            confirm_po(plan.id, line.id, "PO-X", datetime.now(timezone.utc), test_user, db_session)


# ── PO Verification ──────────────────────────────────────────────────


class TestVerifyPO:
    """Tests for verify_po().

    Phase D: verify-PO gates on the per-user ``can_approve_purchase_orders`` right (a
    manager-held action), NOT ops verification-group membership — each authorising test
    grants the flag explicitly.
    """

    def test_approve_po(self, db_session: Session, test_user: User, test_quote: Quote, test_requisition: Requisition):
        _grant_po_approver(db_session, test_user)
        plan = _make_plan(db_session, test_user, test_quote, test_requisition, status=BuyPlanStatus.ACTIVE.value)
        line = _make_line(db_session, plan, status=BuyPlanLineStatus.PENDING_VERIFY.value)
        db_session.refresh(plan)

        result = verify_po(plan.id, line.id, "approve", test_user, db_session)

        assert result.status == BuyPlanLineStatus.VERIFIED.value
        assert result.po_verified_by_id == test_user.id
        assert result.po_verified_at is not None

    def test_reject_po(self, db_session: Session, test_user: User, test_quote: Quote, test_requisition: Requisition):
        _grant_po_approver(db_session, test_user)
        plan = _make_plan(db_session, test_user, test_quote, test_requisition, status=BuyPlanStatus.ACTIVE.value)
        line = _make_line(db_session, plan, status=BuyPlanLineStatus.PENDING_VERIFY.value, po_number="PO-123")
        db_session.refresh(plan)

        result = verify_po(plan.id, line.id, "reject", test_user, db_session, rejection_note="Wrong PO")

        assert result.status == BuyPlanLineStatus.AWAITING_PO.value
        assert result.po_rejection_note == "Wrong PO"
        assert result.po_number is None
        assert result.estimated_ship_date is None
        assert result.po_confirmed_at is None

    def test_verify_po_not_found(self, db_session: Session, test_user: User):
        _grant_po_approver(db_session, test_user)
        with pytest.raises(ValueError, match="not found"):
            verify_po(9999, 1, "approve", test_user, db_session)

    def test_verify_po_line_not_found(
        self, db_session: Session, test_user: User, test_quote: Quote, test_requisition: Requisition
    ):
        _grant_po_approver(db_session, test_user)
        plan = _make_plan(db_session, test_user, test_quote, test_requisition, status=BuyPlanStatus.ACTIVE.value)
        with pytest.raises(ValueError, match="Line .* not found"):
            verify_po(plan.id, 9999, "approve", test_user, db_session)

    def test_verify_po_wrong_status(
        self, db_session: Session, test_user: User, test_quote: Quote, test_requisition: Requisition
    ):
        _grant_po_approver(db_session, test_user)
        plan = _make_plan(db_session, test_user, test_quote, test_requisition, status=BuyPlanStatus.ACTIVE.value)
        line = _make_line(db_session, plan, status=BuyPlanLineStatus.AWAITING_PO.value)
        with pytest.raises(ValueError, match="Line must be pending verification"):
            verify_po(plan.id, line.id, "approve", test_user, db_session)

    def test_verify_po_without_right(
        self, db_session: Session, test_user: User, test_quote: Quote, test_requisition: Requisition
    ):
        """A user WITHOUT can_approve_purchase_orders is refused (ops membership no
        longer grants the right)."""
        _make_verification_member(db_session, test_user)  # ops member but no PO right
        plan = _make_plan(db_session, test_user, test_quote, test_requisition, status=BuyPlanStatus.ACTIVE.value)
        line = _make_line(db_session, plan, status=BuyPlanLineStatus.PENDING_VERIFY.value)
        with pytest.raises(PermissionError, match="Purchase-order approval right required"):
            verify_po(plan.id, line.id, "approve", test_user, db_session)

    def test_verify_po_with_right_succeeds(
        self, db_session: Session, manager_user: User, test_user: User, test_quote: Quote, test_requisition: Requisition
    ):
        """A manager holding can_approve_purchase_orders (but NOT an ops member) can
        verify."""
        _grant_po_approver(db_session, manager_user)
        plan = _make_plan(db_session, test_user, test_quote, test_requisition, status=BuyPlanStatus.ACTIVE.value)
        line = _make_line(db_session, plan, status=BuyPlanLineStatus.PENDING_VERIFY.value)
        db_session.refresh(plan)

        result = verify_po(plan.id, line.id, "approve", manager_user, db_session)
        assert result.status == BuyPlanLineStatus.VERIFIED.value

    def test_verify_po_invalid_action(
        self, db_session: Session, test_user: User, test_quote: Quote, test_requisition: Requisition
    ):
        _grant_po_approver(db_session, test_user)
        plan = _make_plan(db_session, test_user, test_quote, test_requisition, status=BuyPlanStatus.ACTIVE.value)
        line = _make_line(db_session, plan, status=BuyPlanLineStatus.PENDING_VERIFY.value)
        with pytest.raises(ValueError, match="Invalid PO verification action"):
            verify_po(plan.id, line.id, "bad", test_user, db_session)


# ── Flag Line Issue ──────────────────────────────────────────────────


class TestFlagLineIssue:
    """Tests for flag_line_issue()."""

    def test_flag_awaiting_po(
        self, db_session: Session, test_user: User, test_quote: Quote, test_requisition: Requisition
    ):
        plan = _make_plan(db_session, test_user, test_quote, test_requisition, status=BuyPlanStatus.ACTIVE.value)
        line = _make_line(db_session, plan, status=BuyPlanLineStatus.AWAITING_PO.value)

        result = flag_line_issue(plan.id, line.id, "sold_out", test_user, db_session, note="No stock")

        assert result.status == BuyPlanLineStatus.ISSUE.value
        assert result.issue_type == "sold_out"
        assert result.issue_note == "No stock"

    def test_flag_pending_verify(
        self, db_session: Session, test_user: User, test_quote: Quote, test_requisition: Requisition
    ):
        plan = _make_plan(db_session, test_user, test_quote, test_requisition, status=BuyPlanStatus.ACTIVE.value)
        line = _make_line(db_session, plan, status=BuyPlanLineStatus.PENDING_VERIFY.value)

        result = flag_line_issue(plan.id, line.id, "price_changed", test_user, db_session)

        assert result.status == BuyPlanLineStatus.ISSUE.value

    def test_flag_plan_not_found(self, db_session: Session, test_user: User):
        with pytest.raises(ValueError, match="not found"):
            flag_line_issue(9999, 1, "sold_out", test_user, db_session)

    def test_flag_plan_not_active(
        self, db_session: Session, test_user: User, test_quote: Quote, test_requisition: Requisition
    ):
        plan = _make_plan(db_session, test_user, test_quote, test_requisition, status=BuyPlanStatus.DRAFT.value)
        line = _make_line(db_session, plan)
        with pytest.raises(ValueError, match="Plan must be active"):
            flag_line_issue(plan.id, line.id, "sold_out", test_user, db_session)

    def test_flag_line_not_found(
        self, db_session: Session, test_user: User, test_quote: Quote, test_requisition: Requisition
    ):
        plan = _make_plan(db_session, test_user, test_quote, test_requisition, status=BuyPlanStatus.ACTIVE.value)
        with pytest.raises(ValueError, match="Line .* not found"):
            flag_line_issue(plan.id, 9999, "sold_out", test_user, db_session)

    def test_flag_unflaggable_status(
        self, db_session: Session, test_user: User, test_quote: Quote, test_requisition: Requisition
    ):
        plan = _make_plan(db_session, test_user, test_quote, test_requisition, status=BuyPlanStatus.ACTIVE.value)
        line = _make_line(db_session, plan, status=BuyPlanLineStatus.VERIFIED.value)
        with pytest.raises(ValueError, match="Cannot flag issue"):
            flag_line_issue(plan.id, line.id, "sold_out", test_user, db_session)


# ── Check Completion ─────────────────────────────────────────────────


class TestCheckCompletion:
    """Tests for check_completion()."""

    def test_auto_complete_all_verified(
        self, db_session: Session, test_user: User, test_quote: Quote, test_requisition: Requisition
    ):
        plan = _make_plan(
            db_session,
            test_user,
            test_quote,
            test_requisition,
            status=BuyPlanStatus.ACTIVE.value,
            so_status=SOVerificationStatus.APPROVED.value,
        )
        _make_line(db_session, plan, status=BuyPlanLineStatus.VERIFIED.value)
        _make_line(db_session, plan, status=BuyPlanLineStatus.CANCELLED.value)
        db_session.refresh(plan)

        result = check_completion(plan.id, db_session)

        assert result.status == BuyPlanStatus.COMPLETED.value
        assert result.completed_at is not None
        assert result.case_report is not None

    def test_no_complete_non_terminal_lines(
        self, db_session: Session, test_user: User, test_quote: Quote, test_requisition: Requisition
    ):
        plan = _make_plan(
            db_session,
            test_user,
            test_quote,
            test_requisition,
            status=BuyPlanStatus.ACTIVE.value,
            so_status=SOVerificationStatus.APPROVED.value,
        )
        _make_line(db_session, plan, status=BuyPlanLineStatus.VERIFIED.value)
        _make_line(db_session, plan, status=BuyPlanLineStatus.AWAITING_PO.value)
        db_session.refresh(plan)

        result = check_completion(plan.id, db_session)

        assert result.status == BuyPlanStatus.ACTIVE.value

    def test_no_complete_so_not_approved(
        self, db_session: Session, test_user: User, test_quote: Quote, test_requisition: Requisition
    ):
        plan = _make_plan(
            db_session,
            test_user,
            test_quote,
            test_requisition,
            status=BuyPlanStatus.ACTIVE.value,
            so_status=SOVerificationStatus.PENDING.value,
        )
        _make_line(db_session, plan, status=BuyPlanLineStatus.VERIFIED.value)
        db_session.refresh(plan)

        result = check_completion(plan.id, db_session)

        assert result.status == BuyPlanStatus.ACTIVE.value

    def test_no_complete_empty_lines(
        self, db_session: Session, test_user: User, test_quote: Quote, test_requisition: Requisition
    ):
        plan = _make_plan(
            db_session,
            test_user,
            test_quote,
            test_requisition,
            status=BuyPlanStatus.ACTIVE.value,
            so_status=SOVerificationStatus.APPROVED.value,
        )
        result = check_completion(plan.id, db_session)
        assert result.status == BuyPlanStatus.ACTIVE.value

    def test_check_completion_not_active(
        self, db_session: Session, test_user: User, test_quote: Quote, test_requisition: Requisition
    ):
        plan = _make_plan(db_session, test_user, test_quote, test_requisition, status=BuyPlanStatus.DRAFT.value)
        result = check_completion(plan.id, db_session)
        assert result.status == BuyPlanStatus.DRAFT.value

    def test_check_completion_not_found(self, db_session: Session):
        result = check_completion(9999, db_session)
        assert result is None


# ── Reset & Resubmit ─────────────────────────────────────────────────


class TestResetAndResubmit:
    """Tests for reset_buy_plan_to_draft() and resubmit_buy_plan()."""

    def test_reset_halted_to_draft(
        self, db_session: Session, test_user: User, test_quote: Quote, test_requisition: Requisition
    ):
        plan = _make_plan(db_session, test_user, test_quote, test_requisition, status=BuyPlanStatus.HALTED.value)
        _make_line(db_session, plan)
        db_session.refresh(plan)

        result = reset_buy_plan_to_draft(plan.id, test_user, db_session)

        assert result.status == BuyPlanStatus.DRAFT.value
        assert result.so_status == SOVerificationStatus.PENDING.value
        assert result.auto_approved is False
        assert result.approved_by_id is None

    def test_reset_cancelled_to_draft(
        self, db_session: Session, test_user: User, test_quote: Quote, test_requisition: Requisition
    ):
        plan = _make_plan(db_session, test_user, test_quote, test_requisition, status=BuyPlanStatus.CANCELLED.value)
        result = reset_buy_plan_to_draft(plan.id, test_user, db_session)
        assert result.status == BuyPlanStatus.DRAFT.value

    def test_reset_not_found(self, db_session: Session, test_user: User):
        with pytest.raises(ValueError, match="not found"):
            reset_buy_plan_to_draft(9999, test_user, db_session)

    def test_reset_wrong_status(
        self, db_session: Session, test_user: User, test_quote: Quote, test_requisition: Requisition
    ):
        plan = _make_plan(db_session, test_user, test_quote, test_requisition, status=BuyPlanStatus.ACTIVE.value)
        with pytest.raises(ValueError, match="Only halted/cancelled"):
            reset_buy_plan_to_draft(plan.id, test_user, db_session)

    def test_resubmit_goes_to_manager(
        self, db_session: Session, test_user: User, test_quote: Quote, test_requisition: Requisition
    ):
        plan = _make_plan(db_session, test_user, test_quote, test_requisition, total_cost=50.00)
        _make_line(db_session, plan)
        db_session.refresh(plan)

        result = resubmit_buy_plan(plan.id, "SO-RESUB", test_user, db_session, customer_po_number="PO-R1")

        assert result.status == BuyPlanStatus.PENDING.value
        assert result.auto_approved is not True
        assert result.sales_order_number == "SO-RESUB"
        assert result.customer_po_number == "PO-R1"

    def test_resubmit_pending(
        self, db_session: Session, test_user: User, test_quote: Quote, test_requisition: Requisition
    ):
        plan = _make_plan(db_session, test_user, test_quote, test_requisition, total_cost=10000.00)
        _make_line(db_session, plan)
        db_session.refresh(plan)

        result = resubmit_buy_plan(plan.id, "SO-RESUB2", test_user, db_session)

        assert result.status == BuyPlanStatus.PENDING.value

    def test_resubmit_not_draft(
        self, db_session: Session, test_user: User, test_quote: Quote, test_requisition: Requisition
    ):
        plan = _make_plan(db_session, test_user, test_quote, test_requisition, status=BuyPlanStatus.ACTIVE.value)
        with pytest.raises(ValueError, match="Can only resubmit draft"):
            resubmit_buy_plan(plan.id, "SO-X", test_user, db_session)

    def test_resubmit_not_found(self, db_session: Session, test_user: User):
        with pytest.raises(ValueError, match="not found"):
            resubmit_buy_plan(9999, "SO-X", test_user, db_session)

    def test_resubmittable_statuses_constant(self):
        assert BuyPlanStatus.HALTED.value in RESUBMITTABLE_STATUSES
        assert BuyPlanStatus.CANCELLED.value in RESUBMITTABLE_STATUSES


# ── Helper Functions ─────────────────────────────────────────────────


class TestHelpers:
    """Tests for private helper functions."""

    def test_recalculate_financials(
        self, db_session: Session, test_user: User, test_quote: Quote, test_requisition: Requisition
    ):
        plan = _make_plan(db_session, test_user, test_quote, test_requisition)
        _make_line(db_session, plan, unit_cost=1.00, unit_sell=2.00, quantity=100)
        _make_line(db_session, plan, unit_cost=0.50, unit_sell=1.00, quantity=200)
        db_session.refresh(plan)

        _recalculate_financials(plan)

        assert float(plan.total_cost) == 200.0
        assert float(plan.total_revenue) == 400.0
        assert float(plan.total_margin_pct) == 50.0

    def test_recalculate_financials_zero_revenue(
        self, db_session: Session, test_user: User, test_quote: Quote, test_requisition: Requisition
    ):
        plan = _make_plan(db_session, test_user, test_quote, test_requisition)
        _make_line(db_session, plan, unit_cost=1.00, unit_sell=None, quantity=100)
        db_session.refresh(plan)

        _recalculate_financials(plan)

        assert float(plan.total_cost) == 100.0
        assert plan.total_revenue is None

    def test_recalculate_financials_no_lines(
        self, db_session: Session, test_user: User, test_quote: Quote, test_requisition: Requisition
    ):
        plan = _make_plan(db_session, test_user, test_quote, test_requisition)
        plan.lines = []
        _recalculate_financials(plan)
        assert plan.total_cost is None

    def test_is_stock_sale_true(
        self, db_session: Session, test_user: User, test_quote: Quote, test_requisition: Requisition, test_offer: Offer
    ):
        test_offer.vendor_name = "trio"
        db_session.flush()

        plan = _make_plan(db_session, test_user, test_quote, test_requisition)
        _make_line(db_session, plan, offer_id=test_offer.id)
        db_session.refresh(plan)

        assert _is_stock_sale(plan, db_session) is True

    def test_is_stock_sale_false(
        self, db_session: Session, test_user: User, test_quote: Quote, test_requisition: Requisition, test_offer: Offer
    ):
        test_offer.vendor_name = "Arrow Electronics"
        db_session.flush()

        plan = _make_plan(db_session, test_user, test_quote, test_requisition)
        _make_line(db_session, plan, offer_id=test_offer.id)
        db_session.refresh(plan)

        assert _is_stock_sale(plan, db_session) is False

    def test_is_stock_sale_no_lines(
        self, db_session: Session, test_user: User, test_quote: Quote, test_requisition: Requisition
    ):
        plan = _make_plan(db_session, test_user, test_quote, test_requisition)
        assert _is_stock_sale(plan, db_session) is False

    def test_is_stock_sale_no_offer(
        self, db_session: Session, test_user: User, test_quote: Quote, test_requisition: Requisition
    ):
        plan = _make_plan(db_session, test_user, test_quote, test_requisition)
        _make_line(db_session, plan, offer_id=None)
        db_session.refresh(plan)
        assert _is_stock_sale(plan, db_session) is False

    def test_generate_buyer_tasks_success(
        self, db_session: Session, test_user: User, test_quote: Quote, test_requisition: Requisition
    ):
        plan = _make_plan(db_session, test_user, test_quote, test_requisition)
        _make_line(db_session, plan, buyer_id=test_user.id)
        db_session.refresh(plan)

        with patch("app.services.task_service.on_buy_plan_assigned") as mock_task:
            _generate_buyer_tasks(plan, db_session)
            assert mock_task.called

    def test_generate_buyer_tasks_no_buyer(
        self, db_session: Session, test_user: User, test_quote: Quote, test_requisition: Requisition
    ):
        plan = _make_plan(db_session, test_user, test_quote, test_requisition)
        _make_line(db_session, plan, buyer_id=None)
        db_session.refresh(plan)

        with patch("app.services.task_service.on_buy_plan_assigned") as mock_task:
            _generate_buyer_tasks(plan, db_session)
            mock_task.assert_not_called()

    def test_generate_buyer_tasks_exception_swallowed(
        self, db_session: Session, test_user: User, test_quote: Quote, test_requisition: Requisition
    ):
        """Task generation failures are logged but don't raise."""
        plan = _make_plan(db_session, test_user, test_quote, test_requisition)
        _make_line(db_session, plan, buyer_id=test_user.id)
        db_session.refresh(plan)

        with patch(
            "app.services.task_service.on_buy_plan_assigned",
            side_effect=Exception("task service down"),
        ):
            # Should not raise
            _generate_buyer_tasks(plan, db_session)

    def test_apply_line_overrides(
        self, db_session: Session, test_user: User, test_quote: Quote, test_requisition: Requisition, test_offer: Offer
    ):
        plan = _make_plan(db_session, test_user, test_quote, test_requisition)
        line = _make_line(db_session, plan, unit_sell=3.00)
        db_session.refresh(plan)

        _apply_line_overrides(
            plan, [{"line_id": line.id, "offer_id": test_offer.id, "quantity": 200, "manager_note": "Swap"}], db_session
        )

        assert line.offer_id == test_offer.id
        assert line.quantity == 200
        assert line.manager_note == "Swap"

    def test_apply_line_overrides_missing_line(
        self, db_session: Session, test_user: User, test_quote: Quote, test_requisition: Requisition
    ):
        """Override with nonexistent line_id is silently skipped."""
        plan = _make_plan(db_session, test_user, test_quote, test_requisition)
        db_session.refresh(plan)

        _apply_line_overrides(plan, [{"line_id": 9999, "offer_id": 1}], db_session)


# ── Favoritism Detection ─────────────────────────────────────────────


class TestDetectFavoritism:
    """Tests for detect_favoritism()."""

    def test_no_data_returns_empty(self, db_session: Session, test_user: User):
        result = detect_favoritism(test_user.id, db_session)
        assert result == []

    def test_too_few_plans(
        self, db_session: Session, test_user: User, test_quote: Quote, test_requisition: Requisition
    ):
        """Less than 3 plans → no findings."""
        plan1 = _make_plan(
            db_session,
            test_user,
            test_quote,
            test_requisition,
            status=BuyPlanStatus.ACTIVE.value,
            submitted_by_id=test_user.id,
        )
        plan2 = _make_plan(
            db_session,
            test_user,
            test_quote,
            test_requisition,
            status=BuyPlanStatus.ACTIVE.value,
            submitted_by_id=test_user.id,
        )
        _make_line(db_session, plan1, buyer_id=test_user.id)
        _make_line(db_session, plan2, buyer_id=test_user.id)

        result = detect_favoritism(test_user.id, db_session)
        assert result == []

    def test_favoritism_detected(
        self, db_session: Session, test_user: User, sales_user: User, test_quote: Quote, test_requisition: Requisition
    ):
        """All lines go to same buyer → flagged."""
        for _ in range(3):
            plan = _make_plan(
                db_session,
                test_user,
                test_quote,
                test_requisition,
                status=BuyPlanStatus.ACTIVE.value,
                submitted_by_id=test_user.id,
            )
            _make_line(db_session, plan, buyer_id=sales_user.id)

        result = detect_favoritism(test_user.id, db_session)
        assert len(result) >= 1
        assert result[0]["pct"] == 100.0
        assert result[0]["buyer_id"] == sales_user.id

    def test_no_favoritism_even_distribution(
        self,
        db_session: Session,
        test_user: User,
        sales_user: User,
        manager_user: User,
        test_quote: Quote,
        test_requisition: Requisition,
    ):
        """Even distribution → no flags (assuming threshold is 60%)."""
        for buyer in [test_user, sales_user, manager_user]:
            plan = _make_plan(
                db_session,
                test_user,
                test_quote,
                test_requisition,
                status=BuyPlanStatus.ACTIVE.value,
                submitted_by_id=test_user.id,
            )
            _make_line(db_session, plan, buyer_id=buyer.id)

        result = detect_favoritism(test_user.id, db_session)
        assert result == []

    def test_favoritism_no_buyer_assignments(
        self, db_session: Session, test_user: User, test_quote: Quote, test_requisition: Requisition
    ):
        """Lines with no buyer → no findings."""
        for _ in range(3):
            plan = _make_plan(
                db_session,
                test_user,
                test_quote,
                test_requisition,
                status=BuyPlanStatus.ACTIVE.value,
                submitted_by_id=test_user.id,
            )
            _make_line(db_session, plan, buyer_id=None)

        result = detect_favoritism(test_user.id, db_session)
        assert result == []


# ── Case Report Generation ───────────────────────────────────────────


class TestCaseReport:
    """Tests for generate_case_report()."""

    def test_basic_report(
        self, db_session: Session, test_user: User, test_quote: Quote, test_requisition: Requisition, test_offer: Offer
    ):
        plan = _make_plan(
            db_session,
            test_user,
            test_quote,
            test_requisition,
            status=BuyPlanStatus.COMPLETED.value,
            submitted_by_id=test_user.id,
            approved_by_id=test_user.id,
            auto_approved=True,
            sales_order_number="SO-RPT-001",
            total_cost=500.00,
            total_revenue=1000.00,
            total_margin_pct=50.0,
            submitted_at=datetime.now(timezone.utc) - timedelta(days=5),
            approved_at=datetime.now(timezone.utc) - timedelta(days=4),
            completed_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc) - timedelta(days=7),
        )
        _make_line(db_session, plan, offer_id=test_offer.id, status=BuyPlanLineStatus.VERIFIED.value)
        db_session.refresh(plan)

        report = generate_case_report(plan, db_session)

        assert "CASE REPORT" in report
        assert "SO-RPT-001" in report
        assert "500.00" in report
        assert "1,000.00" in report
        assert "50.0%" in report

    def test_report_with_issues_and_flags(
        self, db_session: Session, test_user: User, test_quote: Quote, test_requisition: Requisition, test_offer: Offer
    ):
        plan = _make_plan(
            db_session,
            test_user,
            test_quote,
            test_requisition,
            status=BuyPlanStatus.COMPLETED.value,
            ai_flags=[{"severity": "warning", "type": "price_outlier", "message": "Price 30% above market"}],
            so_rejection_note="First SO was wrong",
            created_at=datetime.now(timezone.utc) - timedelta(days=3),
        )
        line = _make_line(
            db_session,
            plan,
            offer_id=test_offer.id,
            status=BuyPlanLineStatus.VERIFIED.value,
            issue_type="sold_out",
            issue_note="Had to find alternative",
            po_rejection_note="Wrong amount",
        )
        db_session.refresh(plan)

        report = generate_case_report(plan, db_session)

        assert "price_outlier" in report
        assert "sold_out" in report
        assert "First SO was wrong" in report
        assert "Wrong amount" in report

    def test_report_no_quote(
        self, db_session: Session, test_user: User, test_quote: Quote, test_requisition: Requisition
    ):
        plan = _make_plan(
            db_session,
            test_user,
            test_quote,
            test_requisition,
            status=BuyPlanStatus.COMPLETED.value,
            created_at=datetime.now(timezone.utc),
        )
        plan.quote_id = test_quote.id  # quote exists but has no customer info
        db_session.refresh(plan)

        report = generate_case_report(plan, db_session)
        assert "CASE REPORT" in report

    def test_report_auto_approved_label(
        self, db_session: Session, test_user: User, test_quote: Quote, test_requisition: Requisition
    ):
        plan = _make_plan(
            db_session,
            test_user,
            test_quote,
            test_requisition,
            auto_approved=True,
            approved_by_id=None,
            created_at=datetime.now(timezone.utc),
        )
        report = generate_case_report(plan, db_session)
        assert "Auto-approved" in report

    def test_report_falls_back_to_requisition_customer_for_so_origin(
        self, db_session: Session, test_user: User, test_quote: Quote, test_requisition: Requisition
    ):
        """SO-origin plan (no quote) renders the requisition's customer, not Unknown."""
        plan = _make_plan(
            db_session,
            test_user,
            test_quote,
            test_requisition,
            quote_id=None,
            status=BuyPlanStatus.COMPLETED.value,
            created_at=datetime.now(timezone.utc),
        )
        db_session.refresh(plan)

        report = generate_case_report(plan, db_session)
        assert "Customer: Acme Electronics" in report


# ── Async PO Verification ───────────────────────────────────────────


class TestVerifyPOSent:
    """Tests for verify_po_sent() async function."""

    def test_skip_no_po_number(
        self, db_session: Session, test_user: User, test_quote: Quote, test_requisition: Requisition
    ):
        plan = _make_plan(db_session, test_user, test_quote, test_requisition, status=BuyPlanStatus.ACTIVE.value)
        _make_line(db_session, plan, po_number=None)
        db_session.refresh(plan)

        results = _run(verify_po_sent(plan, db_session))

        assert len(results) == 1
        assert results[0]["skipped"] is True

    def test_skip_no_buyer(
        self, db_session: Session, test_user: User, test_quote: Quote, test_requisition: Requisition
    ):
        plan = _make_plan(db_session, test_user, test_quote, test_requisition, status=BuyPlanStatus.ACTIVE.value)
        _make_line(db_session, plan, po_number="PO-TEST", buyer_id=None)
        db_session.refresh(plan)

        results = _run(verify_po_sent(plan, db_session))

        assert results[0]["reason"] == "no_buyer"

    @patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value=None)
    def test_skip_no_token(
        self, mock_token, db_session: Session, test_user: User, test_quote: Quote, test_requisition: Requisition
    ):
        plan = _make_plan(db_session, test_user, test_quote, test_requisition, status=BuyPlanStatus.ACTIVE.value)
        _make_line(db_session, plan, po_number="PO-TEST", buyer_id=test_user.id)
        db_session.refresh(plan)

        results = _run(verify_po_sent(plan, db_session))

        assert results[0]["reason"] == "no_token"

    @patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="mock-token")
    @patch("app.utils.graph_client.GraphClient")
    def test_po_found(
        self, MockGC, mock_token, db_session: Session, test_user: User, test_quote: Quote, test_requisition: Requisition
    ):
        mock_client = MagicMock()
        mock_client.search_sent_messages = AsyncMock(return_value=[{"id": "msg1"}])
        MockGC.return_value = mock_client

        plan = _make_plan(
            db_session,
            test_user,
            test_quote,
            test_requisition,
            status=BuyPlanStatus.ACTIVE.value,
            so_status=SOVerificationStatus.APPROVED.value,
        )
        _make_line(
            db_session, plan, po_number="PO-FOUND", buyer_id=test_user.id, status=BuyPlanLineStatus.PENDING_VERIFY.value
        )
        db_session.refresh(plan)

        results = _run(verify_po_sent(plan, db_session))

        assert results[0]["found"] is True
        assert results[0]["message_count"] == 1

    @patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="mock-token")
    @patch("app.utils.graph_client.GraphClient")
    def test_po_not_found(
        self, MockGC, mock_token, db_session: Session, test_user: User, test_quote: Quote, test_requisition: Requisition
    ):
        mock_client = MagicMock()
        mock_client.search_sent_messages = AsyncMock(return_value=[])
        MockGC.return_value = mock_client

        plan = _make_plan(db_session, test_user, test_quote, test_requisition, status=BuyPlanStatus.ACTIVE.value)
        _make_line(
            db_session,
            plan,
            po_number="PO-MISSING",
            buyer_id=test_user.id,
            status=BuyPlanLineStatus.PENDING_VERIFY.value,
        )
        db_session.refresh(plan)

        results = _run(verify_po_sent(plan, db_session))

        assert results[0]["found"] is False

    @patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, side_effect=Exception("API down"))
    def test_graph_api_error(
        self, mock_token, db_session: Session, test_user: User, test_quote: Quote, test_requisition: Requisition
    ):
        plan = _make_plan(db_session, test_user, test_quote, test_requisition, status=BuyPlanStatus.ACTIVE.value)
        _make_line(db_session, plan, po_number="PO-ERR", buyer_id=test_user.id)
        db_session.refresh(plan)

        results = _run(verify_po_sent(plan, db_session))

        assert results[0]["found"] is False
        assert "error" in results[0]


class TestCancelBuyPlan:
    """Tests for cancel_buy_plan() — BUG-3: service-layer cancel with line cascade."""

    def test_cancel_active_cascades_open_lines(
        self, db_session: Session, test_user: User, test_quote: Quote, test_requisition: Requisition
    ):
        plan = _make_plan(db_session, test_user, test_quote, test_requisition, status=BuyPlanStatus.ACTIVE.value)
        awaiting = _make_line(db_session, plan, status=BuyPlanLineStatus.AWAITING_PO.value)
        pending = _make_line(db_session, plan, status=BuyPlanLineStatus.PENDING_VERIFY.value)
        verified = _make_line(db_session, plan, status=BuyPlanLineStatus.VERIFIED.value)

        result = cancel_buy_plan(plan.id, test_user, db_session, reason="customer pulled out")

        assert result.status == BuyPlanStatus.CANCELLED.value
        assert result.cancelled_by_id == test_user.id
        assert result.cancellation_reason == "customer pulled out"
        assert result.cancelled_at is not None
        for ln in (awaiting, pending, verified):
            db_session.refresh(ln)
        assert awaiting.status == BuyPlanLineStatus.CANCELLED.value
        assert pending.status == BuyPlanLineStatus.CANCELLED.value
        assert verified.status == BuyPlanLineStatus.VERIFIED.value  # terminal line untouched

    def test_cancel_rejects_completed(
        self, db_session: Session, test_user: User, test_quote: Quote, test_requisition: Requisition
    ):
        plan = _make_plan(db_session, test_user, test_quote, test_requisition, status=BuyPlanStatus.COMPLETED.value)
        with pytest.raises(ValueError, match="Cannot cancel"):
            cancel_buy_plan(plan.id, test_user, db_session)

    def test_cancel_rejects_already_cancelled(
        self, db_session: Session, test_user: User, test_quote: Quote, test_requisition: Requisition
    ):
        plan = _make_plan(db_session, test_user, test_quote, test_requisition, status=BuyPlanStatus.CANCELLED.value)
        with pytest.raises(ValueError, match="Cannot cancel"):
            cancel_buy_plan(plan.id, test_user, db_session)

    def test_cancel_not_found(self, db_session: Session, test_user: User):
        with pytest.raises(ValueError, match="not found"):
            cancel_buy_plan(99999, test_user, db_session)


class TestVerifyPoRejectClearsNudge:
    """Review fix: PO-reject re-activates the line, so last_nudge_at must reset."""

    def test_reject_clears_last_nudge_at(
        self, db_session: Session, test_user: User, test_quote: Quote, test_requisition: Requisition
    ):
        plan = _make_plan(db_session, test_user, test_quote, test_requisition, status=BuyPlanStatus.ACTIVE.value)
        line = _make_line(
            db_session,
            plan,
            status=BuyPlanLineStatus.PENDING_VERIFY.value,
            po_number="PO-1",
            last_nudge_at=datetime.now(timezone.utc),
        )
        _grant_po_approver(db_session, test_user)

        verify_po(plan.id, line.id, "reject", test_user, db_session, rejection_note="wrong vendor")

        db_session.refresh(line)
        assert line.status == BuyPlanLineStatus.AWAITING_PO.value
        assert line.last_nudge_at is None


class TestResolveLineIssue:
    """resolve_line_issue() — clears a flagged issue back to awaiting_po (supervisor-
    gated)."""

    def test_supervisor_resolves_issue_to_awaiting_po(
        self, db_session: Session, manager_user: User, test_user: User, test_quote: Quote, test_requisition: Requisition
    ):
        plan = _make_plan(db_session, test_user, test_quote, test_requisition, status=BuyPlanStatus.ACTIVE.value)
        line = _make_line(
            db_session, plan, status=BuyPlanLineStatus.ISSUE.value, issue_type="price_changed", issue_note="up 20%"
        )

        result = resolve_line_issue(plan.id, line.id, manager_user, db_session)

        assert result.status == BuyPlanLineStatus.AWAITING_PO.value
        assert result.issue_type is None
        assert result.issue_note is None

    def test_resolve_clears_stale_po_fields(
        self, db_session: Session, manager_user: User, test_user: User, test_quote: Quote, test_requisition: Requisition
    ):
        """A PENDING_VERIFY line flagged then resolved must land in a clean awaiting_po
        (no confirmed PO), so the buyer re-confirms rather than inheriting a stale
        PO#."""
        plan = _make_plan(db_session, test_user, test_quote, test_requisition, status=BuyPlanStatus.ACTIVE.value)
        line = _make_line(
            db_session,
            plan,
            status=BuyPlanLineStatus.ISSUE.value,
            issue_type="sold_out",
            po_number="PO-STALE",
            po_confirmed_at=datetime.now(timezone.utc),
        )

        result = resolve_line_issue(plan.id, line.id, manager_user, db_session)

        assert result.status == BuyPlanLineStatus.AWAITING_PO.value
        assert result.po_number is None
        assert result.po_confirmed_at is None

    def test_non_supervisor_cannot_resolve(
        self, db_session: Session, test_user: User, test_quote: Quote, test_requisition: Requisition
    ):
        """The buyer who raised the issue can't self-resolve (matches the
        flagged→supervisor My Queue routing) — a plain buyer is refused."""
        plan = _make_plan(db_session, test_user, test_quote, test_requisition, status=BuyPlanStatus.ACTIVE.value)
        line = _make_line(db_session, plan, status=BuyPlanLineStatus.ISSUE.value, issue_type="other")

        with pytest.raises(PermissionError):
            resolve_line_issue(plan.id, line.id, test_user, db_session)

    def test_resolve_non_issue_line_rejected(
        self, db_session: Session, manager_user: User, test_user: User, test_quote: Quote, test_requisition: Requisition
    ):
        plan = _make_plan(db_session, test_user, test_quote, test_requisition, status=BuyPlanStatus.ACTIVE.value)
        line = _make_line(db_session, plan, status=BuyPlanLineStatus.AWAITING_PO.value)

        with pytest.raises(ValueError, match="no issue to resolve"):
            resolve_line_issue(plan.id, line.id, manager_user, db_session)


class TestPlanNeedsApproverReason:
    """plan_needs_approver_reason() — detects a plan silently stalled for lack of an
    approver."""

    def test_pending_no_buy_plan_approver(
        self, db_session: Session, test_user: User, test_quote: Quote, test_requisition: Requisition
    ):
        plan = _make_plan(db_session, test_user, test_quote, test_requisition, status=BuyPlanStatus.PENDING.value)
        assert plan_needs_approver_reason(plan, db_session) == "buy_plan"

    def test_pending_with_approver_returns_none(
        self, db_session: Session, manager_user: User, test_user: User, test_quote: Quote, test_requisition: Requisition
    ):
        _grant_approver(db_session, manager_user)
        plan = _make_plan(db_session, test_user, test_quote, test_requisition, status=BuyPlanStatus.PENDING.value)
        assert plan_needs_approver_reason(plan, db_session) is None

    def test_active_pending_verify_line_no_po_approver(
        self, db_session: Session, test_user: User, test_quote: Quote, test_requisition: Requisition
    ):
        """Phase 3: the PO stall is per PENDING_VERIFY line — a cut PO awaiting sign-off
        with no purchase-order approver stalls the plan, whatever the plan total."""
        plan = _make_plan(
            db_session, test_user, test_quote, test_requisition, status=BuyPlanStatus.ACTIVE.value, total_cost=100.00
        )
        _make_line(db_session, plan, status=BuyPlanLineStatus.PENDING_VERIFY.value)
        assert plan_needs_approver_reason(plan, db_session) == "purchase_order"

    def test_active_without_pending_verify_line_returns_none(
        self, db_session: Session, test_user: User, test_quote: Quote, test_requisition: Requisition
    ):
        """No PENDING_VERIFY line → no PO stall, even with no approver and a big
        total."""
        plan = _make_plan(
            db_session, test_user, test_quote, test_requisition, status=BuyPlanStatus.ACTIVE.value, total_cost=10000.00
        )
        _make_line(db_session, plan, status=BuyPlanLineStatus.AWAITING_PO.value)
        assert plan_needs_approver_reason(plan, db_session) is None

    def test_draft_returns_none(
        self, db_session: Session, test_user: User, test_quote: Quote, test_requisition: Requisition
    ):
        plan = _make_plan(db_session, test_user, test_quote, test_requisition, status=BuyPlanStatus.DRAFT.value)
        assert plan_needs_approver_reason(plan, db_session) is None
