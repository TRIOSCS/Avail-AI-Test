"""
test_buyplan_workflow_bugs.py — Tests for Phase 1 bug fixes in buyplan_workflow.py

Covers:
- check_completion idempotency (no duplicate case reports)
- approve_buy_plan requires manager/admin role
- _should_auto_approve consistency between submit and resubmit

Called by: pytest
Depends on: conftest fixtures, app/services/buyplan_workflow.py
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session

from app.models import Company, CustomerSite, Quote, Requirement, Requisition, User
from app.models.buy_plan import (
    BuyPlanLine,
    BuyPlanLineStatus,
    BuyPlanStatus,
    BuyPlan,
    SOVerificationStatus,
)
from app.services.buyplan_workflow import (
    _should_auto_approve,
    approve_buy_plan,
    check_completion,
    resubmit_buy_plan,
    submit_buy_plan,
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

    line_statuses = line_statuses or [BuyPlanLineStatus.awaiting_po.value]
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
        """Calling check_completion on an already-completed plan should not
        regenerate the case report."""
        plan, _ = _make_plan_with_lines(
            db_session,
            status=BuyPlanStatus.completed.value,
            so_status=SOVerificationStatus.approved.value,
            line_statuses=[BuyPlanLineStatus.verified.value],
        )
        plan.case_report = "EXISTING REPORT"
        plan.completed_at = datetime.now(timezone.utc)
        db_session.flush()

        result = check_completion(plan.id, db_session)

        assert result.status == BuyPlanStatus.completed.value
        assert result.case_report == "EXISTING REPORT"

    def test_completes_when_all_lines_terminal(self, db_session):
        """Plan should auto-complete when all lines verified and SO approved."""
        plan, _ = _make_plan_with_lines(
            db_session,
            status=BuyPlanStatus.active.value,
            so_status=SOVerificationStatus.approved.value,
            line_statuses=[BuyPlanLineStatus.verified.value, BuyPlanLineStatus.cancelled.value],
        )

        result = check_completion(plan.id, db_session)

        assert result.status == BuyPlanStatus.completed.value
        assert result.completed_at is not None
        assert result.case_report is not None

    def test_does_not_complete_with_pending_lines(self, db_session):
        """Plan should NOT complete if any line is still in progress."""
        plan, _ = _make_plan_with_lines(
            db_session,
            status=BuyPlanStatus.active.value,
            so_status=SOVerificationStatus.approved.value,
            line_statuses=[BuyPlanLineStatus.verified.value, BuyPlanLineStatus.awaiting_po.value],
        )

        result = check_completion(plan.id, db_session)

        assert result.status == BuyPlanStatus.active.value
        assert result.completed_at is None

    def test_does_not_complete_without_so_approval(self, db_session):
        """Plan should NOT complete if SO is not yet approved."""
        plan, _ = _make_plan_with_lines(
            db_session,
            status=BuyPlanStatus.active.value,
            so_status=SOVerificationStatus.pending.value,
            line_statuses=[BuyPlanLineStatus.verified.value],
        )

        result = check_completion(plan.id, db_session)

        assert result.status == BuyPlanStatus.active.value


# ── approve_buy_plan role check ────────────────────────────────────────


class TestApproveBuyPlanRoleCheck:
    def test_buyer_cannot_approve(self, db_session):
        """Buyer role should be rejected from approving buy plans."""
        plan, user = _make_plan_with_lines(
            db_session,
            status=BuyPlanStatus.pending.value,
        )
        # user has role="sales", should fail
        with pytest.raises(PermissionError, match="Only managers/admins"):
            approve_buy_plan(plan.id, "approve", user, db_session)

    def test_manager_can_approve(self, db_session):
        """Manager should be allowed to approve buy plans."""
        plan, user = _make_plan_with_lines(
            db_session,
            status=BuyPlanStatus.pending.value,
        )
        user.role = "manager"
        db_session.flush()

        result = approve_buy_plan(plan.id, "approve", user, db_session)

        assert result.status == BuyPlanStatus.active.value
        assert result.approved_by_id == user.id

    def test_admin_can_approve(self, db_session):
        """Admin should be allowed to approve buy plans."""
        plan, user = _make_plan_with_lines(
            db_session,
            status=BuyPlanStatus.pending.value,
        )
        user.role = "admin"
        db_session.flush()

        result = approve_buy_plan(plan.id, "approve", user, db_session)

        assert result.status == BuyPlanStatus.active.value

    def test_manager_can_reject(self, db_session):
        """Manager should be allowed to reject buy plans back to draft."""
        plan, user = _make_plan_with_lines(
            db_session,
            status=BuyPlanStatus.pending.value,
        )
        user.role = "manager"
        db_session.flush()

        result = approve_buy_plan(plan.id, "reject", user, db_session, notes="Needs revision")

        assert result.status == BuyPlanStatus.draft.value
        assert result.approval_notes == "Needs revision"


# ── _should_auto_approve consistency ──────────────────────────────────


class TestShouldAutoApprove:
    def test_low_cost_no_flags_auto_approves(self, db_session):
        plan, _ = _make_plan_with_lines(
            db_session,
            total_cost=100.0,
            ai_flags=[],
        )
        assert _should_auto_approve(plan) is True

    def test_high_cost_does_not_auto_approve(self, db_session):
        plan, _ = _make_plan_with_lines(
            db_session,
            total_cost=10000.0,
            ai_flags=[],
        )
        assert _should_auto_approve(plan) is False

    def test_critical_flags_prevent_auto_approve(self, db_session):
        plan, _ = _make_plan_with_lines(
            db_session,
            total_cost=100.0,
            ai_flags=[{"type": "stale_offer", "severity": "critical", "message": "test"}],
        )
        assert _should_auto_approve(plan) is False

    def test_warning_flags_allow_auto_approve(self, db_session):
        plan, _ = _make_plan_with_lines(
            db_session,
            total_cost=100.0,
            ai_flags=[{"type": "low_margin", "severity": "warning", "message": "test"}],
        )
        assert _should_auto_approve(plan) is True

    def test_submit_and_resubmit_use_same_logic(self, db_session):
        """Verify submit and resubmit produce same auto-approve decision."""
        plan, user = _make_plan_with_lines(
            db_session,
            total_cost=100.0,
            ai_flags=[],
        )
        result1 = submit_buy_plan(plan.id, "SO-001", user, db_session)
        status_after_submit = result1.status
        auto_after_submit = result1.auto_approved

        # Reset to draft for resubmit
        plan.status = BuyPlanStatus.draft.value
        plan.auto_approved = False
        plan.approved_at = None
        db_session.flush()

        result2 = resubmit_buy_plan(plan.id, "SO-002", user, db_session)
        assert result2.status == status_after_submit
        assert result2.auto_approved == auto_after_submit
