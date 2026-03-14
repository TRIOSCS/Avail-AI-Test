"""
test_e2e_sourcing_flow.py — End-to-end smoke tests for the sourcing → buy plan workflow

Covers the full internal UAT path:
1. Requisition/requirement lifecycle transitions
2. Offer creation → requirement status advance
3. Quote creation → requirement status advance
4. Buy plan V3: build → submit → approve → PO confirm → PO verify → SO verify → complete
5. Rejection → resubmission flow
6. Illegal transition rejection

Called by: pytest
Depends on: conftest fixtures, all service layer modules
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session

from app.models import Company, CustomerSite, Offer, Quote, Requirement, Requisition, User, VendorCard
from app.models.buy_plan import (
    BuyPlanLine,
    BuyPlanLineStatus,
    BuyPlanStatus,
    BuyPlanV3,
    SOVerificationStatus,
    VerificationGroupMember,
)
from app.services.buyplan_builder import build_buy_plan
from app.services.buyplan_workflow import (
    approve_buy_plan,
    check_completion,
    confirm_po,
    flag_line_issue,
    resubmit_buy_plan,
    submit_buy_plan,
    verify_po,
    verify_so,
)
from app.services.requirement_status import (
    on_offer_created,
    on_quote_built,
    transition_requirement,
)
from app.services.requisition_state import transition as req_transition

pytestmark = pytest.mark.slow

# ── Fixtures ──────────────────────────────────────────────────────────


def _full_setup(db: Session):
    """Create complete test data for end-to-end workflow testing."""
    sales = User(
        email="sales@trioscs.com",
        name="Sales Rep",
        role="sales",
        azure_id="az-sales-e2e",
        created_at=datetime.now(timezone.utc),
    )
    manager = User(
        email="manager@trioscs.com",
        name="Manager",
        role="manager",
        azure_id="az-mgr-e2e",
        created_at=datetime.now(timezone.utc),
    )
    buyer = User(
        email="buyer@trioscs.com",
        name="Buyer",
        role="buyer",
        azure_id="az-buyer-e2e",
        created_at=datetime.now(timezone.utc),
    )
    ops = User(
        email="ops@trioscs.com",
        name="Ops",
        role="admin",
        azure_id="az-ops-e2e",
        created_at=datetime.now(timezone.utc),
    )
    db.add_all([sales, manager, buyer, ops])
    db.flush()

    # Add ops to verification group
    vg = VerificationGroupMember(user_id=ops.id, is_active=True)
    db.add(vg)

    company = Company(
        name="Acme Electronics",
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(company)
    db.flush()

    site = CustomerSite(
        company_id=company.id,
        site_name="Main",
        created_at=datetime.now(timezone.utc),
    )
    db.add(site)
    db.flush()

    vendor = VendorCard(
        normalized_name="arrow electronics",
        display_name="Arrow Electronics",
        created_at=datetime.now(timezone.utc),
    )
    db.add(vendor)
    db.flush()

    req = Requisition(
        name="REQ-E2E-001",
        status="draft",
        created_by=sales.id,
        customer_site_id=site.id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(req)
    db.flush()

    requirement = Requirement(
        requisition_id=req.id,
        primary_mpn="LM317T",
        target_qty=1000,
        target_price=1.50,
        sourcing_status="open",
        created_at=datetime.now(timezone.utc),
    )
    db.add(requirement)
    db.flush()

    return {
        "sales": sales,
        "manager": manager,
        "buyer": buyer,
        "ops": ops,
        "company": company,
        "site": site,
        "vendor": vendor,
        "requisition": req,
        "requirement": requirement,
    }


# ── Requisition lifecycle ─────────────────────────────────────────────


class TestRequisitionLifecycle:
    def test_draft_to_active(self, db_session):
        data = _full_setup(db_session)
        req = data["requisition"]
        req_transition(req, "active", data["sales"], db_session)
        assert req.status == "active"

    def test_active_to_sourcing(self, db_session):
        data = _full_setup(db_session)
        req = data["requisition"]
        req_transition(req, "active", data["sales"], db_session)
        req_transition(req, "sourcing", data["sales"], db_session)
        assert req.status == "sourcing"

    def test_illegal_transition_rejected(self, db_session):
        data = _full_setup(db_session)
        req = data["requisition"]
        with pytest.raises(ValueError, match="Invalid transition"):
            req_transition(req, "completed", data["sales"], db_session)

    def test_won_to_active_allowed(self, db_session):
        """Regression: toggle archive from won back to active must work."""
        data = _full_setup(db_session)
        req = data["requisition"]
        req_transition(req, "active", data["sales"], db_session)
        req_transition(req, "won", data["sales"], db_session)
        req_transition(req, "active", data["sales"], db_session)
        assert req.status == "active"


# ── Requirement sourcing status ───────────────────────────────────────


class TestRequirementStatusProgression:
    def test_offer_advances_to_offered(self, db_session):
        data = _full_setup(db_session)
        requirement = data["requirement"]
        assert requirement.sourcing_status == "open"

        changed = on_offer_created(requirement, db_session, actor=data["sales"])
        assert changed is True
        assert requirement.sourcing_status == "offered"

    def test_quote_advances_to_quoted(self, db_session):
        data = _full_setup(db_session)
        requirement = data["requirement"]
        transition_requirement(requirement, "offered", db_session, actor=data["sales"])

        count = on_quote_built([requirement.id], db_session, actor=data["sales"])
        assert count == 1
        assert requirement.sourcing_status == "quoted"


# ── Full buy plan V3 lifecycle ────────────────────────────────────────


class TestBuyPlanV3FullLifecycle:
    def _setup_plan(self, db_session, *, total_cost=100.0):
        """Create a complete buy plan ready for submission."""
        data = _full_setup(db_session)

        offer = Offer(
            requisition_id=data["requisition"].id,
            requirement_id=data["requirement"].id,
            vendor_card_id=data["vendor"].id,
            vendor_name="Arrow Electronics",
            mpn="LM317T",
            qty_available=1000,
            unit_price=0.50,
            status="active",
            entered_by_id=data["buyer"].id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(offer)
        db_session.flush()

        quote = Quote(
            requisition_id=data["requisition"].id,
            customer_site_id=data["site"].id,
            quote_number="Q-E2E-001",
            status="won",
            created_by_id=data["sales"].id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(quote)
        db_session.flush()

        plan = BuyPlanV3(
            quote_id=quote.id,
            requisition_id=data["requisition"].id,
            status=BuyPlanStatus.draft.value,
            total_cost=total_cost,
            ai_flags=[],
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(plan)
        db_session.flush()

        line = BuyPlanLine(
            buy_plan_id=plan.id,
            requirement_id=data["requirement"].id,
            offer_id=offer.id,
            quantity=1000,
            unit_cost=0.50,
            unit_sell=1.50,
            buyer_id=data["buyer"].id,
            assignment_reason="manual",
            status=BuyPlanLineStatus.awaiting_po.value,
        )
        db_session.add(line)
        db_session.flush()

        return {**data, "offer": offer, "quote": quote, "plan": plan, "line": line}

    def test_full_happy_path(self, db_session):
        """Complete flow: submit → approve → PO confirm → PO verify → SO verify → complete."""
        ctx = self._setup_plan(db_session)
        plan = ctx["plan"]
        line = ctx["line"]

        # Submit
        plan = submit_buy_plan(plan.id, "SO-001", ctx["sales"], db_session)
        assert plan.status in (BuyPlanStatus.active.value, BuyPlanStatus.pending.value)

        # Auto-approved since total_cost=100 < threshold
        assert plan.status == BuyPlanStatus.active.value
        assert plan.auto_approved is True

        # Confirm PO
        line = confirm_po(
            plan.id, line.id, "PO-12345", datetime(2026, 4, 1, tzinfo=timezone.utc), ctx["buyer"], db_session
        )
        assert line.status == BuyPlanLineStatus.pending_verify.value

        # Verify PO
        line = verify_po(plan.id, line.id, "approve", ctx["ops"], db_session)
        assert line.status == BuyPlanLineStatus.verified.value

        # Verify SO
        plan = verify_so(plan.id, "approve", ctx["ops"], db_session)
        assert plan.so_status == SOVerificationStatus.approved.value

        # Check completion
        db_session.refresh(plan)
        plan = check_completion(plan.id, db_session)
        assert plan.status == BuyPlanStatus.completed.value
        assert plan.case_report is not None

    def test_manager_approval_flow(self, db_session):
        """High-cost plan requires manager approval."""
        ctx = self._setup_plan(db_session, total_cost=10000.0)
        plan = ctx["plan"]

        # Submit → pending (high cost)
        plan = submit_buy_plan(plan.id, "SO-002", ctx["sales"], db_session)
        assert plan.status == BuyPlanStatus.pending.value

        # Manager approves
        plan = approve_buy_plan(plan.id, "approve", ctx["manager"], db_session)
        assert plan.status == BuyPlanStatus.active.value

    def test_rejection_resubmit_flow(self, db_session):
        """Submit → reject → resubmit → approve."""
        ctx = self._setup_plan(db_session, total_cost=10000.0)
        plan = ctx["plan"]

        # Submit
        plan = submit_buy_plan(plan.id, "SO-003", ctx["sales"], db_session)
        assert plan.status == BuyPlanStatus.pending.value

        # Reject
        plan = approve_buy_plan(plan.id, "reject", ctx["manager"], db_session, notes="Needs better margin")
        assert plan.status == BuyPlanStatus.draft.value

        # Resubmit
        plan = resubmit_buy_plan(plan.id, "SO-003-R2", ctx["sales"], db_session)
        assert plan.status == BuyPlanStatus.pending.value

        # Approve
        plan = approve_buy_plan(plan.id, "approve", ctx["manager"], db_session)
        assert plan.status == BuyPlanStatus.active.value

    def test_issue_flag_prevents_completion(self, db_session):
        """Flagged line should prevent auto-completion."""
        ctx = self._setup_plan(db_session)
        plan = ctx["plan"]
        line = ctx["line"]

        plan = submit_buy_plan(plan.id, "SO-004", ctx["sales"], db_session)
        assert plan.status == BuyPlanStatus.active.value

        # Flag issue on line
        line = flag_line_issue(plan.id, line.id, "price_changed", ctx["buyer"], db_session, note="Price went up 20%")
        assert line.status == BuyPlanLineStatus.issue.value

        # SO approved
        plan = verify_so(plan.id, "approve", ctx["ops"], db_session)

        # Check completion — should NOT complete (line has issue)
        db_session.refresh(plan)
        plan = check_completion(plan.id, db_session)
        assert plan.status == BuyPlanStatus.active.value  # not completed

    def test_buyer_cannot_approve(self, db_session):
        """Non-manager/admin users cannot approve buy plans."""
        ctx = self._setup_plan(db_session, total_cost=10000.0)
        plan = ctx["plan"]

        plan = submit_buy_plan(plan.id, "SO-005", ctx["sales"], db_session)
        assert plan.status == BuyPlanStatus.pending.value

        with pytest.raises(PermissionError, match="Only managers/admins"):
            approve_buy_plan(plan.id, "approve", ctx["buyer"], db_session)

    def test_po_reject_resubmit_cycle(self, db_session):
        """PO rejection sends line back to awaiting_po."""
        ctx = self._setup_plan(db_session)
        plan = ctx["plan"]
        line = ctx["line"]

        plan = submit_buy_plan(plan.id, "SO-006", ctx["sales"], db_session)
        line = confirm_po(
            plan.id, line.id, "PO-BAD", datetime(2026, 4, 1, tzinfo=timezone.utc), ctx["buyer"], db_session
        )
        assert line.status == BuyPlanLineStatus.pending_verify.value

        # Reject PO
        line = verify_po(plan.id, line.id, "reject", ctx["ops"], db_session, rejection_note="Wrong PO number")
        assert line.status == BuyPlanLineStatus.awaiting_po.value
        assert line.po_number is None

        # Re-confirm with correct PO
        line = confirm_po(
            plan.id, line.id, "PO-GOOD", datetime(2026, 4, 5, tzinfo=timezone.utc), ctx["buyer"], db_session
        )
        assert line.status == BuyPlanLineStatus.pending_verify.value
        assert line.po_number == "PO-GOOD"


# ── Build buy plan from quote (integration) ──────────────────────────


class TestBuildBuyPlanIntegration:
    def test_build_from_won_quote(self, db_session):
        """Building a buy plan from a won quote with valid offers."""
        data = _full_setup(db_session)
        req_transition(data["requisition"], "active", data["sales"], db_session)

        offer = Offer(
            requisition_id=data["requisition"].id,
            requirement_id=data["requirement"].id,
            vendor_card_id=data["vendor"].id,
            vendor_name="Arrow Electronics",
            mpn="LM317T",
            qty_available=1000,
            unit_price=0.50,
            status="active",
            entered_by_id=data["buyer"].id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(offer)
        db_session.flush()

        quote = Quote(
            requisition_id=data["requisition"].id,
            customer_site_id=data["site"].id,
            quote_number="Q-BUILD-E2E",
            status="won",
            created_by_id=data["sales"].id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(quote)
        db_session.flush()

        plan = build_buy_plan(quote.id, db_session)
        assert plan.status == BuyPlanStatus.draft.value
        assert len(plan.lines) >= 1
        assert plan.total_cost is not None
