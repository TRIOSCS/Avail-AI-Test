"""
test_buy_plan_service_v3.py — Buy Plan V3 Service Layer Tests

Phase 3: Offer scoring, auto-split, buyer assignment, AI flags, summary.
Phase 4: Submit, approve, verify SO/PO, flag issues, completion, resubmit.

Called by: pytest
Depends on: conftest.py fixtures, app.services.buy_plan_v3_service
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from app.models import Offer, Quote, Requirement, Requisition, User, VendorCard
from app.models.buy_plan import (
    BuyPlanLine,
    BuyPlanLineStatus,
    BuyPlanStatus,
    BuyPlanV3,
    SOVerificationStatus,
    VerificationGroupMember,
)
from app.services.buy_plan_v3_service import (
    _parse_lead_time_days,
    approve_buy_plan,
    assign_buyer,
    build_buy_plan,
    check_completion,
    confirm_po,
    flag_line_issue,
    generate_ai_flags,
    generate_ai_summary,
    resubmit_buy_plan,
    score_offer,
    submit_buy_plan,
    verify_po,
    verify_so,
)


# ── Helpers ──────────────────────────────────────────────────────────


def _make_vendor_card(db, **overrides) -> VendorCard:
    defaults = {
        "normalized_name": f"vendor-{id(overrides)}",
        "display_name": "Test Vendor",
        "vendor_score": 75.0,
        "is_new_vendor": False,
    }
    defaults.update(overrides)
    card = VendorCard(**defaults)
    db.add(card)
    db.flush()
    return card


def _make_offer(db, req_id, requirement_id, **overrides) -> Offer:
    defaults = {
        "requisition_id": req_id,
        "requirement_id": requirement_id,
        "vendor_name": "Test Vendor",
        "mpn": "LM317T",
        "qty_available": 1000,
        "unit_price": 0.50,
        "status": "active",
        "created_at": datetime.now(timezone.utc),
    }
    defaults.update(overrides)
    offer = Offer(**defaults)
    db.add(offer)
    db.flush()
    return offer


def _make_requirement(db, req_id, **overrides) -> Requirement:
    defaults = {
        "requisition_id": req_id,
        "primary_mpn": "LM317T",
        "target_qty": 1000,
        "target_price": 0.75,
    }
    defaults.update(overrides)
    req = Requirement(**defaults)
    db.add(req)
    db.flush()
    return req


# ── Lead Time Parsing ────────────────────────────────────────────────


class TestLeadTimeParsing:
    def test_stock(self):
        assert _parse_lead_time_days("stock") == 0
        assert _parse_lead_time_days("In Stock") == 0
        assert _parse_lead_time_days("immediate") == 0

    def test_days(self):
        assert _parse_lead_time_days("5 days") == 5
        assert _parse_lead_time_days("3-5 days") == 5  # uses last number

    def test_weeks(self):
        assert _parse_lead_time_days("2 weeks") == 14

    def test_months(self):
        assert _parse_lead_time_days("1 month") == 30

    def test_none(self):
        assert _parse_lead_time_days(None) is None
        assert _parse_lead_time_days("") is None
        assert _parse_lead_time_days("contact us") is None


# ── Offer Scoring ────────────────────────────────────────────────────


class TestOfferScoring:
    def test_perfect_score(self, db_session: Session, test_requisition: Requisition):
        """Offer at target price with great vendor scores high."""
        req = db_session.query(Requirement).filter_by(
            requisition_id=test_requisition.id
        ).first()
        vendor = _make_vendor_card(
            db_session, normalized_name="perfect-vendor",
            display_name="Perfect Vendor", vendor_score=95.0,
            hq_country="US",
        )
        offer = _make_offer(
            db_session, test_requisition.id, req.id,
            unit_price=req.target_price,  # at target
            lead_time="stock",
            vendor_card_id=vendor.id,
            vendor_name="Perfect Vendor",
        )
        db_session.flush()

        score = score_offer(offer, req, vendor, customer_region="americas")
        # Price=100*0.3 + Reliability=95*0.25 + Lead=100*0.2 + Geo=100*0.15 + Terms=50*0.1
        # = 30 + 23.75 + 20 + 15 + 5 = 93.75
        assert score > 85

    def test_expensive_offer_scores_lower(
        self, db_session: Session, test_requisition: Requisition
    ):
        """Offer 2x target price gets low price score."""
        req = db_session.query(Requirement).filter_by(
            requisition_id=test_requisition.id
        ).first()
        vendor = _make_vendor_card(
            db_session, normalized_name="exp-vendor",
            display_name="Expensive Vendor", vendor_score=75.0,
        )
        cheap_offer = _make_offer(
            db_session, test_requisition.id, req.id,
            unit_price=req.target_price, vendor_name="Cheap",
            vendor_card_id=vendor.id,
        )
        expensive_offer = _make_offer(
            db_session, test_requisition.id, req.id,
            unit_price=float(req.target_price) * 2,
            vendor_name="Expensive",
        )
        db_session.flush()

        score_cheap = score_offer(cheap_offer, req, vendor)
        score_expensive = score_offer(expensive_offer, req, None)
        assert score_cheap > score_expensive

    def test_unknown_vendor_lower_reliability(
        self, db_session: Session, test_requisition: Requisition
    ):
        """Unknown vendor (no card) gets lower reliability score."""
        req = db_session.query(Requirement).filter_by(
            requisition_id=test_requisition.id
        ).first()
        offer = _make_offer(
            db_session, test_requisition.id, req.id,
            unit_price=0.50, vendor_name="Unknown Co",
        )
        db_session.flush()

        score_unknown = score_offer(offer, req, None)  # no vendor card
        vendor = _make_vendor_card(
            db_session, normalized_name="known-v", vendor_score=80.0,
        )
        score_known = score_offer(offer, req, vendor)
        assert score_known > score_unknown

    def test_geography_match_bonus(
        self, db_session: Session, test_requisition: Requisition
    ):
        """Same-region vendor gets geography bonus."""
        req = db_session.query(Requirement).filter_by(
            requisition_id=test_requisition.id
        ).first()
        us_vendor = _make_vendor_card(
            db_session, normalized_name="us-vendor",
            display_name="US Vendor", hq_country="US", vendor_score=70.0,
        )
        cn_vendor = _make_vendor_card(
            db_session, normalized_name="cn-vendor",
            display_name="CN Vendor", hq_country="China", vendor_score=70.0,
        )
        offer = _make_offer(
            db_session, test_requisition.id, req.id,
            unit_price=0.50, vendor_name="Geo Test",
        )
        db_session.flush()

        score_us = score_offer(offer, req, us_vendor, customer_region="americas")
        score_cn = score_offer(offer, req, cn_vendor, customer_region="americas")
        assert score_us > score_cn

    def test_no_target_price_gets_50(
        self, db_session: Session, test_requisition: Requisition
    ):
        """If requirement has no target price, price score defaults to 50."""
        req = _make_requirement(
            db_session, test_requisition.id, target_price=None,
            primary_mpn="NOTARGET",
        )
        offer = _make_offer(
            db_session, test_requisition.id, req.id,
            unit_price=1.00, vendor_name="NoTarget Vendor",
        )
        db_session.flush()
        score = score_offer(offer, req, None)
        assert 0 < score < 100


# ── Buyer Assignment ─────────────────────────────────────────────────


class TestBuyerAssignment:
    def test_vendor_ownership_priority(
        self, db_session: Session, test_requisition: Requisition, test_user: User
    ):
        """Buyer who entered the offer gets priority (vendor ownership)."""
        req = db_session.query(Requirement).filter_by(
            requisition_id=test_requisition.id
        ).first()
        offer = _make_offer(
            db_session, test_requisition.id, req.id,
            entered_by_id=test_user.id, vendor_name="Owned Vendor",
        )
        db_session.flush()

        buyer, reason = assign_buyer(offer, None, db_session)
        assert buyer is not None
        assert buyer.id == test_user.id
        assert reason == "vendor_ownership"

    def test_workload_fallback(
        self, db_session: Session, test_requisition: Requisition,
        test_quote: Quote, test_user: User,
    ):
        """When no owner, assigns to buyer with lowest workload."""
        req = db_session.query(Requirement).filter_by(
            requisition_id=test_requisition.id
        ).first()
        # Deactivate conftest buyer so only our test buyers are candidates
        test_user.is_active = False
        # Create two buyers
        buyer1 = User(
            email="buyer1@trioscs.com", name="Buyer One",
            role="buyer", azure_id="b1-az", is_active=True,
        )
        buyer2 = User(
            email="buyer2@trioscs.com", name="Buyer Two",
            role="buyer", azure_id="b2-az", is_active=True,
        )
        db_session.add_all([buyer1, buyer2])
        db_session.flush()

        # Give buyer1 some existing workload
        plan = BuyPlanV3(
            quote_id=test_quote.id,
            requisition_id=test_requisition.id,
        )
        db_session.add(plan)
        db_session.flush()
        for _ in range(3):
            db_session.add(BuyPlanLine(
                buy_plan_id=plan.id, quantity=100, buyer_id=buyer1.id,
                status=BuyPlanLineStatus.awaiting_po.value,
            ))
        db_session.flush()

        # Offer with no entered_by
        offer = _make_offer(
            db_session, test_requisition.id, req.id,
            entered_by_id=None, vendor_name="Unowned",
        )
        db_session.flush()

        buyer, reason = assign_buyer(offer, None, db_session)
        assert buyer is not None
        assert buyer.id == buyer2.id  # buyer2 has 0 workload
        assert reason == "workload"

    def test_no_buyers_available(self, db_session: Session, test_requisition: Requisition):
        """Returns None when no active buyers exist."""
        req = db_session.query(Requirement).filter_by(
            requisition_id=test_requisition.id
        ).first()
        # test_user from conftest is a buyer but let's make offer with no entered_by
        # and deactivate all buyers
        for u in db_session.query(User).filter(User.role.in_(["buyer", "trader"])).all():
            u.is_active = False
        db_session.flush()

        offer = _make_offer(
            db_session, test_requisition.id, req.id,
            entered_by_id=None, vendor_name="Nobody",
        )
        db_session.flush()

        buyer, reason = assign_buyer(offer, None, db_session)
        assert buyer is None
        assert reason == "no_buyers"


# ── Build Buy Plan ───────────────────────────────────────────────────


class TestBuildBuyPlan:
    def test_single_offer_full_qty(
        self, db_session: Session, test_quote: Quote, test_user: User
    ):
        """Single offer covering full requirement qty → one line."""
        req = db_session.query(Requirement).filter_by(
            requisition_id=test_quote.requisition_id
        ).first()
        offer = _make_offer(
            db_session, test_quote.requisition_id, req.id,
            qty_available=1000, unit_price=0.50,
            entered_by_id=test_user.id, vendor_name="Arrow",
        )
        db_session.commit()

        plan = build_buy_plan(test_quote.id, db_session)

        assert plan.quote_id == test_quote.id
        assert plan.status == "draft"
        assert len(plan.lines) == 1
        assert plan.lines[0].quantity == 1000
        assert plan.lines[0].offer_id == offer.id
        assert plan.ai_summary is not None

    def test_auto_split_partial_qty(
        self, db_session: Session, test_quote: Quote, test_user: User
    ):
        """Two offers each covering partial qty → auto-split into 2 lines."""
        req = db_session.query(Requirement).filter_by(
            requisition_id=test_quote.requisition_id
        ).first()
        # Neither offer covers the full 1000
        _make_offer(
            db_session, test_quote.requisition_id, req.id,
            qty_available=600, unit_price=0.50,
            entered_by_id=test_user.id, vendor_name="Arrow",
        )
        _make_offer(
            db_session, test_quote.requisition_id, req.id,
            qty_available=500, unit_price=0.55,
            entered_by_id=test_user.id, vendor_name="Digi-Key",
        )
        db_session.commit()

        plan = build_buy_plan(test_quote.id, db_session)

        assert len(plan.lines) == 2
        total_qty = sum(l.quantity for l in plan.lines)
        assert total_qty == 1000  # fills requirement exactly
        # Both lines share same requirement
        assert plan.lines[0].requirement_id == plan.lines[1].requirement_id

    def test_no_offers_empty_plan(
        self, db_session: Session, test_quote: Quote
    ):
        """No offers → plan with no lines."""
        plan = build_buy_plan(test_quote.id, db_session)
        assert len(plan.lines) == 0
        assert "Empty" in plan.ai_summary

    def test_financials_calculated(
        self, db_session: Session, test_quote: Quote, test_user: User
    ):
        """Cost, revenue, and margin calculated from lines."""
        req = db_session.query(Requirement).filter_by(
            requisition_id=test_quote.requisition_id
        ).first()
        # Set a sell price higher than cost so we can test margin
        req.target_price = 0.75
        _make_offer(
            db_session, test_quote.requisition_id, req.id,
            qty_available=1000, unit_price=0.50,
            entered_by_id=test_user.id, vendor_name="Arrow",
        )
        db_session.commit()

        plan = build_buy_plan(test_quote.id, db_session)

        # cost = 1000 * 0.50 = 500, revenue = 1000 * 0.75 (target) = 750
        assert plan.total_cost is not None
        assert float(plan.total_cost) == 500.0
        assert plan.total_revenue is not None
        assert float(plan.total_revenue) == 750.0
        assert plan.total_margin_pct is not None
        assert float(plan.total_margin_pct) > 0

    def test_invalid_quote_raises(self, db_session: Session):
        """Non-existent quote raises ValueError."""
        with pytest.raises(ValueError, match="not found"):
            build_buy_plan(99999, db_session)


# ── AI Flags ─────────────────────────────────────────────────────────


class TestAIFlags:
    def _make_plan_with_line(
        self, db_session, test_quote, test_user, offer_age_days=0, margin_pct=20.0
    ):
        req = db_session.query(Requirement).filter_by(
            requisition_id=test_quote.requisition_id
        ).first()
        offer = _make_offer(
            db_session, test_quote.requisition_id, req.id,
            qty_available=1000, unit_price=0.50,
            entered_by_id=test_user.id, vendor_name="Flag Test",
            created_at=datetime.now(timezone.utc) - timedelta(days=offer_age_days),
        )
        plan = BuyPlanV3(
            quote_id=test_quote.id,
            requisition_id=test_quote.requisition_id,
        )
        db_session.add(plan)
        db_session.flush()
        line = BuyPlanLine(
            buy_plan_id=plan.id,
            requirement_id=req.id,
            offer_id=offer.id,
            quantity=1000,
            unit_cost=0.50,
            unit_sell=0.75,
            margin_pct=margin_pct,
        )
        db_session.add(line)
        db_session.flush()
        db_session.refresh(plan)
        return plan

    def test_stale_offer_flag(
        self, db_session: Session, test_quote: Quote, test_user: User
    ):
        plan = self._make_plan_with_line(
            db_session, test_quote, test_user, offer_age_days=10
        )
        flags = generate_ai_flags(plan, db_session)
        stale = [f for f in flags if f["type"] == "stale_offer"]
        assert len(stale) == 1
        assert "10 days" in stale[0]["message"]

    def test_fresh_offer_no_flag(
        self, db_session: Session, test_quote: Quote, test_user: User
    ):
        plan = self._make_plan_with_line(
            db_session, test_quote, test_user, offer_age_days=2
        )
        flags = generate_ai_flags(plan, db_session)
        stale = [f for f in flags if f["type"] == "stale_offer"]
        assert len(stale) == 0

    def test_low_margin_flag(
        self, db_session: Session, test_quote: Quote, test_user: User
    ):
        plan = self._make_plan_with_line(
            db_session, test_quote, test_user, margin_pct=5.0
        )
        flags = generate_ai_flags(plan, db_session)
        low = [f for f in flags if f["type"] == "low_margin"]
        assert len(low) == 1

    def test_negative_margin_critical(
        self, db_session: Session, test_quote: Quote, test_user: User
    ):
        plan = self._make_plan_with_line(
            db_session, test_quote, test_user, margin_pct=-5.0
        )
        flags = generate_ai_flags(plan, db_session)
        low = [f for f in flags if f["type"] == "low_margin"]
        assert len(low) == 1
        assert low[0]["severity"] == "critical"

    def test_quantity_gap_flag(
        self, db_session: Session, test_quote: Quote, test_user: User
    ):
        """Partial allocation triggers quantity gap flag."""
        req = db_session.query(Requirement).filter_by(
            requisition_id=test_quote.requisition_id
        ).first()
        offer = _make_offer(
            db_session, test_quote.requisition_id, req.id,
            qty_available=500, unit_price=0.50,
            entered_by_id=test_user.id, vendor_name="Partial",
        )
        plan = BuyPlanV3(
            quote_id=test_quote.id,
            requisition_id=test_quote.requisition_id,
        )
        db_session.add(plan)
        db_session.flush()
        # Only 500 of 1000 allocated
        line = BuyPlanLine(
            buy_plan_id=plan.id, requirement_id=req.id,
            offer_id=offer.id, quantity=500,
        )
        db_session.add(line)
        db_session.flush()
        db_session.refresh(plan)

        flags = generate_ai_flags(plan, db_session)
        gap = [f for f in flags if f["type"] == "quantity_gap"]
        assert len(gap) == 1
        assert "gap: 500" in gap[0]["message"]


# ── AI Summary ───────────────────────────────────────────────────────


class TestAISummary:
    def test_summary_with_lines(
        self, db_session: Session, test_quote: Quote, test_user: User
    ):
        req = db_session.query(Requirement).filter_by(
            requisition_id=test_quote.requisition_id
        ).first()
        offer = _make_offer(
            db_session, test_quote.requisition_id, req.id,
            qty_available=1000, unit_price=0.50,
            entered_by_id=test_user.id, vendor_name="Summary Vendor",
        )
        plan = BuyPlanV3(
            quote_id=test_quote.id,
            requisition_id=test_quote.requisition_id,
        )
        db_session.add(plan)
        db_session.flush()
        line = BuyPlanLine(
            buy_plan_id=plan.id, requirement_id=req.id,
            offer_id=offer.id, quantity=1000, margin_pct=33.33,
        )
        db_session.add(line)
        db_session.flush()
        db_session.refresh(plan)

        summary = generate_ai_summary(plan)
        assert "1 line" in summary
        assert "33.3%" in summary

    def test_empty_plan_summary(self, db_session: Session, test_quote: Quote):
        plan = BuyPlanV3(
            quote_id=test_quote.id,
            requisition_id=test_quote.requisition_id,
        )
        db_session.add(plan)
        db_session.flush()
        db_session.refresh(plan)

        summary = generate_ai_summary(plan)
        assert "Empty" in summary


# ── Phase 4 Helpers ─────────────────────────────────────────────────


def _make_draft_plan(db, test_quote, test_user, *, total_cost=500.0, margin_pct=33.33):
    """Create a draft buy plan with one line, ready for submit."""
    req = db.query(Requirement).filter_by(
        requisition_id=test_quote.requisition_id
    ).first()
    offer = _make_offer(
        db, test_quote.requisition_id, req.id,
        qty_available=1000, unit_price=0.50,
        entered_by_id=test_user.id, vendor_name="Arrow",
    )
    plan = BuyPlanV3(
        quote_id=test_quote.id,
        requisition_id=test_quote.requisition_id,
        status=BuyPlanStatus.draft.value,
        total_cost=total_cost,
        total_revenue=750.0,
        total_margin_pct=margin_pct,
    )
    db.add(plan)
    db.flush()
    line = BuyPlanLine(
        buy_plan_id=plan.id,
        requirement_id=req.id,
        offer_id=offer.id,
        quantity=1000,
        unit_cost=0.50,
        unit_sell=0.75,
        margin_pct=margin_pct,
        buyer_id=test_user.id,
        assignment_reason="vendor_ownership",
        status=BuyPlanLineStatus.awaiting_po.value,
    )
    db.add(line)
    db.flush()
    db.refresh(plan)
    return plan, line, offer, req


def _make_ops_member(db, user):
    """Add user to the ops verification group."""
    member = VerificationGroupMember(user_id=user.id, is_active=True)
    db.add(member)
    db.flush()
    return member


# ── Submit Buy Plan ─────────────────────────────────────────────────


class TestSubmitBuyPlan:
    def test_auto_approve_low_cost(
        self, db_session: Session, test_quote: Quote, test_user: User
    ):
        """Cost below threshold with no critical flags → auto-approve."""
        plan, _, _, _ = _make_draft_plan(db_session, test_quote, test_user, total_cost=500.0)

        result = submit_buy_plan(
            plan.id, "SO-2026-001", test_user, db_session,
        )
        assert result.status == BuyPlanStatus.active.value
        assert result.auto_approved is True
        assert result.sales_order_number == "SO-2026-001"
        assert result.submitted_by_id == test_user.id

    def test_pending_high_cost(
        self, db_session: Session, test_quote: Quote, test_user: User
    ):
        """Cost above threshold → pending for manager approval."""
        plan, _, _, _ = _make_draft_plan(
            db_session, test_quote, test_user, total_cost=10000.0
        )

        result = submit_buy_plan(
            plan.id, "SO-2026-002", test_user, db_session,
        )
        assert result.status == BuyPlanStatus.pending.value
        assert result.auto_approved is False

    def test_pending_critical_flags(
        self, db_session: Session, test_quote: Quote, test_user: User
    ):
        """Critical AI flags → pending even if cost is low."""
        plan, _, _, _ = _make_draft_plan(db_session, test_quote, test_user, total_cost=100.0)
        plan.ai_flags = [{"type": "low_margin", "severity": "critical", "message": "Negative margin"}]
        db_session.flush()

        result = submit_buy_plan(
            plan.id, "SO-2026-003", test_user, db_session,
        )
        assert result.status == BuyPlanStatus.pending.value

    def test_wrong_status_rejected(
        self, db_session: Session, test_quote: Quote, test_user: User
    ):
        """Cannot submit a plan that's not in draft."""
        plan, _, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.active.value
        db_session.flush()

        with pytest.raises(ValueError, match="draft"):
            submit_buy_plan(plan.id, "SO-001", test_user, db_session)

    def test_with_customer_po(
        self, db_session: Session, test_quote: Quote, test_user: User
    ):
        """Customer PO# is stored on submit."""
        plan, _, _, _ = _make_draft_plan(db_session, test_quote, test_user)

        result = submit_buy_plan(
            plan.id, "SO-001", test_user, db_session,
            customer_po_number="CPO-42",
        )
        assert result.customer_po_number == "CPO-42"

    def test_with_line_edits(
        self, db_session: Session, test_quote: Quote, test_user: User
    ):
        """Line edits replace AI-generated lines for the affected requirement."""
        plan, line, offer, req = _make_draft_plan(db_session, test_quote, test_user)
        # Create an alternative offer
        alt_offer = _make_offer(
            db_session, test_quote.requisition_id, req.id,
            qty_available=1000, unit_price=0.45,
            entered_by_id=test_user.id, vendor_name="Digi-Key",
        )
        db_session.flush()

        edits = [{"requirement_id": req.id, "offer_id": alt_offer.id, "quantity": 1000}]
        result = submit_buy_plan(
            plan.id, "SO-001", test_user, db_session,
            line_edits=edits,
        )
        # Old line replaced with new one
        assert len(result.lines) == 1
        assert result.lines[0].offer_id == alt_offer.id

    def test_stock_sale_detected(
        self, db_session: Session, test_quote: Quote, test_user: User
    ):
        """All vendor names in stock_sale_vendor_names → is_stock_sale=True."""
        req = db_session.query(Requirement).filter_by(
            requisition_id=test_quote.requisition_id
        ).first()
        # Use a stock vendor name (from default config: "trio")
        stock_offer = _make_offer(
            db_session, test_quote.requisition_id, req.id,
            qty_available=1000, unit_price=0.50,
            entered_by_id=test_user.id, vendor_name="Trio",
        )
        plan = BuyPlanV3(
            quote_id=test_quote.id,
            requisition_id=test_quote.requisition_id,
            status=BuyPlanStatus.draft.value,
            total_cost=500.0,
        )
        db_session.add(plan)
        db_session.flush()
        db_session.add(BuyPlanLine(
            buy_plan_id=plan.id, requirement_id=req.id,
            offer_id=stock_offer.id, quantity=1000,
            unit_cost=0.50, unit_sell=0.75,
            status=BuyPlanLineStatus.awaiting_po.value,
        ))
        db_session.flush()
        db_session.refresh(plan)

        result = submit_buy_plan(plan.id, "SO-STOCK", test_user, db_session)
        assert result.is_stock_sale is True

    def test_not_found(self, db_session: Session, test_user: User):
        with pytest.raises(ValueError, match="not found"):
            submit_buy_plan(99999, "SO-001", test_user, db_session)


# ── Approve Buy Plan ────────────────────────────────────────────────


class TestApproveBuyPlan:
    def test_approve(
        self, db_session: Session, test_quote: Quote,
        test_user: User, manager_user: User,
    ):
        """Manager approve → active."""
        plan, _, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.pending.value
        db_session.flush()

        result = approve_buy_plan(
            plan.id, "approve", manager_user, db_session,
            notes="Looks good",
        )
        assert result.status == BuyPlanStatus.active.value
        assert result.approved_by_id == manager_user.id
        assert result.approved_at is not None
        assert result.approval_notes == "Looks good"

    def test_reject(
        self, db_session: Session, test_quote: Quote,
        test_user: User, manager_user: User,
    ):
        """Manager reject → back to draft."""
        plan, _, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.pending.value
        db_session.flush()

        result = approve_buy_plan(
            plan.id, "reject", manager_user, db_session,
            notes="Margin too low",
        )
        assert result.status == BuyPlanStatus.draft.value
        assert result.approval_notes == "Margin too low"

    def test_with_line_override(
        self, db_session: Session, test_quote: Quote,
        test_user: User, manager_user: User,
    ):
        """Manager can swap a vendor on a specific line."""
        plan, line, _, req = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.pending.value
        alt_offer = _make_offer(
            db_session, test_quote.requisition_id, req.id,
            qty_available=1000, unit_price=0.40,
            entered_by_id=test_user.id, vendor_name="Better Vendor",
        )
        db_session.flush()

        overrides = [{
            "line_id": line.id, "offer_id": alt_offer.id,
            "manager_note": "Better pricing",
        }]
        result = approve_buy_plan(
            plan.id, "approve", manager_user, db_session,
            line_overrides=overrides,
        )
        assert result.status == BuyPlanStatus.active.value
        updated_line = next(l for l in result.lines if l.id == line.id)
        assert updated_line.offer_id == alt_offer.id
        assert updated_line.manager_note == "Better pricing"

    def test_wrong_status(
        self, db_session: Session, test_quote: Quote,
        test_user: User, manager_user: User,
    ):
        """Cannot approve a plan that's not pending."""
        plan, _, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        # Still in draft — not submitted yet
        with pytest.raises(ValueError, match="pending"):
            approve_buy_plan(plan.id, "approve", manager_user, db_session)

    def test_invalid_action(
        self, db_session: Session, test_quote: Quote,
        test_user: User, manager_user: User,
    ):
        plan, _, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.pending.value
        db_session.flush()

        with pytest.raises(ValueError, match="Invalid action"):
            approve_buy_plan(plan.id, "maybe", manager_user, db_session)


# ── SO Verification ─────────────────────────────────────────────────


class TestVerifySO:
    def test_approve(
        self, db_session: Session, test_quote: Quote,
        test_user: User, admin_user: User,
    ):
        """Ops approves SO → so_status=approved."""
        plan, _, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.active.value
        _make_ops_member(db_session, admin_user)
        db_session.flush()

        result = verify_so(plan.id, "approve", admin_user, db_session)
        assert result.so_status == SOVerificationStatus.approved.value
        assert result.so_verified_by_id == admin_user.id

    def test_reject(
        self, db_session: Session, test_quote: Quote,
        test_user: User, admin_user: User,
    ):
        """Ops rejects SO → so_status=rejected."""
        plan, _, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.active.value
        _make_ops_member(db_session, admin_user)
        db_session.flush()

        result = verify_so(
            plan.id, "reject", admin_user, db_session,
            rejection_note="Wrong SO number",
        )
        assert result.so_status == SOVerificationStatus.rejected.value
        assert result.so_rejection_note == "Wrong SO number"

    def test_halt_stops_plan(
        self, db_session: Session, test_quote: Quote,
        test_user: User, admin_user: User,
    ):
        """Halt → plan.status=halted (everything stops)."""
        plan, _, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.active.value
        _make_ops_member(db_session, admin_user)
        db_session.flush()

        result = verify_so(
            plan.id, "halt", admin_user, db_session,
            rejection_note="Fraud suspected",
        )
        assert result.status == BuyPlanStatus.halted.value
        assert result.halted_by_id == admin_user.id

    def test_non_ops_member_rejected(
        self, db_session: Session, test_quote: Quote,
        test_user: User, manager_user: User,
    ):
        """Non-ops user cannot verify SO."""
        plan, _, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.active.value
        db_session.flush()

        with pytest.raises(PermissionError, match="verification group"):
            verify_so(plan.id, "approve", manager_user, db_session)

    def test_already_verified(
        self, db_session: Session, test_quote: Quote,
        test_user: User, admin_user: User,
    ):
        """Cannot re-verify an already-verified SO."""
        plan, _, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.active.value
        plan.so_status = SOVerificationStatus.approved.value
        _make_ops_member(db_session, admin_user)
        db_session.flush()

        with pytest.raises(ValueError, match="already verified"):
            verify_so(plan.id, "approve", admin_user, db_session)


# ── Confirm PO ──────────────────────────────────────────────────────


class TestConfirmPO:
    def test_valid_confirmation(
        self, db_session: Session, test_quote: Quote, test_user: User
    ):
        """Buyer confirms PO → line goes to pending_verify."""
        plan, line, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.active.value
        db_session.flush()

        ship_date = datetime(2026, 3, 15, tzinfo=timezone.utc)
        result = confirm_po(
            plan.id, line.id, "PO-2026-042", ship_date, test_user, db_session,
        )
        assert result.status == BuyPlanLineStatus.pending_verify.value
        assert result.po_number == "PO-2026-042"
        assert result.estimated_ship_date == ship_date
        assert result.po_confirmed_at is not None

    def test_wrong_plan_status(
        self, db_session: Session, test_quote: Quote, test_user: User
    ):
        """Cannot confirm PO on a non-active plan."""
        plan, line, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        # plan is still draft
        with pytest.raises(ValueError, match="active"):
            confirm_po(
                plan.id, line.id, "PO-001",
                datetime(2026, 3, 15, tzinfo=timezone.utc),
                test_user, db_session,
            )

    def test_wrong_line_status(
        self, db_session: Session, test_quote: Quote, test_user: User
    ):
        """Cannot confirm PO on a line not awaiting PO."""
        plan, line, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.active.value
        line.status = BuyPlanLineStatus.verified.value
        db_session.flush()

        with pytest.raises(ValueError, match="awaiting PO"):
            confirm_po(
                plan.id, line.id, "PO-001",
                datetime(2026, 3, 15, tzinfo=timezone.utc),
                test_user, db_session,
            )


# ── Verify PO ───────────────────────────────────────────────────────


class TestVerifyPO:
    def test_approve(
        self, db_session: Session, test_quote: Quote,
        test_user: User, admin_user: User,
    ):
        """Ops approves PO → line verified."""
        plan, line, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.active.value
        line.status = BuyPlanLineStatus.pending_verify.value
        line.po_number = "PO-001"
        _make_ops_member(db_session, admin_user)
        db_session.flush()

        result = verify_po(plan.id, line.id, "approve", admin_user, db_session)
        assert result.status == BuyPlanLineStatus.verified.value
        assert result.po_verified_by_id == admin_user.id

    def test_reject_resets_po(
        self, db_session: Session, test_quote: Quote,
        test_user: User, admin_user: User,
    ):
        """PO rejection clears PO data and sends back to awaiting_po."""
        plan, line, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.active.value
        line.status = BuyPlanLineStatus.pending_verify.value
        line.po_number = "PO-001"
        line.estimated_ship_date = datetime(2026, 3, 15, tzinfo=timezone.utc)
        line.po_confirmed_at = datetime.now(timezone.utc)
        _make_ops_member(db_session, admin_user)
        db_session.flush()

        result = verify_po(
            plan.id, line.id, "reject", admin_user, db_session,
            rejection_note="Wrong PO amount",
        )
        assert result.status == BuyPlanLineStatus.awaiting_po.value
        assert result.po_number is None
        assert result.estimated_ship_date is None
        assert result.po_rejection_note == "Wrong PO amount"

    def test_non_ops_rejected(
        self, db_session: Session, test_quote: Quote,
        test_user: User, manager_user: User,
    ):
        """Non-ops user cannot verify PO."""
        plan, line, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.active.value
        line.status = BuyPlanLineStatus.pending_verify.value
        db_session.flush()

        with pytest.raises(PermissionError, match="verification group"):
            verify_po(plan.id, line.id, "approve", manager_user, db_session)


# ── Flag Line Issue ─────────────────────────────────────────────────


class TestFlagLineIssue:
    def test_flag_sold_out(
        self, db_session: Session, test_quote: Quote, test_user: User
    ):
        plan, line, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.active.value
        db_session.flush()

        result = flag_line_issue(
            plan.id, line.id, "sold_out", test_user, db_session,
        )
        assert result.status == BuyPlanLineStatus.issue.value
        assert result.issue_type == "sold_out"

    def test_flag_with_note(
        self, db_session: Session, test_quote: Quote, test_user: User
    ):
        plan, line, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.active.value
        db_session.flush()

        result = flag_line_issue(
            plan.id, line.id, "price_changed", test_user, db_session,
            note="Up 20%",
        )
        assert result.issue_note == "Up 20%"

    def test_wrong_plan_status(
        self, db_session: Session, test_quote: Quote, test_user: User
    ):
        plan, line, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        # plan is draft, not active

        with pytest.raises(ValueError, match="active"):
            flag_line_issue(plan.id, line.id, "sold_out", test_user, db_session)

    def test_cannot_flag_verified_line(
        self, db_session: Session, test_quote: Quote, test_user: User
    ):
        plan, line, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.active.value
        line.status = BuyPlanLineStatus.verified.value
        db_session.flush()

        with pytest.raises(ValueError, match="Cannot flag"):
            flag_line_issue(plan.id, line.id, "sold_out", test_user, db_session)


# ── Check Completion ────────────────────────────────────────────────


class TestCheckCompletion:
    def test_all_verified_and_so_approved(
        self, db_session: Session, test_quote: Quote, test_user: User
    ):
        """All lines verified + SO approved → completed."""
        plan, line, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.active.value
        plan.so_status = SOVerificationStatus.approved.value
        line.status = BuyPlanLineStatus.verified.value
        db_session.flush()

        result = check_completion(plan.id, db_session)
        assert result.status == BuyPlanStatus.completed.value
        assert result.completed_at is not None

    def test_partial_lines_not_complete(
        self, db_session: Session, test_quote: Quote, test_user: User
    ):
        """Some lines still pending → not complete."""
        plan, line, _, req = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.active.value
        plan.so_status = SOVerificationStatus.approved.value
        line.status = BuyPlanLineStatus.verified.value
        # Add a second line still in progress
        offer2 = _make_offer(
            db_session, test_quote.requisition_id, req.id,
            qty_available=500, unit_price=0.55,
            entered_by_id=test_user.id, vendor_name="Vendor2",
        )
        db_session.add(BuyPlanLine(
            buy_plan_id=plan.id, requirement_id=req.id,
            offer_id=offer2.id, quantity=500,
            status=BuyPlanLineStatus.awaiting_po.value,
        ))
        db_session.flush()

        result = check_completion(plan.id, db_session)
        assert result.status == BuyPlanStatus.active.value

    def test_so_not_approved_blocks_completion(
        self, db_session: Session, test_quote: Quote, test_user: User
    ):
        """All lines verified but SO still pending → not complete."""
        plan, line, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.active.value
        plan.so_status = SOVerificationStatus.pending.value
        line.status = BuyPlanLineStatus.verified.value
        db_session.flush()

        result = check_completion(plan.id, db_session)
        assert result.status == BuyPlanStatus.active.value

    def test_cancelled_lines_count_as_terminal(
        self, db_session: Session, test_quote: Quote, test_user: User
    ):
        """Mix of verified + cancelled lines → still completes."""
        plan, line, _, req = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.active.value
        plan.so_status = SOVerificationStatus.approved.value
        line.status = BuyPlanLineStatus.verified.value
        offer2 = _make_offer(
            db_session, test_quote.requisition_id, req.id,
            qty_available=500, unit_price=0.55,
            entered_by_id=test_user.id, vendor_name="Vendor2",
        )
        db_session.add(BuyPlanLine(
            buy_plan_id=plan.id, requirement_id=req.id,
            offer_id=offer2.id, quantity=500,
            status=BuyPlanLineStatus.cancelled.value,
        ))
        db_session.flush()

        result = check_completion(plan.id, db_session)
        assert result.status == BuyPlanStatus.completed.value


# ── Resubmit Buy Plan ──────────────────────────────────────────────


class TestResubmitBuyPlan:
    def test_resubmit_auto_approve(
        self, db_session: Session, test_quote: Quote, test_user: User
    ):
        """Resubmit a rejected (draft) plan → auto-approve if under threshold."""
        plan, _, _, _ = _make_draft_plan(db_session, test_quote, test_user, total_cost=500.0)
        # Simulate prior rejection
        plan.approval_notes = "Fix the SO number"
        plan.so_status = SOVerificationStatus.rejected.value
        db_session.flush()

        result = resubmit_buy_plan(
            plan.id, "SO-FIXED", test_user, db_session,
        )
        assert result.status == BuyPlanStatus.active.value
        assert result.auto_approved is True
        assert result.sales_order_number == "SO-FIXED"
        # SO verification reset
        assert result.so_status == SOVerificationStatus.pending.value
        assert result.so_rejection_note is None

    def test_resubmit_needs_approval(
        self, db_session: Session, test_quote: Quote, test_user: User
    ):
        """High-cost resubmit → pending approval again."""
        plan, _, _, _ = _make_draft_plan(
            db_session, test_quote, test_user, total_cost=10000.0,
        )

        result = resubmit_buy_plan(
            plan.id, "SO-FIXED", test_user, db_session,
        )
        assert result.status == BuyPlanStatus.pending.value
        assert result.auto_approved is False

    def test_wrong_status(
        self, db_session: Session, test_quote: Quote, test_user: User
    ):
        """Cannot resubmit an active plan."""
        plan, _, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.active.value
        db_session.flush()

        with pytest.raises(ValueError, match="draft"):
            resubmit_buy_plan(plan.id, "SO-001", test_user, db_session)


# ── Auto-complete via PO Verify ─────────────────────────────────────


class TestAutoCompleteViaPOVerify:
    def test_last_line_verified_triggers_completion(
        self, db_session: Session, test_quote: Quote,
        test_user: User, admin_user: User,
    ):
        """Verifying the last PO triggers auto-completion."""
        plan, line, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.active.value
        plan.so_status = SOVerificationStatus.approved.value
        line.status = BuyPlanLineStatus.pending_verify.value
        line.po_number = "PO-LAST"
        _make_ops_member(db_session, admin_user)
        db_session.flush()

        verify_po(plan.id, line.id, "approve", admin_user, db_session)

        db_session.refresh(plan)
        assert plan.status == BuyPlanStatus.completed.value
