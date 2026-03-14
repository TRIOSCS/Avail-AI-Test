"""
test_buy_plan_models.py — Buy Plan Model Tests

Covers model creation, ENUM values, relationships, split lines
(multiple BuyPlanLine rows sharing the same requirement_id),
and VerificationGroupMember uniqueness.

Called by: pytest
Depends on: conftest.py fixtures, app.models.buy_plan
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session

from app.models import (
    Offer,
    Quote,
    User,
)
from app.models.buy_plan import (
    AIFlagSeverity,
    BuyPlanLine,
    BuyPlanLineStatus,
    BuyPlanStatus,
    BuyPlan,
    LineIssueType,
    SOVerificationStatus,
    VerificationGroupMember,
)

# ── ENUM Tests ───────────────────────────────────────────────────────


class TestEnums:
    def test_buy_plan_status_values(self):
        assert set(BuyPlanStatus) == {
            BuyPlanStatus.draft,
            BuyPlanStatus.pending,
            BuyPlanStatus.active,
            BuyPlanStatus.halted,
            BuyPlanStatus.completed,
            BuyPlanStatus.cancelled,
        }

    def test_so_verification_status_values(self):
        assert set(SOVerificationStatus) == {
            SOVerificationStatus.pending,
            SOVerificationStatus.approved,
            SOVerificationStatus.rejected,
        }

    def test_line_status_values(self):
        assert set(BuyPlanLineStatus) == {
            BuyPlanLineStatus.awaiting_po,
            BuyPlanLineStatus.pending_verify,
            BuyPlanLineStatus.verified,
            BuyPlanLineStatus.issue,
            BuyPlanLineStatus.cancelled,
        }

    def test_issue_type_values(self):
        assert set(LineIssueType) == {
            LineIssueType.sold_out,
            LineIssueType.price_changed,
            LineIssueType.lead_time_changed,
            LineIssueType.other,
        }

    def test_ai_flag_severity_values(self):
        assert set(AIFlagSeverity) == {
            AIFlagSeverity.info,
            AIFlagSeverity.warning,
            AIFlagSeverity.critical,
        }

    def test_enums_are_str_subclass(self):
        """Ensures enums serialize as strings in JSON/DB."""
        assert isinstance(BuyPlanStatus.draft, str)
        assert BuyPlanStatus.draft == "draft"


# ── BuyPlan Model Tests ───────────────────────────────────────────


class TestBuyPlanModel:
    def test_create_minimal(self, db_session: Session, test_quote: Quote):
        plan = BuyPlan(
            quote_id=test_quote.id,
            requisition_id=test_quote.requisition_id,
        )
        db_session.add(plan)
        db_session.commit()
        db_session.refresh(plan)

        assert plan.id is not None
        assert plan.status == "draft"
        assert plan.so_status == "pending"
        assert plan.created_at is not None
        assert plan.is_stock_sale is False
        assert plan.auto_approved is False

    def test_create_with_all_fields(self, db_session: Session, test_quote: Quote, test_user: User):
        plan = BuyPlan(
            quote_id=test_quote.id,
            requisition_id=test_quote.requisition_id,
            sales_order_number="SO-2026-001",
            customer_po_number="CPO-42",
            status=BuyPlanStatus.pending.value,
            so_status=SOVerificationStatus.pending.value,
            total_cost=500.00,
            total_revenue=1000.00,
            total_margin_pct=50.00,
            ai_summary="2 lines, 2 vendors, avg margin 50%",
            ai_flags=[{"type": "stale_offer", "severity": "warning", "message": "Offer >5 days old"}],
            submitted_by_id=test_user.id,
            submitted_at=datetime.now(timezone.utc),
            salesperson_notes="Rush order",
        )
        db_session.add(plan)
        db_session.commit()
        db_session.refresh(plan)

        assert plan.sales_order_number == "SO-2026-001"
        assert plan.customer_po_number == "CPO-42"
        assert float(plan.total_margin_pct) == 50.00
        assert len(plan.ai_flags) == 1
        assert plan.ai_flags[0]["type"] == "stale_offer"

    def test_quote_relationship(self, db_session: Session, test_quote: Quote):
        plan = BuyPlan(
            quote_id=test_quote.id,
            requisition_id=test_quote.requisition_id,
        )
        db_session.add(plan)
        db_session.commit()
        db_session.refresh(plan)

        assert plan.quote is not None
        assert plan.quote.id == test_quote.id
        assert plan.requisition is not None

    def test_submitted_by_relationship(self, db_session: Session, test_quote: Quote, test_user: User):
        plan = BuyPlan(
            quote_id=test_quote.id,
            requisition_id=test_quote.requisition_id,
            submitted_by_id=test_user.id,
        )
        db_session.add(plan)
        db_session.commit()
        db_session.refresh(plan)

        assert plan.submitted_by is not None
        assert plan.submitted_by.email == test_user.email


# ── BuyPlanLine Model Tests ─────────────────────────────────────────


class TestBuyPlanLineModel:
    def _make_plan(self, db_session, test_quote):
        plan = BuyPlan(
            quote_id=test_quote.id,
            requisition_id=test_quote.requisition_id,
        )
        db_session.add(plan)
        db_session.commit()
        db_session.refresh(plan)
        return plan

    def test_create_line(self, db_session: Session, test_quote: Quote, test_offer: Offer):
        plan = self._make_plan(db_session, test_quote)
        line = BuyPlanLine(
            buy_plan_id=plan.id,
            requirement_id=test_offer.requirement_id,
            offer_id=test_offer.id,
            quantity=500,
            unit_cost=0.50,
            unit_sell=0.75,
            margin_pct=33.33,
            ai_score=85.5,
        )
        db_session.add(line)
        db_session.commit()
        db_session.refresh(line)

        assert line.id is not None
        assert line.status == "awaiting_po"
        assert line.quantity == 500
        assert float(line.unit_cost) == 0.50

    def test_line_relationships(self, db_session: Session, test_quote: Quote, test_offer: Offer, test_user: User):
        plan = self._make_plan(db_session, test_quote)
        line = BuyPlanLine(
            buy_plan_id=plan.id,
            offer_id=test_offer.id,
            quantity=1000,
            buyer_id=test_user.id,
            assignment_reason="vendor_ownership",
        )
        db_session.add(line)
        db_session.commit()
        db_session.refresh(line)

        assert line.buy_plan.id == plan.id
        assert line.offer.id == test_offer.id
        assert line.buyer.id == test_user.id
        assert line.assignment_reason == "vendor_ownership"

    def test_plan_lines_relationship(self, db_session: Session, test_quote: Quote, test_offer: Offer):
        plan = self._make_plan(db_session, test_quote)
        line1 = BuyPlanLine(buy_plan_id=plan.id, offer_id=test_offer.id, quantity=500)
        line2 = BuyPlanLine(buy_plan_id=plan.id, offer_id=test_offer.id, quantity=500)
        db_session.add_all([line1, line2])
        db_session.commit()
        db_session.refresh(plan)

        assert len(plan.lines) == 2

    def test_split_lines_same_requirement(self, db_session: Session, test_quote: Quote, test_offer: Offer):
        """Multiple lines can share the same requirement_id (split across vendors)."""
        plan = self._make_plan(db_session, test_quote)
        req_id = test_offer.requirement_id

        # Create a second offer for the split
        offer2 = Offer(
            requisition_id=test_quote.requisition_id,
            vendor_name="Digi-Key",
            mpn="LM317T",
            qty_available=500,
            unit_price=0.55,
            status="active",
        )
        db_session.add(offer2)
        db_session.commit()
        db_session.refresh(offer2)

        line1 = BuyPlanLine(
            buy_plan_id=plan.id,
            requirement_id=req_id,
            offer_id=test_offer.id,
            quantity=600,
        )
        line2 = BuyPlanLine(
            buy_plan_id=plan.id,
            requirement_id=req_id,
            offer_id=offer2.id,
            quantity=400,
        )
        db_session.add_all([line1, line2])
        db_session.commit()

        # Both lines share the same requirement
        assert line1.requirement_id == line2.requirement_id
        # Total covers the requirement qty (1000)
        assert line1.quantity + line2.quantity == 1000

    def test_po_confirmation_fields(self, db_session: Session, test_quote: Quote, test_offer: Offer):
        plan = self._make_plan(db_session, test_quote)
        line = BuyPlanLine(
            buy_plan_id=plan.id,
            offer_id=test_offer.id,
            quantity=1000,
            status=BuyPlanLineStatus.pending_verify.value,
            po_number="PO-2026-0042",
            estimated_ship_date=datetime(2026, 3, 15, tzinfo=timezone.utc),
            po_confirmed_at=datetime.now(timezone.utc),
        )
        db_session.add(line)
        db_session.commit()
        db_session.refresh(line)

        assert line.po_number == "PO-2026-0042"
        assert line.estimated_ship_date.year == 2026
        assert line.status == "pending_verify"

    def test_issue_fields(self, db_session: Session, test_quote: Quote, test_offer: Offer):
        plan = self._make_plan(db_session, test_quote)
        line = BuyPlanLine(
            buy_plan_id=plan.id,
            offer_id=test_offer.id,
            quantity=1000,
            status=BuyPlanLineStatus.issue.value,
            issue_type=LineIssueType.sold_out.value,
            issue_note="Vendor says part is discontinued",
        )
        db_session.add(line)
        db_session.commit()
        db_session.refresh(line)

        assert line.status == "issue"
        assert line.issue_type == "sold_out"

    def test_cascade_delete(self, db_session: Session, test_quote: Quote, test_offer: Offer):
        """Deleting a buy plan cascades to its lines."""
        plan = self._make_plan(db_session, test_quote)
        line = BuyPlanLine(buy_plan_id=plan.id, offer_id=test_offer.id, quantity=100)
        db_session.add(line)
        db_session.commit()
        line_id = line.id

        db_session.delete(plan)
        db_session.commit()

        assert db_session.get(BuyPlanLine, line_id) is None


# ── VerificationGroupMember Tests ────────────────────────────────────


class TestVerificationGroupMember:
    def test_create_member(self, db_session: Session, test_user: User):
        member = VerificationGroupMember(user_id=test_user.id)
        db_session.add(member)
        db_session.commit()
        db_session.refresh(member)

        assert member.id is not None
        assert member.is_active is True
        assert member.added_at is not None
        assert member.user.id == test_user.id

    def test_unique_user(self, db_session: Session, test_user: User):
        """Each user can only be in the verification group once."""
        m1 = VerificationGroupMember(user_id=test_user.id)
        db_session.add(m1)
        db_session.commit()

        m2 = VerificationGroupMember(user_id=test_user.id)
        db_session.add(m2)
        with pytest.raises(Exception):  # IntegrityError
            db_session.commit()

    def test_deactivate_member(self, db_session: Session, test_user: User):
        member = VerificationGroupMember(user_id=test_user.id, is_active=True)
        db_session.add(member)
        db_session.commit()

        member.is_active = False
        db_session.commit()
        db_session.refresh(member)

        assert member.is_active is False
