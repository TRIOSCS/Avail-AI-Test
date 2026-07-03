"""test_buyplan_workflow_bugs.py — Tests for Phase 1 bug fixes in buyplan_workflow.py.

Covers:
- check_completion idempotency (no duplicate case reports)
- approve_buy_plan requires manager/admin role

Called by: pytest
Depends on: conftest fixtures, app/services/buyplan_workflow.py
"""

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.constants import ApprovalGateType, ApprovalRequestStatus, ApprovalSubjectType
from app.models import Company, CustomerSite, Quote, Requirement, Requisition, User
from app.models.approvals import ApprovalRequest
from app.models.buy_plan import (
    BuyPlan,
    BuyPlanLine,
    BuyPlanLineStatus,
    BuyPlanStatus,
    SOVerificationStatus,
)
from app.services.buyplan_workflow import (
    approve_buy_plan,
    check_completion,
)

# ── Helpers ────────────────────────────────────────────────────────────


def _make_plan_with_lines(
    db: Session, *, status="draft", so_status="pending", line_statuses=None, total_cost=100.0, ai_flags=None
):
    """Create a BuyPlan with associated records for testing."""
    user = User(
        email="test@trioscs.com",
        name="Test",
        role="sales",
        azure_id="az-test",
        created_at=datetime.now(timezone.utc),
    )
    db.add(user)
    db.flush()

    company = Company(
        name="Test Corp",
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(company)
    db.flush()

    site = CustomerSite(
        company_id=company.id,
        site_name="HQ",
        created_at=datetime.now(timezone.utc),
    )
    db.add(site)
    db.flush()

    req = Requisition(
        name="REQ-WF-TEST",
        status="won",
        created_by=user.id,
        customer_site_id=site.id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(req)
    db.flush()

    requirement = Requirement(
        requisition_id=req.id,
        primary_mpn="TEST-MPN",
        target_qty=100,
        target_price=1.0,
        created_at=datetime.now(timezone.utc),
    )
    db.add(requirement)
    db.flush()

    quote = Quote(
        requisition_id=req.id,
        customer_site_id=site.id,
        quote_number="Q-WF-001",
        status="won",
        created_by_id=user.id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(quote)
    db.flush()

    plan = BuyPlan(
        quote_id=quote.id,
        requisition_id=req.id,
        status=status,
        so_status=so_status,
        total_cost=total_cost,
        ai_flags=ai_flags or [],
        created_at=datetime.now(timezone.utc),
    )
    db.add(plan)
    db.flush()

    line_statuses = line_statuses or [BuyPlanLineStatus.AWAITING_PO.value]
    for ls in line_statuses:
        line = BuyPlanLine(
            buy_plan_id=plan.id,
            requirement_id=requirement.id,
            quantity=100,
            unit_cost=1.0,
            status=ls,
        )
        db.add(line)

    db.flush()
    return plan, user


# ── check_completion idempotency ───────────────────────────────────────


class TestCheckCompletionIdempotency:
    def test_already_completed_returns_early(self, db_session):
        """Calling check_completion on an already-completed plan should not regenerate
        the case report."""
        plan, _ = _make_plan_with_lines(
            db_session,
            status=BuyPlanStatus.COMPLETED.value,
            so_status=SOVerificationStatus.APPROVED.value,
            line_statuses=[BuyPlanLineStatus.VERIFIED.value],
        )
        plan.case_report = "EXISTING REPORT"
        plan.completed_at = datetime.now(timezone.utc)
        db_session.flush()

        result = check_completion(plan.id, db_session)

        assert result.status == BuyPlanStatus.COMPLETED.value
        assert result.case_report == "EXISTING REPORT"

    def test_completes_when_all_lines_terminal(self, db_session):
        """Plan should auto-complete when all lines verified and SO approved."""
        plan, _ = _make_plan_with_lines(
            db_session,
            status=BuyPlanStatus.ACTIVE.value,
            so_status=SOVerificationStatus.APPROVED.value,
            line_statuses=[BuyPlanLineStatus.VERIFIED.value, BuyPlanLineStatus.CANCELLED.value],
        )

        result = check_completion(plan.id, db_session)

        assert result.status == BuyPlanStatus.COMPLETED.value
        assert result.completed_at is not None
        assert result.case_report is not None

    def test_does_not_complete_with_pending_lines(self, db_session):
        """Plan should NOT complete if any line is still in progress."""
        plan, _ = _make_plan_with_lines(
            db_session,
            status=BuyPlanStatus.ACTIVE.value,
            so_status=SOVerificationStatus.APPROVED.value,
            line_statuses=[BuyPlanLineStatus.VERIFIED.value, BuyPlanLineStatus.AWAITING_PO.value],
        )

        result = check_completion(plan.id, db_session)

        assert result.status == BuyPlanStatus.ACTIVE.value
        assert result.completed_at is None

    def test_completion_cancels_open_po_gate(self, db_session):
        """Auto-completing via the line flow cancels an orphaned open PURCHASE_ORDER
        gate.

        Regression: the line flow can run to terminal before a deal-level PO approver
        decides. _complete_plan left that PURCHASE_ORDER ApprovalRequest REQUESTED — a
        later decision hit the plan.status != ACTIVE guard (400) and the SP-3 large-PO
        sign-off was silently bypassed. Completion must close the gate (like cancel/halt).
        """
        plan, user = _make_plan_with_lines(
            db_session,
            status=BuyPlanStatus.ACTIVE.value,
            so_status=SOVerificationStatus.APPROVED.value,
            line_statuses=[BuyPlanLineStatus.VERIFIED.value],
            total_cost=25000.0,
        )
        # Deal-level PO gate still open when the line flow reaches terminal.
        po_req = ApprovalRequest(
            gate_type=ApprovalGateType.PURCHASE_ORDER,
            status=ApprovalRequestStatus.REQUESTED,
            subject_type=ApprovalSubjectType.BUY_PLAN,
            subject_id=plan.id,
            amount=Decimal("25000"),
            requested_by_id=user.id,
            owner_id=user.id,
        )
        db_session.add(po_req)
        db_session.flush()

        result = check_completion(plan.id, db_session)

        assert result.status == BuyPlanStatus.COMPLETED.value
        db_session.refresh(po_req)
        # The orphan is closed, not left REQUESTED in the approvals queue.
        assert po_req.status == ApprovalRequestStatus.CANCELLED

    def test_does_not_complete_without_so_approval(self, db_session):
        """Plan should NOT complete if SO is not yet approved."""
        plan, _ = _make_plan_with_lines(
            db_session,
            status=BuyPlanStatus.ACTIVE.value,
            so_status=SOVerificationStatus.PENDING.value,
            line_statuses=[BuyPlanLineStatus.VERIFIED.value],
        )

        result = check_completion(plan.id, db_session)

        assert result.status == BuyPlanStatus.ACTIVE.value


# ── approve_buy_plan role check ────────────────────────────────────────


class TestApproveBuyPlanRoleCheck:
    """Approval is gated by the per-user can_approve_buy_plans right, NOT by role."""

    def test_user_without_right_cannot_approve(self, db_session):
        """A user lacking the approval right is rejected even with a manager role."""
        plan, user = _make_plan_with_lines(
            db_session,
            status=BuyPlanStatus.PENDING.value,
        )
        user.role = "manager"  # role alone no longer qualifies
        db_session.flush()
        with pytest.raises(PermissionError, match="approval right required"):
            approve_buy_plan(plan.id, "approve", user, db_session)

    def test_approver_right_can_approve(self, db_session):
        """A user holding the approval right can approve buy plans."""
        plan, user = _make_plan_with_lines(
            db_session,
            status=BuyPlanStatus.PENDING.value,
        )
        user.can_approve_buy_plans = True
        db_session.flush()

        result = approve_buy_plan(plan.id, "approve", user, db_session)

        assert result.status == BuyPlanStatus.ACTIVE.value
        assert result.approved_by_id == user.id

    def test_approver_right_independent_of_role(self, db_session):
        """The right grants access regardless of the user's role string."""
        plan, user = _make_plan_with_lines(
            db_session,
            status=BuyPlanStatus.PENDING.value,
        )
        user.role = "buyer"
        user.can_approve_buy_plans = True
        db_session.flush()

        result = approve_buy_plan(plan.id, "approve", user, db_session)

        assert result.status == BuyPlanStatus.ACTIVE.value

    def test_approver_can_reject(self, db_session):
        """An approver can reject buy plans back to draft (with a reason)."""
        plan, user = _make_plan_with_lines(
            db_session,
            status=BuyPlanStatus.PENDING.value,
        )
        user.can_approve_buy_plans = True
        db_session.flush()

        result = approve_buy_plan(plan.id, "reject", user, db_session, notes="Needs revision")

        assert result.status == BuyPlanStatus.DRAFT.value
        assert result.approval_notes == "Needs revision"
