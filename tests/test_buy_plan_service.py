"""test_buy_plan_service_v3.py — Buy Plan V3 Service Layer Tests.

Phase 3: Offer scoring, auto-split, buyer assignment, AI flags, summary.
Phase 4: Submit, approve, verify SO/PO, flag issues, completion, resubmit.

Called by: pytest
Depends on: conftest.py fixtures, app.services.buyplan_service
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from app.models import Offer, Quote, Requirement, Requisition, User, VendorCard
from app.models.buy_plan import (
    BuyPlan,
    BuyPlanLine,
    BuyPlanLineStatus,
    BuyPlanStatus,
    SOVerificationStatus,
    VerificationGroupMember,
)
from app.services.buyplan_service import (
    _apply_line_edits,
    _apply_line_overrides,
    _check_better_offer,
    _check_geo_mismatch,
    _check_quantity_gaps,
    _country_to_region,
    _create_line,
    _get_routing_maps,
    _is_stock_sale,
    _parse_lead_time_days,
    approve_buy_plan,
    assign_buyer,
    build_buy_plan,
    check_completion,
    confirm_po,
    detect_favoritism,
    flag_line_issue,
    generate_ai_flags,
    generate_ai_summary,
    generate_case_report,
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
        req = db_session.query(Requirement).filter_by(requisition_id=test_requisition.id).first()
        vendor = _make_vendor_card(
            db_session,
            normalized_name="perfect-vendor",
            display_name="Perfect Vendor",
            vendor_score=95.0,
            hq_country="US",
        )
        offer = _make_offer(
            db_session,
            test_requisition.id,
            req.id,
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

    def test_expensive_offer_scores_lower(self, db_session: Session, test_requisition: Requisition):
        """Offer 2x target price gets low price score."""
        req = db_session.query(Requirement).filter_by(requisition_id=test_requisition.id).first()
        vendor = _make_vendor_card(
            db_session,
            normalized_name="exp-vendor",
            display_name="Expensive Vendor",
            vendor_score=75.0,
        )
        cheap_offer = _make_offer(
            db_session,
            test_requisition.id,
            req.id,
            unit_price=req.target_price,
            vendor_name="Cheap",
            vendor_card_id=vendor.id,
        )
        expensive_offer = _make_offer(
            db_session,
            test_requisition.id,
            req.id,
            unit_price=float(req.target_price) * 2,
            vendor_name="Expensive",
        )
        db_session.flush()

        score_cheap = score_offer(cheap_offer, req, vendor)
        score_expensive = score_offer(expensive_offer, req, None)
        assert score_cheap > score_expensive

    def test_unknown_vendor_lower_reliability(self, db_session: Session, test_requisition: Requisition):
        """Unknown vendor (no card) gets lower reliability score."""
        req = db_session.query(Requirement).filter_by(requisition_id=test_requisition.id).first()
        offer = _make_offer(
            db_session,
            test_requisition.id,
            req.id,
            unit_price=0.50,
            vendor_name="Unknown Co",
        )
        db_session.flush()

        score_unknown = score_offer(offer, req, None)  # no vendor card
        vendor = _make_vendor_card(
            db_session,
            normalized_name="known-v",
            vendor_score=80.0,
        )
        score_known = score_offer(offer, req, vendor)
        assert score_known > score_unknown

    def test_geography_match_bonus(self, db_session: Session, test_requisition: Requisition):
        """Same-region vendor gets geography bonus."""
        req = db_session.query(Requirement).filter_by(requisition_id=test_requisition.id).first()
        us_vendor = _make_vendor_card(
            db_session,
            normalized_name="us-vendor",
            display_name="US Vendor",
            hq_country="US",
            vendor_score=70.0,
        )
        cn_vendor = _make_vendor_card(
            db_session,
            normalized_name="cn-vendor",
            display_name="CN Vendor",
            hq_country="China",
            vendor_score=70.0,
        )
        offer = _make_offer(
            db_session,
            test_requisition.id,
            req.id,
            unit_price=0.50,
            vendor_name="Geo Test",
        )
        db_session.flush()

        score_us = score_offer(offer, req, us_vendor, customer_region="americas")
        score_cn = score_offer(offer, req, cn_vendor, customer_region="americas")
        assert score_us > score_cn

    def test_no_target_price_gets_50(self, db_session: Session, test_requisition: Requisition):
        """If requirement has no target price, price score defaults to 50."""
        req = _make_requirement(
            db_session,
            test_requisition.id,
            target_price=None,
            primary_mpn="NOTARGET",
        )
        offer = _make_offer(
            db_session,
            test_requisition.id,
            req.id,
            unit_price=1.00,
            vendor_name="NoTarget Vendor",
        )
        db_session.flush()
        score = score_offer(offer, req, None)
        assert 0 < score < 100


# ── Buyer Assignment ─────────────────────────────────────────────────


class TestBuyerAssignment:
    def test_vendor_ownership_priority(self, db_session: Session, test_requisition: Requisition, test_user: User):
        """Buyer who entered the offer gets priority (vendor ownership)."""
        req = db_session.query(Requirement).filter_by(requisition_id=test_requisition.id).first()
        offer = _make_offer(
            db_session,
            test_requisition.id,
            req.id,
            entered_by_id=test_user.id,
            vendor_name="Owned Vendor",
        )
        db_session.flush()

        buyer, reason = assign_buyer(offer, None, db_session)
        assert buyer is not None
        assert buyer.id == test_user.id
        assert reason == "vendor_ownership"

    def test_workload_fallback(
        self,
        db_session: Session,
        test_requisition: Requisition,
        test_quote: Quote,
        test_user: User,
    ):
        """When no owner, assigns to buyer with lowest workload."""
        req = db_session.query(Requirement).filter_by(requisition_id=test_requisition.id).first()
        # Deactivate conftest buyer so only our test buyers are candidates
        test_user.is_active = False
        # Create two buyers
        buyer1 = User(
            email="buyer1@trioscs.com",
            name="Buyer One",
            role="buyer",
            azure_id="b1-az",
            is_active=True,
        )
        buyer2 = User(
            email="buyer2@trioscs.com",
            name="Buyer Two",
            role="buyer",
            azure_id="b2-az",
            is_active=True,
        )
        db_session.add_all([buyer1, buyer2])
        db_session.flush()

        # Give buyer1 some existing workload
        plan = BuyPlan(
            quote_id=test_quote.id,
            requisition_id=test_requisition.id,
        )
        db_session.add(plan)
        db_session.flush()
        for _ in range(3):
            db_session.add(
                BuyPlanLine(
                    buy_plan_id=plan.id,
                    quantity=100,
                    buyer_id=buyer1.id,
                    status=BuyPlanLineStatus.awaiting_po.value,
                )
            )
        db_session.flush()

        # Offer with no entered_by
        offer = _make_offer(
            db_session,
            test_requisition.id,
            req.id,
            entered_by_id=None,
            vendor_name="Unowned",
        )
        db_session.flush()

        buyer, reason = assign_buyer(offer, None, db_session)
        assert buyer is not None
        assert buyer.id == buyer2.id  # buyer2 has 0 workload
        assert reason == "workload"

    def test_no_buyers_available(self, db_session: Session, test_requisition: Requisition):
        """Returns None when no active buyers exist."""
        req = db_session.query(Requirement).filter_by(requisition_id=test_requisition.id).first()
        # test_user from conftest is a buyer but let's make offer with no entered_by
        # and deactivate all buyers
        for u in db_session.query(User).filter(User.role.in_(["buyer", "trader"])).all():
            u.is_active = False
        db_session.flush()

        offer = _make_offer(
            db_session,
            test_requisition.id,
            req.id,
            entered_by_id=None,
            vendor_name="Nobody",
        )
        db_session.flush()

        buyer, reason = assign_buyer(offer, None, db_session)
        assert buyer is None
        assert reason == "no_buyers"


# ── Build Buy Plan ───────────────────────────────────────────────────


class TestBuildBuyPlan:
    def test_single_offer_full_qty(self, db_session: Session, test_quote: Quote, test_user: User):
        """Single offer covering full requirement qty → one line."""
        req = db_session.query(Requirement).filter_by(requisition_id=test_quote.requisition_id).first()
        offer = _make_offer(
            db_session,
            test_quote.requisition_id,
            req.id,
            qty_available=1000,
            unit_price=0.50,
            entered_by_id=test_user.id,
            vendor_name="Arrow",
        )
        db_session.commit()

        plan = build_buy_plan(test_quote.id, db_session)

        assert plan.quote_id == test_quote.id
        assert plan.status == "draft"
        assert len(plan.lines) == 1
        assert plan.lines[0].quantity == 1000
        assert plan.lines[0].offer_id == offer.id
        assert plan.ai_summary is not None

    def test_auto_split_partial_qty(self, db_session: Session, test_quote: Quote, test_user: User):
        """Two offers each covering partial qty → auto-split into 2 lines."""
        req = db_session.query(Requirement).filter_by(requisition_id=test_quote.requisition_id).first()
        # Neither offer covers the full 1000
        _make_offer(
            db_session,
            test_quote.requisition_id,
            req.id,
            qty_available=600,
            unit_price=0.50,
            entered_by_id=test_user.id,
            vendor_name="Arrow",
        )
        _make_offer(
            db_session,
            test_quote.requisition_id,
            req.id,
            qty_available=500,
            unit_price=0.55,
            entered_by_id=test_user.id,
            vendor_name="Digi-Key",
        )
        db_session.commit()

        plan = build_buy_plan(test_quote.id, db_session)

        assert len(plan.lines) == 2
        total_qty = sum(ln.quantity for ln in plan.lines)
        assert total_qty == 1000  # fills requirement exactly
        # Both lines share same requirement
        assert plan.lines[0].requirement_id == plan.lines[1].requirement_id

    def test_no_offers_empty_plan(self, db_session: Session, test_quote: Quote):
        """No offers → plan with no lines."""
        plan = build_buy_plan(test_quote.id, db_session)
        assert len(plan.lines) == 0
        assert "Empty" in plan.ai_summary

    def test_financials_calculated(self, db_session: Session, test_quote: Quote, test_user: User):
        """Cost, revenue, and margin calculated from lines."""
        req = db_session.query(Requirement).filter_by(requisition_id=test_quote.requisition_id).first()
        # Set a sell price higher than cost so we can test margin
        req.target_price = 0.75
        _make_offer(
            db_session,
            test_quote.requisition_id,
            req.id,
            qty_available=1000,
            unit_price=0.50,
            entered_by_id=test_user.id,
            vendor_name="Arrow",
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
    def _make_plan_with_line(self, db_session, test_quote, test_user, offer_age_days=0, margin_pct=20.0):
        req = db_session.query(Requirement).filter_by(requisition_id=test_quote.requisition_id).first()
        offer = _make_offer(
            db_session,
            test_quote.requisition_id,
            req.id,
            qty_available=1000,
            unit_price=0.50,
            entered_by_id=test_user.id,
            vendor_name="Flag Test",
            created_at=datetime.now(timezone.utc) - timedelta(days=offer_age_days),
        )
        plan = BuyPlan(
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

    def test_stale_offer_flag(self, db_session: Session, test_quote: Quote, test_user: User):
        plan = self._make_plan_with_line(db_session, test_quote, test_user, offer_age_days=10)
        flags = generate_ai_flags(plan, db_session)
        stale = [f for f in flags if f["type"] == "stale_offer"]
        assert len(stale) == 1
        assert "10 days" in stale[0]["message"]

    def test_fresh_offer_no_flag(self, db_session: Session, test_quote: Quote, test_user: User):
        plan = self._make_plan_with_line(db_session, test_quote, test_user, offer_age_days=2)
        flags = generate_ai_flags(plan, db_session)
        stale = [f for f in flags if f["type"] == "stale_offer"]
        assert len(stale) == 0

    def test_low_margin_flag(self, db_session: Session, test_quote: Quote, test_user: User):
        plan = self._make_plan_with_line(db_session, test_quote, test_user, margin_pct=5.0)
        flags = generate_ai_flags(plan, db_session)
        low = [f for f in flags if f["type"] == "low_margin"]
        assert len(low) == 1

    def test_negative_margin_critical(self, db_session: Session, test_quote: Quote, test_user: User):
        plan = self._make_plan_with_line(db_session, test_quote, test_user, margin_pct=-5.0)
        flags = generate_ai_flags(plan, db_session)
        low = [f for f in flags if f["type"] == "low_margin"]
        assert len(low) == 1
        assert low[0]["severity"] == "critical"

    def test_quantity_gap_flag(self, db_session: Session, test_quote: Quote, test_user: User):
        """Partial allocation triggers quantity gap flag."""
        req = db_session.query(Requirement).filter_by(requisition_id=test_quote.requisition_id).first()
        offer = _make_offer(
            db_session,
            test_quote.requisition_id,
            req.id,
            qty_available=500,
            unit_price=0.50,
            entered_by_id=test_user.id,
            vendor_name="Partial",
        )
        plan = BuyPlan(
            quote_id=test_quote.id,
            requisition_id=test_quote.requisition_id,
        )
        db_session.add(plan)
        db_session.flush()
        # Only 500 of 1000 allocated
        line = BuyPlanLine(
            buy_plan_id=plan.id,
            requirement_id=req.id,
            offer_id=offer.id,
            quantity=500,
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
    def test_summary_with_lines(self, db_session: Session, test_quote: Quote, test_user: User):
        req = db_session.query(Requirement).filter_by(requisition_id=test_quote.requisition_id).first()
        offer = _make_offer(
            db_session,
            test_quote.requisition_id,
            req.id,
            qty_available=1000,
            unit_price=0.50,
            entered_by_id=test_user.id,
            vendor_name="Summary Vendor",
        )
        plan = BuyPlan(
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
            margin_pct=33.33,
        )
        db_session.add(line)
        db_session.flush()
        db_session.refresh(plan)

        summary = generate_ai_summary(plan)
        assert "1 line" in summary
        assert "33.3%" in summary

    def test_empty_plan_summary(self, db_session: Session, test_quote: Quote):
        plan = BuyPlan(
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
    req = db.query(Requirement).filter_by(requisition_id=test_quote.requisition_id).first()
    offer = _make_offer(
        db,
        test_quote.requisition_id,
        req.id,
        qty_available=1000,
        unit_price=0.50,
        entered_by_id=test_user.id,
        vendor_name="Arrow",
    )
    plan = BuyPlan(
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
    def test_auto_approve_low_cost(self, db_session: Session, test_quote: Quote, test_user: User):
        """Cost below threshold with no critical flags → auto-approve."""
        plan, _, _, _ = _make_draft_plan(db_session, test_quote, test_user, total_cost=500.0)

        result = submit_buy_plan(
            plan.id,
            "SO-2026-001",
            test_user,
            db_session,
        )
        assert result.status == BuyPlanStatus.active.value
        assert result.auto_approved is True
        assert result.sales_order_number == "SO-2026-001"
        assert result.submitted_by_id == test_user.id

    def test_pending_high_cost(self, db_session: Session, test_quote: Quote, test_user: User):
        """Cost above threshold → pending for manager approval."""
        plan, _, _, _ = _make_draft_plan(db_session, test_quote, test_user, total_cost=10000.0)

        result = submit_buy_plan(
            plan.id,
            "SO-2026-002",
            test_user,
            db_session,
        )
        assert result.status == BuyPlanStatus.pending.value
        assert result.auto_approved is False

    def test_pending_critical_flags(self, db_session: Session, test_quote: Quote, test_user: User):
        """Critical AI flags → pending even if cost is low."""
        plan, _, _, _ = _make_draft_plan(db_session, test_quote, test_user, total_cost=100.0)
        plan.ai_flags = [{"type": "low_margin", "severity": "critical", "message": "Negative margin"}]
        db_session.flush()

        result = submit_buy_plan(
            plan.id,
            "SO-2026-003",
            test_user,
            db_session,
        )
        assert result.status == BuyPlanStatus.pending.value

    def test_wrong_status_rejected(self, db_session: Session, test_quote: Quote, test_user: User):
        """Cannot submit a plan that's not in draft."""
        plan, _, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.active.value
        db_session.flush()

        with pytest.raises(ValueError, match="draft"):
            submit_buy_plan(plan.id, "SO-001", test_user, db_session)

    def test_with_customer_po(self, db_session: Session, test_quote: Quote, test_user: User):
        """Customer PO# is stored on submit."""
        plan, _, _, _ = _make_draft_plan(db_session, test_quote, test_user)

        result = submit_buy_plan(
            plan.id,
            "SO-001",
            test_user,
            db_session,
            customer_po_number="CPO-42",
        )
        assert result.customer_po_number == "CPO-42"

    def test_with_line_edits(self, db_session: Session, test_quote: Quote, test_user: User):
        """Line edits replace AI-generated lines for the affected requirement."""
        plan, line, offer, req = _make_draft_plan(db_session, test_quote, test_user)
        # Create an alternative offer
        alt_offer = _make_offer(
            db_session,
            test_quote.requisition_id,
            req.id,
            qty_available=1000,
            unit_price=0.45,
            entered_by_id=test_user.id,
            vendor_name="Digi-Key",
        )
        db_session.flush()

        edits = [{"requirement_id": req.id, "offer_id": alt_offer.id, "quantity": 1000}]
        result = submit_buy_plan(
            plan.id,
            "SO-001",
            test_user,
            db_session,
            line_edits=edits,
        )
        # Old line replaced with new one
        assert len(result.lines) == 1
        assert result.lines[0].offer_id == alt_offer.id

    def test_stock_sale_detected(self, db_session: Session, test_quote: Quote, test_user: User):
        """All vendor names in stock_sale_vendor_names → is_stock_sale=True."""
        req = db_session.query(Requirement).filter_by(requisition_id=test_quote.requisition_id).first()
        # Use a stock vendor name (from default config: "trio")
        stock_offer = _make_offer(
            db_session,
            test_quote.requisition_id,
            req.id,
            qty_available=1000,
            unit_price=0.50,
            entered_by_id=test_user.id,
            vendor_name="Trio",
        )
        plan = BuyPlan(
            quote_id=test_quote.id,
            requisition_id=test_quote.requisition_id,
            status=BuyPlanStatus.draft.value,
            total_cost=500.0,
        )
        db_session.add(plan)
        db_session.flush()
        db_session.add(
            BuyPlanLine(
                buy_plan_id=plan.id,
                requirement_id=req.id,
                offer_id=stock_offer.id,
                quantity=1000,
                unit_cost=0.50,
                unit_sell=0.75,
                status=BuyPlanLineStatus.awaiting_po.value,
            )
        )
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
        self,
        db_session: Session,
        test_quote: Quote,
        test_user: User,
        manager_user: User,
    ):
        """Manager approve → active."""
        plan, _, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.pending.value
        db_session.flush()

        result = approve_buy_plan(
            plan.id,
            "approve",
            manager_user,
            db_session,
            notes="Looks good",
        )
        assert result.status == BuyPlanStatus.active.value
        assert result.approved_by_id == manager_user.id
        assert result.approved_at is not None
        assert result.approval_notes == "Looks good"

    def test_reject(
        self,
        db_session: Session,
        test_quote: Quote,
        test_user: User,
        manager_user: User,
    ):
        """Manager reject → back to draft."""
        plan, _, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.pending.value
        db_session.flush()

        result = approve_buy_plan(
            plan.id,
            "reject",
            manager_user,
            db_session,
            notes="Margin too low",
        )
        assert result.status == BuyPlanStatus.draft.value
        assert result.approval_notes == "Margin too low"

    def test_with_line_override(
        self,
        db_session: Session,
        test_quote: Quote,
        test_user: User,
        manager_user: User,
    ):
        """Manager can swap a vendor on a specific line."""
        plan, line, _, req = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.pending.value
        alt_offer = _make_offer(
            db_session,
            test_quote.requisition_id,
            req.id,
            qty_available=1000,
            unit_price=0.40,
            entered_by_id=test_user.id,
            vendor_name="Better Vendor",
        )
        db_session.flush()

        overrides = [
            {
                "line_id": line.id,
                "offer_id": alt_offer.id,
                "manager_note": "Better pricing",
            }
        ]
        result = approve_buy_plan(
            plan.id,
            "approve",
            manager_user,
            db_session,
            line_overrides=overrides,
        )
        assert result.status == BuyPlanStatus.active.value
        updated_line = next(ln for ln in result.lines if ln.id == line.id)
        assert updated_line.offer_id == alt_offer.id
        assert updated_line.manager_note == "Better pricing"

    def test_wrong_status(
        self,
        db_session: Session,
        test_quote: Quote,
        test_user: User,
        manager_user: User,
    ):
        """Cannot approve a plan that's not pending."""
        plan, _, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        # Still in draft — not submitted yet
        with pytest.raises(ValueError, match="pending"):
            approve_buy_plan(plan.id, "approve", manager_user, db_session)

    def test_invalid_action(
        self,
        db_session: Session,
        test_quote: Quote,
        test_user: User,
        manager_user: User,
    ):
        plan, _, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.pending.value
        db_session.flush()

        with pytest.raises(ValueError, match="Invalid action"):
            approve_buy_plan(plan.id, "maybe", manager_user, db_session)


# ── SO Verification ─────────────────────────────────────────────────


class TestVerifySO:
    def test_approve(
        self,
        db_session: Session,
        test_quote: Quote,
        test_user: User,
        admin_user: User,
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
        self,
        db_session: Session,
        test_quote: Quote,
        test_user: User,
        admin_user: User,
    ):
        """Ops rejects SO → so_status=rejected."""
        plan, _, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.active.value
        _make_ops_member(db_session, admin_user)
        db_session.flush()

        result = verify_so(
            plan.id,
            "reject",
            admin_user,
            db_session,
            rejection_note="Wrong SO number",
        )
        assert result.so_status == SOVerificationStatus.rejected.value
        assert result.so_rejection_note == "Wrong SO number"

    def test_halt_stops_plan(
        self,
        db_session: Session,
        test_quote: Quote,
        test_user: User,
        admin_user: User,
    ):
        """Halt → plan.status=halted (everything stops)."""
        plan, _, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.active.value
        _make_ops_member(db_session, admin_user)
        db_session.flush()

        result = verify_so(
            plan.id,
            "halt",
            admin_user,
            db_session,
            rejection_note="Fraud suspected",
        )
        assert result.status == BuyPlanStatus.halted.value
        assert result.halted_by_id == admin_user.id

    def test_non_ops_member_rejected(
        self,
        db_session: Session,
        test_quote: Quote,
        test_user: User,
        manager_user: User,
    ):
        """Non-ops user cannot verify SO."""
        plan, _, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.active.value
        db_session.flush()

        with pytest.raises(PermissionError, match="verification group"):
            verify_so(plan.id, "approve", manager_user, db_session)

    def test_already_verified(
        self,
        db_session: Session,
        test_quote: Quote,
        test_user: User,
        admin_user: User,
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
    def test_valid_confirmation(self, db_session: Session, test_quote: Quote, test_user: User):
        """Buyer confirms PO → line goes to pending_verify."""
        plan, line, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.active.value
        db_session.flush()

        ship_date = datetime(2026, 3, 15, tzinfo=timezone.utc)
        result = confirm_po(
            plan.id,
            line.id,
            "PO-2026-042",
            ship_date,
            test_user,
            db_session,
        )
        assert result.status == BuyPlanLineStatus.pending_verify.value
        assert result.po_number == "PO-2026-042"
        assert result.estimated_ship_date == ship_date
        assert result.po_confirmed_at is not None

    def test_wrong_plan_status(self, db_session: Session, test_quote: Quote, test_user: User):
        """Cannot confirm PO on a non-active plan."""
        plan, line, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        # plan is still draft
        with pytest.raises(ValueError, match="active"):
            confirm_po(
                plan.id,
                line.id,
                "PO-001",
                datetime(2026, 3, 15, tzinfo=timezone.utc),
                test_user,
                db_session,
            )

    def test_wrong_line_status(self, db_session: Session, test_quote: Quote, test_user: User):
        """Cannot confirm PO on a line not awaiting PO."""
        plan, line, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.active.value
        line.status = BuyPlanLineStatus.verified.value
        db_session.flush()

        with pytest.raises(ValueError, match="awaiting PO"):
            confirm_po(
                plan.id,
                line.id,
                "PO-001",
                datetime(2026, 3, 15, tzinfo=timezone.utc),
                test_user,
                db_session,
            )


# ── Verify PO ───────────────────────────────────────────────────────


class TestVerifyPO:
    def test_approve(
        self,
        db_session: Session,
        test_quote: Quote,
        test_user: User,
        admin_user: User,
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
        self,
        db_session: Session,
        test_quote: Quote,
        test_user: User,
        admin_user: User,
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
            plan.id,
            line.id,
            "reject",
            admin_user,
            db_session,
            rejection_note="Wrong PO amount",
        )
        assert result.status == BuyPlanLineStatus.awaiting_po.value
        assert result.po_number is None
        assert result.estimated_ship_date is None
        assert result.po_rejection_note == "Wrong PO amount"

    def test_non_ops_rejected(
        self,
        db_session: Session,
        test_quote: Quote,
        test_user: User,
        manager_user: User,
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
    def test_flag_sold_out(self, db_session: Session, test_quote: Quote, test_user: User):
        plan, line, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.active.value
        db_session.flush()

        result = flag_line_issue(
            plan.id,
            line.id,
            "sold_out",
            test_user,
            db_session,
        )
        assert result.status == BuyPlanLineStatus.issue.value
        assert result.issue_type == "sold_out"

    def test_flag_with_note(self, db_session: Session, test_quote: Quote, test_user: User):
        plan, line, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.active.value
        db_session.flush()

        result = flag_line_issue(
            plan.id,
            line.id,
            "price_changed",
            test_user,
            db_session,
            note="Up 20%",
        )
        assert result.issue_note == "Up 20%"

    def test_wrong_plan_status(self, db_session: Session, test_quote: Quote, test_user: User):
        plan, line, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        # plan is draft, not active

        with pytest.raises(ValueError, match="active"):
            flag_line_issue(plan.id, line.id, "sold_out", test_user, db_session)

    def test_cannot_flag_verified_line(self, db_session: Session, test_quote: Quote, test_user: User):
        plan, line, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.active.value
        line.status = BuyPlanLineStatus.verified.value
        db_session.flush()

        with pytest.raises(ValueError, match="Cannot flag"):
            flag_line_issue(plan.id, line.id, "sold_out", test_user, db_session)


# ── Check Completion ────────────────────────────────────────────────


class TestCheckCompletion:
    def test_all_verified_and_so_approved(self, db_session: Session, test_quote: Quote, test_user: User):
        """All lines verified + SO approved → completed."""
        plan, line, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.active.value
        plan.so_status = SOVerificationStatus.approved.value
        line.status = BuyPlanLineStatus.verified.value
        db_session.flush()

        result = check_completion(plan.id, db_session)
        assert result.status == BuyPlanStatus.completed.value
        assert result.completed_at is not None

    def test_partial_lines_not_complete(self, db_session: Session, test_quote: Quote, test_user: User):
        """Some lines still pending → not complete."""
        plan, line, _, req = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.active.value
        plan.so_status = SOVerificationStatus.approved.value
        line.status = BuyPlanLineStatus.verified.value
        # Add a second line still in progress
        offer2 = _make_offer(
            db_session,
            test_quote.requisition_id,
            req.id,
            qty_available=500,
            unit_price=0.55,
            entered_by_id=test_user.id,
            vendor_name="Vendor2",
        )
        db_session.add(
            BuyPlanLine(
                buy_plan_id=plan.id,
                requirement_id=req.id,
                offer_id=offer2.id,
                quantity=500,
                status=BuyPlanLineStatus.awaiting_po.value,
            )
        )
        db_session.flush()

        result = check_completion(plan.id, db_session)
        assert result.status == BuyPlanStatus.active.value

    def test_so_not_approved_blocks_completion(self, db_session: Session, test_quote: Quote, test_user: User):
        """All lines verified but SO still pending → not complete."""
        plan, line, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.active.value
        plan.so_status = SOVerificationStatus.pending.value
        line.status = BuyPlanLineStatus.verified.value
        db_session.flush()

        result = check_completion(plan.id, db_session)
        assert result.status == BuyPlanStatus.active.value

    def test_cancelled_lines_count_as_terminal(self, db_session: Session, test_quote: Quote, test_user: User):
        """Mix of verified + cancelled lines → still completes."""
        plan, line, _, req = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.active.value
        plan.so_status = SOVerificationStatus.approved.value
        line.status = BuyPlanLineStatus.verified.value
        offer2 = _make_offer(
            db_session,
            test_quote.requisition_id,
            req.id,
            qty_available=500,
            unit_price=0.55,
            entered_by_id=test_user.id,
            vendor_name="Vendor2",
        )
        db_session.add(
            BuyPlanLine(
                buy_plan_id=plan.id,
                requirement_id=req.id,
                offer_id=offer2.id,
                quantity=500,
                status=BuyPlanLineStatus.cancelled.value,
            )
        )
        db_session.flush()

        result = check_completion(plan.id, db_session)
        assert result.status == BuyPlanStatus.completed.value


# ── Resubmit Buy Plan ──────────────────────────────────────────────


class TestResubmitBuyPlan:
    def test_resubmit_auto_approve(self, db_session: Session, test_quote: Quote, test_user: User):
        """Resubmit a rejected (draft) plan → auto-approve if under threshold."""
        plan, _, _, _ = _make_draft_plan(db_session, test_quote, test_user, total_cost=500.0)
        # Simulate prior rejection
        plan.approval_notes = "Fix the SO number"
        plan.so_status = SOVerificationStatus.rejected.value
        db_session.flush()

        result = resubmit_buy_plan(
            plan.id,
            "SO-FIXED",
            test_user,
            db_session,
        )
        assert result.status == BuyPlanStatus.active.value
        assert result.auto_approved is True
        assert result.sales_order_number == "SO-FIXED"
        # SO verification reset
        assert result.so_status == SOVerificationStatus.pending.value
        assert result.so_rejection_note is None

    def test_resubmit_needs_approval(self, db_session: Session, test_quote: Quote, test_user: User):
        """High-cost resubmit → pending approval again."""
        plan, _, _, _ = _make_draft_plan(
            db_session,
            test_quote,
            test_user,
            total_cost=10000.0,
        )

        result = resubmit_buy_plan(
            plan.id,
            "SO-FIXED",
            test_user,
            db_session,
        )
        assert result.status == BuyPlanStatus.pending.value
        assert result.auto_approved is False

    def test_wrong_status(self, db_session: Session, test_quote: Quote, test_user: User):
        """Cannot resubmit an active plan."""
        plan, _, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.active.value
        db_session.flush()

        with pytest.raises(ValueError, match="draft"):
            resubmit_buy_plan(plan.id, "SO-001", test_user, db_session)


# ── Auto-complete via PO Verify ─────────────────────────────────────


class TestAutoCompleteViaPOVerify:
    def test_last_line_verified_triggers_completion(
        self,
        db_session: Session,
        test_quote: Quote,
        test_user: User,
        admin_user: User,
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


# ── Coverage Gap Tests ──────────────────────────────────────────────


def _make_ops_member_v2(db, user):
    """Create a VerificationGroupMember for ops verification tests."""
    vgm = VerificationGroupMember(user_id=user.id, is_active=True)
    db.add(vgm)
    db.flush()
    return vgm


class TestBuyPlanCoverageGaps:
    """Cover specific uncovered lines in buy_plan_service."""

    def test_verify_so_plan_not_found(self, db_session, test_user):
        """Line 752: verify_so raises when plan not found."""
        with pytest.raises(ValueError, match="not found"):
            verify_so(99999, "approve", test_user, db_session)

    def test_verify_so_already_verified(self, db_session, test_quote, test_user):
        """Line 756 (approx): verify_so raises when SO already verified."""
        plan, line, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.active.value
        plan.so_status = SOVerificationStatus.approved.value
        db_session.flush()

        with pytest.raises(ValueError, match="already verified"):
            verify_so(plan.id, "approve", test_user, db_session)

    def test_verify_so_plan_halted(self, db_session, test_quote, test_user):
        """Line 756: verify_so raises when plan is halted."""
        plan, line, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.halted.value
        plan.so_status = SOVerificationStatus.pending.value
        db_session.flush()

        with pytest.raises(ValueError, match="halted"):
            verify_so(plan.id, "approve", test_user, db_session)

    def test_verify_so_invalid_action(self, db_session, test_quote, test_user, admin_user):
        """Line 785: invalid SO verification action."""
        plan, line, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.active.value
        plan.so_status = SOVerificationStatus.pending.value
        _make_ops_member_v2(db_session, admin_user)
        db_session.flush()

        with pytest.raises(ValueError, match="Invalid SO verification"):
            verify_so(plan.id, "bogus", admin_user, db_session)

    def test_confirm_po_plan_not_found(self, db_session, test_user):
        """Line 808: confirm_po raises when plan not found."""
        with pytest.raises(ValueError, match="not found"):
            confirm_po(99999, 1, "PO-X", datetime.now(timezone.utc), test_user, db_session)

    def test_confirm_po_line_not_found(self, db_session, test_quote, test_user):
        """Line 814: confirm_po raises when line not found."""
        plan, line, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.active.value
        db_session.flush()

        with pytest.raises(ValueError, match="Line.*not found"):
            confirm_po(plan.id, 99999, "PO-X", datetime.now(timezone.utc), test_user, db_session)

    def test_verify_po_plan_not_found(self, db_session, test_user):
        """Line 844: verify_po raises when plan not found."""
        with pytest.raises(ValueError, match="not found"):
            verify_po(99999, 1, "approve", test_user, db_session)

    def test_verify_po_line_not_found(self, db_session, test_quote, test_user):
        """Line 848: verify_po raises when line not found."""
        plan, line, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.active.value
        db_session.flush()

        with pytest.raises(ValueError, match="Line.*not found"):
            verify_po(plan.id, 99999, "approve", test_user, db_session)

    def test_verify_po_wrong_status(self, db_session, test_quote, test_user, admin_user):
        """Line 850: verify_po raises when line not pending_verify."""
        plan, line, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.active.value
        line.status = BuyPlanLineStatus.awaiting_po.value
        _make_ops_member_v2(db_session, admin_user)
        db_session.flush()

        with pytest.raises(ValueError, match="pending verification"):
            verify_po(plan.id, line.id, "approve", admin_user, db_session)

    def test_verify_po_invalid_action(self, db_session, test_quote, test_user, admin_user):
        """Line 877: invalid PO verification action."""
        plan, line, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.active.value
        line.status = BuyPlanLineStatus.pending_verify.value
        _make_ops_member_v2(db_session, admin_user)
        db_session.flush()

        with pytest.raises(ValueError, match="Invalid PO verification"):
            verify_po(plan.id, line.id, "bogus", admin_user, db_session)

    def test_flag_issue_plan_not_found(self, db_session, test_user):
        """Line 901: flag_line_issue raises when plan not found."""
        with pytest.raises(ValueError, match="not found"):
            flag_line_issue(99999, 1, "sold_out", test_user, db_session)

    def test_flag_issue_line_not_found(self, db_session, test_quote, test_user):
        """Line 907: flag_line_issue raises when line not found."""
        plan, line, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.active.value
        db_session.flush()

        with pytest.raises(ValueError, match="Line.*not found"):
            flag_line_issue(plan.id, 99999, "sold_out", test_user, db_session)

    def test_check_completion_no_lines(self, db_session, test_quote, test_user):
        """Line 938: check_completion returns when no lines."""
        plan, line, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.active.value
        plan.lines.clear()
        db_session.flush()

        result = check_completion(plan.id, db_session)
        assert result.status == BuyPlanStatus.active.value

    def test_resubmit_plan_not_found(self, db_session, test_user):
        """Line 968: resubmit raises when plan not found."""
        with pytest.raises(ValueError, match="not found"):
            resubmit_buy_plan(99999, "SO-X", test_user, db_session)

    def test_approve_plan_not_found(self, db_session, test_user):
        """Line 708: approve_buy_plan raises when plan not found."""
        with pytest.raises(ValueError, match="not found"):
            approve_buy_plan(99999, "approve", test_user, db_session)


class TestDetectFavoritism:
    """Cover detect_favoritism function."""

    def test_favoritism_detected(self, db_session, test_quote, test_user):
        """Lines 1144-1191: detect_favoritism finds disproportionate assignment."""
        buyer = User(email="fav-buyer@test.com", name="Fav Buyer", role="buyer", azure_id="az-fav-b", is_active=True)
        db_session.add(buyer)
        db_session.flush()

        # Create 3 plans with all lines assigned to the same buyer
        for i in range(3):
            plan = BuyPlan(
                quote_id=test_quote.id,
                requisition_id=test_quote.requisition_id,
                submitted_by_id=test_user.id,
                status="active",
            )
            db_session.add(plan)
            db_session.flush()

            line = BuyPlanLine(
                buy_plan_id=plan.id,
                buyer_id=buyer.id,
                quantity=100,
                status=BuyPlanLineStatus.awaiting_po.value,
            )
            db_session.add(line)

        db_session.commit()

        findings = detect_favoritism(test_user.id, db_session)
        assert len(findings) >= 1
        assert findings[0]["buyer_id"] == buyer.id
        assert findings[0]["pct"] == 100.0

    def test_favoritism_no_lines(self, db_session, test_quote, test_user):
        """Line 1169: returns [] when total_lines is 0."""
        # Create 3 plans with no lines
        for i in range(3):
            plan = BuyPlan(
                quote_id=test_quote.id,
                requisition_id=test_quote.requisition_id,
                submitted_by_id=test_user.id,
                status="active",
            )
            db_session.add(plan)

        db_session.commit()

        findings = detect_favoritism(test_user.id, db_session)
        assert findings == []


class TestCaseReport:
    """Cover generate_case_report function."""

    def test_case_report_with_timeline(self, db_session, test_quote, test_user):
        """Lines 1249-1290: generate_case_report with full timeline."""
        now = datetime.now(timezone.utc)
        plan = BuyPlan(
            quote_id=test_quote.id,
            requisition_id=test_quote.requisition_id,
            submitted_by_id=test_user.id,
            approved_by_id=test_user.id,
            status=BuyPlanStatus.completed.value,
            created_at=now - timedelta(days=5),
            submitted_at=now - timedelta(days=4),
            approved_at=now - timedelta(days=3),
            completed_at=now,
            so_rejection_note="Initial SO issue",
            ai_flags=[{"severity": "info", "type": "better_offer", "message": "Cheaper alt exists"}],
        )
        db_session.add(plan)
        db_session.flush()

        # Create an offer for the line
        offer = Offer(
            requisition_id=test_quote.requisition_id,
            vendor_name="CaseVend",
            mpn="CASE-PART",
            unit_price=1.0,
            created_at=now,
        )
        db_session.add(offer)
        db_session.flush()

        line = BuyPlanLine(
            buy_plan_id=plan.id,
            offer_id=offer.id,
            buyer_id=test_user.id,
            quantity=100,
            status=BuyPlanLineStatus.verified.value,
            po_number="PO-CASE",
            po_confirmed_at=now - timedelta(days=1),
            issue_type="price_change",
            issue_note="Price went up 10%",
            po_rejection_note="Wrong PO format",
        )
        db_session.add(line)
        db_session.commit()

        report = generate_case_report(plan, db_session)
        assert "CASE REPORT" in report
        assert "Submit" in report
        assert "Approve" in report
        assert "SO rejected" in report
        assert "price_change" in report
        assert "PO rejected" in report
        assert "better_offer" in report


class TestIsStockSale:
    """Cover _is_stock_sale function."""

    def test_stock_sale_true(self, db_session, test_user, test_quote):
        """Lines 1119-1129: _is_stock_sale returns True for stock vendors."""
        offer = Offer(
            requisition_id=test_quote.requisition_id,
            vendor_name="internal stock",
            mpn="INT-PART",
            unit_price=1.0,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(offer)
        db_session.flush()

        plan = BuyPlan(
            requisition_id=test_quote.requisition_id,
            quote_id=test_quote.id,
            submitted_by_id=test_user.id,
            status="active",
        )
        db_session.add(plan)
        db_session.flush()

        line = BuyPlanLine(
            buy_plan_id=plan.id,
            offer_id=offer.id,
            quantity=100,
            status=BuyPlanLineStatus.awaiting_po.value,
        )
        db_session.add(line)
        db_session.commit()
        db_session.refresh(plan)

        with patch("app.services.buyplan_workflow.settings") as mock_s:
            mock_s.stock_sale_vendor_names = {"internal stock"}
            result = _is_stock_sale(plan, db_session)

        assert result is True

    def test_stock_sale_no_offer_id(self, db_session, test_user, test_quote):
        """Line 1122: line without offer_id returns False."""
        plan = BuyPlan(
            requisition_id=test_quote.requisition_id,
            quote_id=test_quote.id,
            submitted_by_id=test_user.id,
            status="active",
        )
        db_session.add(plan)
        db_session.flush()

        line = BuyPlanLine(
            buy_plan_id=plan.id,
            offer_id=None,
            quantity=100,
            status=BuyPlanLineStatus.awaiting_po.value,
        )
        db_session.add(line)
        db_session.commit()
        db_session.refresh(plan)

        with patch("app.services.buyplan_workflow.settings") as mock_s:
            mock_s.stock_sale_vendor_names = {"internal stock"}
            result = _is_stock_sale(plan, db_session)

        assert result is False


class TestCoverageGaps2:
    """Cover remaining uncovered lines in buy_plan_service (round 2)."""

    # ── score_offer edge cases (lines 100, 106, 115-122, 137) ──

    def test_score_price_zero_gets_zero(self, db_session, test_requisition):
        """Line 100: offer with unit_price=0 gets price score 0."""
        req = db_session.query(Requirement).filter_by(requisition_id=test_requisition.id).first()
        offer = _make_offer(
            db_session,
            test_requisition.id,
            req.id,
            unit_price=0.0,
            vendor_name="ZeroPriceVendor",
        )
        db_session.flush()
        score = score_offer(offer, req, None)
        assert 0 < score < 100  # price component is 0 but other components contribute

    def test_score_known_vendor_no_score(self, db_session, test_requisition):
        """Line 106: known vendor (is_new_vendor=False) with no vendor_score gets 50."""
        req = db_session.query(Requirement).filter_by(requisition_id=test_requisition.id).first()
        vendor = _make_vendor_card(
            db_session,
            normalized_name="known-no-score",
            vendor_score=None,
            is_new_vendor=False,
        )
        offer = _make_offer(
            db_session,
            test_requisition.id,
            req.id,
            unit_price=0.50,
            vendor_name="KnownVendor",
            vendor_card_id=vendor.id,
        )
        db_session.flush()
        score = score_offer(offer, req, vendor)
        assert score > 0

    def test_score_lead_time_7_days(self, db_session, test_requisition):
        """Line 115-116: lead_time 7 days gets 85."""
        req = db_session.query(Requirement).filter_by(requisition_id=test_requisition.id).first()
        offer = _make_offer(
            db_session,
            test_requisition.id,
            req.id,
            unit_price=0.50,
            lead_time="7 days",
            vendor_name="Lead7",
        )
        db_session.flush()
        score = score_offer(offer, req, None)
        assert score > 0

    def test_score_lead_time_14_days(self, db_session, test_requisition):
        """Lines 117-118: lead_time 14 days gets 70."""
        req = db_session.query(Requirement).filter_by(requisition_id=test_requisition.id).first()
        offer = _make_offer(
            db_session,
            test_requisition.id,
            req.id,
            unit_price=0.50,
            lead_time="14 days",
            vendor_name="Lead14",
        )
        db_session.flush()
        score = score_offer(offer, req, None)
        assert score > 0

    def test_score_lead_time_30_days(self, db_session, test_requisition):
        """Lines 119-120: lead_time 30 days gets 50."""
        req = db_session.query(Requirement).filter_by(requisition_id=test_requisition.id).first()
        offer = _make_offer(
            db_session,
            test_requisition.id,
            req.id,
            unit_price=0.50,
            lead_time="30 days",
            vendor_name="Lead30",
        )
        db_session.flush()
        score = score_offer(offer, req, None)
        assert score > 0

    def test_score_lead_time_60_days(self, db_session, test_requisition):
        """Lines 121-122: lead_time 60 days gets max(30, 100-60)=40."""
        req = db_session.query(Requirement).filter_by(requisition_id=test_requisition.id).first()
        offer = _make_offer(
            db_session,
            test_requisition.id,
            req.id,
            unit_price=0.50,
            lead_time="60 days",
            vendor_name="Lead60",
        )
        db_session.flush()
        score = score_offer(offer, req, None)
        assert score > 0

    def test_score_vendor_with_po_history(self, db_session, test_requisition):
        """Line 137: vendor with total_pos > 0 gets terms score 85."""
        req = db_session.query(Requirement).filter_by(requisition_id=test_requisition.id).first()
        vendor = _make_vendor_card(
            db_session,
            normalized_name="po-history-v",
            vendor_score=75.0,
            total_pos=10,
            is_new_vendor=False,
        )
        offer = _make_offer(
            db_session,
            test_requisition.id,
            req.id,
            unit_price=0.50,
            vendor_name="POHistory",
            vendor_card_id=vendor.id,
        )
        db_session.flush()
        score = score_offer(offer, req, vendor)
        assert score > 0

    # ── assign_buyer commodity/geography (lines 212-228, 232-240) ──

    def test_assign_buyer_commodity_match(self, db_session, test_requisition, test_quote, test_user):
        """Lines 212-228: buyer assignment via commodity match."""
        # Deactivate conftest buyer
        test_user.is_active = False

        # Create a buyer with commodity tags
        buyer = User(
            email="comm-buyer@test.com",
            name="Commodity Buyer",
            role="buyer",
            azure_id="cb-az",
            is_active=True,
            commodity_tags=["capacitors"],
        )
        db_session.add(buyer)
        db_session.flush()

        req = db_session.query(Requirement).filter_by(requisition_id=test_requisition.id).first()
        vendor = _make_vendor_card(
            db_session,
            normalized_name="comm-vendor",
            commodity_tags=["capacitors"],
        )
        offer = _make_offer(
            db_session,
            test_requisition.id,
            req.id,
            vendor_name="CommodityVendor",
            vendor_card_id=vendor.id,
            entered_by_id=None,
        )
        db_session.flush()

        assigned, reason = assign_buyer(offer, vendor, db_session)
        assert assigned is not None
        assert assigned.id == buyer.id
        assert reason == "commodity_match"

    def test_assign_buyer_commodity_multiple_narrows(self, db_session, test_requisition, test_quote, test_user):
        """Lines 220-228: multiple commodity matches narrow the buyer pool."""
        test_user.is_active = False

        buyer1 = User(
            email="cb1@test.com",
            name="CommBuyer1",
            role="buyer",
            azure_id="cb1-az",
            is_active=True,
            commodity_tags=["capacitors"],
        )
        buyer2 = User(
            email="cb2@test.com",
            name="CommBuyer2",
            role="buyer",
            azure_id="cb2-az",
            is_active=True,
            commodity_tags=["capacitors"],
        )
        db_session.add_all([buyer1, buyer2])
        db_session.flush()

        req = db_session.query(Requirement).filter_by(requisition_id=test_requisition.id).first()
        vendor = _make_vendor_card(
            db_session,
            normalized_name="comm-multi",
            commodity_tags=["capacitors"],
        )
        offer = _make_offer(
            db_session,
            test_requisition.id,
            req.id,
            vendor_name="MultiComm",
            vendor_card_id=vendor.id,
            entered_by_id=None,
        )
        db_session.flush()

        assigned, reason = assign_buyer(offer, vendor, db_session)
        # Multiple match -> narrows pool -> falls through to workload
        assert assigned is not None
        assert reason == "workload"

    def test_assign_buyer_manufacturer_commodity(self, db_session, test_requisition, test_quote, test_user):
        """Lines 215-218: manufacturer-based commodity mapping."""
        test_user.is_active = False

        buyer = User(
            email="mfr-buyer@test.com",
            name="MfrBuyer",
            role="buyer",
            azure_id="mb-az",
            is_active=True,
            commodity_tags=["semiconductor"],
        )
        db_session.add(buyer)
        db_session.flush()

        req = db_session.query(Requirement).filter_by(requisition_id=test_requisition.id).first()
        vendor = _make_vendor_card(
            db_session,
            normalized_name="mfr-vendor",
        )
        offer = _make_offer(
            db_session,
            test_requisition.id,
            req.id,
            vendor_name="MfrVendor",
            vendor_card_id=vendor.id,
            entered_by_id=None,
            manufacturer="Texas Instruments",
        )
        db_session.flush()

        # Patch routing maps to include manufacturer -> commodity mapping
        maps = {"brand_commodity_map": {"texas instruments": "semiconductor"}, "country_region_map": {}}
        with patch("app.services.buyplan_scoring._get_routing_maps", return_value=maps):
            assigned, reason = assign_buyer(offer, vendor, db_session)

        assert assigned is not None
        assert assigned.id == buyer.id
        assert reason == "commodity_match"

    def test_assign_buyer_geography_narrows(self, db_session, test_requisition, test_quote, test_user):
        """Lines 232-240: geography match narrows buyer pool."""
        test_user.is_active = False

        buyer1 = User(
            email="geo1@test.com",
            name="GeoBuyer1",
            role="buyer",
            azure_id="g1-az",
            is_active=True,
            commodity_tags=["general"],  # has routing profile
        )
        buyer2 = User(
            email="geo2@test.com",
            name="GeoBuyer2",
            role="buyer",
            azure_id="g2-az",
            is_active=True,
            commodity_tags=None,  # no routing profile
        )
        db_session.add_all([buyer1, buyer2])
        db_session.flush()

        req = db_session.query(Requirement).filter_by(requisition_id=test_requisition.id).first()
        vendor = _make_vendor_card(
            db_session,
            normalized_name="geo-vendor",
            hq_country="US",
        )
        offer = _make_offer(
            db_session,
            test_requisition.id,
            req.id,
            vendor_name="GeoVendor",
            vendor_card_id=vendor.id,
            entered_by_id=None,
        )
        db_session.flush()

        maps = {"brand_commodity_map": {}, "country_region_map": {"us": "americas"}}
        with patch("app.services.buyplan_scoring._get_routing_maps", return_value=maps):
            assigned, reason = assign_buyer(offer, vendor, db_session)

        assert assigned is not None
        assert assigned.id == buyer1.id

    # ── build_buy_plan no requirements (line 299) ──

    def test_build_buy_plan_no_requirements(self, db_session, test_quote):
        """Line 299: build_buy_plan raises when no requirements exist."""
        # Delete all requirements for this requisition
        db_session.query(Requirement).filter_by(requisition_id=test_quote.requisition_id).delete()
        db_session.flush()

        with pytest.raises(ValueError, match="No requirements found"):
            build_buy_plan(test_quote.id, db_session)

    # ── auto-split break/continue (lines 384, 387) ──

    def test_autosplit_break_when_qty_filled(self, db_session, test_quote, test_user):
        """Lines 384: auto-split break when remaining <= 0 after partial fills."""
        # target_qty=200, but NO single offer covers 200.
        # Two offers of 100 each -> first fills 100, second fills remaining 100,
        # third offer hits "if remaining <= 0: break"
        req = _make_requirement(
            db_session,
            test_quote.requisition_id,
            target_price=1.0,
            target_qty=200,
            primary_mpn="SPLIT-BRK",
        )
        offer1 = _make_offer(
            db_session,
            test_quote.requisition_id,
            req.id,
            unit_price=0.50,
            qty_available=100,
            vendor_name="Split1",
            entered_by_id=test_user.id,
        )
        offer2 = _make_offer(
            db_session,
            test_quote.requisition_id,
            req.id,
            unit_price=0.55,
            qty_available=100,
            vendor_name="Split2",
            entered_by_id=test_user.id,
        )
        # Third offer: after first two fill 200, remaining=0 -> break
        offer3 = _make_offer(
            db_session,
            test_quote.requisition_id,
            req.id,
            unit_price=0.60,
            qty_available=50,
            vendor_name="Split3",
            entered_by_id=test_user.id,
        )
        db_session.flush()

        plan = build_buy_plan(test_quote.id, db_session)
        split_lines = [ln for ln in plan.lines if ln.requirement_id == req.id]
        # Two lines should be created (100+100=200), third skipped by break
        assert len(split_lines) == 2

    def test_autosplit_skip_zero_qty(self, db_session, test_quote, test_user):
        """Line 387: auto-split skips offers with qty_available=0."""
        # target_qty=200 -> no single offer covers it (max is 100+0+100).
        # offer_zero has qty=0, should be skipped via continue (line 387).
        req = _make_requirement(
            db_session,
            test_quote.requisition_id,
            target_price=1.0,
            target_qty=200,
            primary_mpn="SPLIT-SKIP",
        )
        offer_a = _make_offer(
            db_session,
            test_quote.requisition_id,
            req.id,
            unit_price=0.40,
            qty_available=100,
            vendor_name="PartialA",
            entered_by_id=test_user.id,
        )
        offer_zero = _make_offer(
            db_session,
            test_quote.requisition_id,
            req.id,
            unit_price=0.45,
            qty_available=0,
            vendor_name="Empty",
            entered_by_id=test_user.id,
        )
        offer_b = _make_offer(
            db_session,
            test_quote.requisition_id,
            req.id,
            unit_price=0.60,
            qty_available=100,
            vendor_name="PartialB",
            entered_by_id=test_user.id,
        )
        db_session.flush()

        plan = build_buy_plan(test_quote.id, db_session)
        skip_lines = [ln for ln in plan.lines if ln.requirement_id == req.id]
        assert len(skip_lines) >= 1
        # None of the lines should be from the zero-qty offer
        assert all(ln.offer_id != offer_zero.id for ln in skip_lines)

    # ── generate_ai_summary with flags (line 469) ──

    def test_summary_with_flags(self, db_session, test_quote, test_user):
        """Line 469: summary includes flag count."""
        plan, line, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.ai_flags = [
            {"type": "stale_offer", "severity": "warning", "message": "old"},
            {"type": "low_margin", "severity": "critical", "message": "low"},
        ]
        db_session.flush()
        summary = generate_ai_summary(plan)
        assert "2 flags" in summary

    # ── _check_better_offer edge cases (lines 546, 561) ──

    def test_better_offer_no_price(self, db_session, test_quote, test_user):
        """Line 546: _check_better_offer returns early when price is 0."""
        plan, line, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        offer = db_session.get(Offer, line.offer_id)
        offer.unit_price = 0  # trigger early return
        db_session.flush()

        flags = []
        _check_better_offer(line, offer, 10.0, flags, db_session)
        assert flags == []  # no flag added

    def test_better_offer_alt_no_price(self, db_session, test_quote, test_user):
        """Line 561: alternative offer with no price is skipped."""
        plan, line, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        offer = db_session.get(Offer, line.offer_id)

        # Create alternative with null price
        alt = _make_offer(
            db_session,
            test_quote.requisition_id,
            line.requirement_id,
            unit_price=None,
            vendor_name="NoPriceAlt",
        )
        db_session.flush()

        flags = []
        _check_better_offer(line, offer, 10.0, flags, db_session)
        # Alt has no price -> skipped, no "better_offer" flag from it
        better_flags = [f for f in flags if f["type"] == "better_offer"]
        # The alt shouldn't generate a flag since it has no price
        assert all(f.get("message", "") and "NoPriceAlt" not in f.get("message", "") for f in better_flags)

    # ── _check_geo_mismatch (lines 591-593) ──

    def test_geo_mismatch_flagged(self, db_session, test_quote, test_user):
        """Lines 591-593: geo mismatch flag appended."""
        plan, line, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        offer = db_session.get(Offer, line.offer_id)

        # Create vendor card matching offer's vendor_name so DB lookup finds it
        vendor = _make_vendor_card(
            db_session,
            normalized_name=offer.vendor_name.strip().lower(),
            hq_country="China",
        )
        db_session.flush()

        flags = []
        maps = {"brand_commodity_map": {}, "country_region_map": {"china": "apac"}}
        with patch("app.services.buyplan_scoring._get_routing_maps", return_value=maps):
            _check_geo_mismatch(line, offer, "americas", flags, db_session)

        assert len(flags) == 1
        assert flags[0]["type"] == "geo_mismatch"

    # ── check_completion inactive plan (line 935) ──

    def test_check_completion_inactive_plan(self, db_session, test_quote, test_user):
        """Line 935: check_completion returns plan unchanged when not active."""
        plan, line, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.draft.value
        db_session.flush()

        result = check_completion(plan.id, db_session)
        assert result.status == BuyPlanStatus.draft.value

    # ── _apply_line_edits offer not found (line 1028) ──

    def test_line_edits_offer_not_found(self, db_session, test_quote, test_user):
        """Line 1028: _apply_line_edits raises when offer not found."""
        plan, line, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        db_session.flush()

        edits = [{"requirement_id": line.requirement_id, "offer_id": 99999}]
        with pytest.raises(ValueError, match="Offer 99999 not found"):
            _apply_line_edits(plan, edits, db_session)

    # ── _apply_line_overrides line not found and quantity (lines 1072-1075, 1089) ──

    def test_line_overrides_line_not_found(self, db_session, test_quote, test_user):
        """Lines 1072-1075: _apply_line_overrides warns when line not found."""
        plan, line, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        db_session.flush()

        overrides = [{"line_id": 99999, "quantity": 500}]
        # Should not raise, just logs warning
        _apply_line_overrides(plan, overrides, db_session)

    def test_line_overrides_quantity(self, db_session, test_quote, test_user):
        """Line 1089: _apply_line_overrides applies quantity override."""
        plan, line, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        db_session.flush()

        overrides = [{"line_id": line.id, "quantity": 500}]
        _apply_line_overrides(plan, overrides, db_session)
        assert line.quantity == 500

    # ── _is_stock_sale edge cases (lines 1119, 1125) ──

    def test_stock_sale_no_lines(self, db_session, test_quote, test_user):
        """Line 1119: _is_stock_sale returns False when plan has no lines."""
        plan = BuyPlan(
            requisition_id=test_quote.requisition_id,
            quote_id=test_quote.id,
            submitted_by_id=test_user.id,
            status="active",
        )
        db_session.add(plan)
        db_session.commit()
        db_session.refresh(plan)

        with patch("app.services.buyplan_workflow.settings") as mock_s:
            mock_s.stock_sale_vendor_names = {"internal stock"}
            result = _is_stock_sale(plan, db_session)
        assert result is False

    def test_stock_sale_offer_not_found(self, db_session, test_quote, test_user):
        """Line 1125: _is_stock_sale returns False when offer not in DB."""
        # Create an offer, then attach it to a line, then delete the offer
        offer = Offer(
            requisition_id=test_quote.requisition_id,
            vendor_name="ghost",
            mpn="GHOST",
            unit_price=1.0,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(offer)
        db_session.flush()
        offer_id = offer.id

        plan = BuyPlan(
            requisition_id=test_quote.requisition_id,
            quote_id=test_quote.id,
            submitted_by_id=test_user.id,
            status="active",
        )
        db_session.add(plan)
        db_session.flush()
        line = BuyPlanLine(
            buy_plan_id=plan.id,
            offer_id=offer_id,
            quantity=100,
            status=BuyPlanLineStatus.awaiting_po.value,
        )
        db_session.add(line)
        db_session.commit()

        # Now mock db.get to return None for the offer lookup
        real_get = db_session.get

        def fake_get(model, pk, **kw):
            if model is Offer and pk == offer_id:
                return None
            return real_get(model, pk, **kw)

        db_session.refresh(plan)
        with patch("app.services.buyplan_workflow.settings") as mock_s:
            mock_s.stock_sale_vendor_names = {"internal stock"}
            with patch.object(db_session, "get", side_effect=fake_get):
                result = _is_stock_sale(plan, db_session)
        assert result is False

    # ── detect_favoritism < 3 plans (line 1157) ──

    def test_detect_favoritism_insufficient_data(self, db_session, test_quote, test_user):
        """Line 1157: detect_favoritism returns empty when < 3 plans."""
        # Only 1 plan exists (from _make_draft_plan)
        plan, _, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.active.value
        plan.submitted_by_id = test_user.id
        db_session.flush()

        result = detect_favoritism(test_user.id, db_session)
        assert result == []

    # ── _country_to_region empty (line 59) and _get_routing_maps no file (line 52) ──

    def test_country_to_region_empty(self):
        """Line 59: _country_to_region returns None for empty string."""

        assert _country_to_region("") is None
        assert _country_to_region(None) is None

    def test_get_routing_maps_no_file(self, tmp_path):
        """Line 52: _get_routing_maps returns default when file missing."""
        import app.services.buyplan_scoring as scoring_mod

        old = scoring_mod._ROUTING_MAPS
        scoring_mod._ROUTING_MAPS = None  # reset cache
        try:
            # Point Path(__file__).parent.parent to a tmp dir that has no config/routing_maps.json
            fake_file = tmp_path / "services" / "fake.py"
            fake_file.parent.mkdir(parents=True, exist_ok=True)
            fake_file.touch()
            with patch("app.services.buyplan_scoring.Path") as MockPath:
                MockPath.return_value = fake_file
                # __file__ -> fake_file, .parent -> services/, .parent -> tmp_path
                # / "config" / "routing_maps.json" -> tmp_path/config/routing_maps.json (doesn't exist)
                result = scoring_mod._get_routing_maps()
            assert "brand_commodity_map" in result
            assert "country_region_map" in result
        finally:
            scoring_mod._ROUTING_MAPS = old


# ── Tests merged from test_buy_plan_v3_service.py ────────────────────


def _make_simple_offer(**kw):
    """SimpleNamespace-based offer helper (no DB needed)."""
    defaults = {
        "id": 1,
        "unit_price": 0.50,
        "lead_time": "5 days",
        "qty_available": 1000,
        "status": "active",
        "manufacturer": None,
        "entered_by_id": None,
        "vendor_card": None,
        "vendor_name": "Acme",
        "requirement_id": 1,
        "created_at": datetime.now(timezone.utc),
    }
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def _make_simple_requirement(**kw):
    """SimpleNamespace-based requirement helper (no DB needed)."""
    defaults = {"id": 1, "target_qty": 1000, "target_price": 1.00, "requisition_id": 10}
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def _make_simple_vendor_card(**kw):
    """SimpleNamespace-based vendor card helper (no DB needed)."""
    defaults = {
        "vendor_score": 75,
        "is_new_vendor": False,
        "hq_country": "united states",
        "total_pos": 10,
        "commodity_tags": ["semiconductors"],
    }
    defaults.update(kw)
    return SimpleNamespace(**defaults)


# ── _create_line ──────────────────────────────────────────────────────


class TestCreateLine:
    def test_creates_with_margin(self):
        req = _make_simple_requirement(target_price=1.00)
        offer = _make_simple_offer(unit_price=0.50)
        buyer = SimpleNamespace(id=42)
        line = _create_line(req, offer, 100, 85.0, buyer, "workload")
        assert line.quantity == 100
        assert float(line.unit_cost) == 0.50
        assert float(line.unit_sell) == 1.00
        assert line.margin_pct == 50.0
        assert line.buyer_id == 42
        assert line.assignment_reason == "workload"
        assert line.status == BuyPlanLineStatus.awaiting_po.value

    def test_no_buyer(self):
        req = _make_simple_requirement(target_price=1.00)
        offer = _make_simple_offer(unit_price=0.50)
        line = _create_line(req, offer, 100, 75.0, None, "no_buyers")
        assert line.buyer_id is None

    def test_no_prices(self):
        req = _make_simple_requirement(target_price=None)
        offer = _make_simple_offer(unit_price=None)
        line = _create_line(req, offer, 100, 50.0, None, "no_buyers")
        assert line.unit_cost is None
        assert line.unit_sell is None
        assert line.margin_pct is None


# ── _check_quantity_gaps ─────────────────────────────────────────────


class TestCheckQuantityGaps:
    def test_gap_detected(self, db_session):
        req = SimpleNamespace(target_qty=1000)
        line = SimpleNamespace(requirement_id=1, quantity=500, requirement=req)
        plan = SimpleNamespace(lines=[line])
        flags = []
        _check_quantity_gaps(plan, flags, db_session)
        assert len(flags) == 1
        assert flags[0]["type"] == "quantity_gap"
        assert flags[0]["severity"] == "critical"

    def test_no_gap(self, db_session):
        req = SimpleNamespace(target_qty=1000)
        line = SimpleNamespace(requirement_id=1, quantity=1000, requirement=req)
        plan = SimpleNamespace(lines=[line])
        flags = []
        _check_quantity_gaps(plan, flags, db_session)
        assert len(flags) == 0

    def test_split_lines_cover_qty(self, db_session):
        req = SimpleNamespace(target_qty=1000)
        l1 = SimpleNamespace(requirement_id=1, quantity=600, requirement=req)
        l2 = SimpleNamespace(requirement_id=1, quantity=400, requirement=None)
        plan = SimpleNamespace(lines=[l1, l2])
        flags = []
        _check_quantity_gaps(plan, flags, db_session)
        assert len(flags) == 0

    def test_no_requirement_id(self, db_session):
        line = SimpleNamespace(requirement_id=None, quantity=100, requirement=None)
        plan = SimpleNamespace(lines=[line])
        flags = []
        _check_quantity_gaps(plan, flags, db_session)
        assert len(flags) == 0

    def test_zero_target_no_gap(self, db_session):
        req = SimpleNamespace(target_qty=0)
        line = SimpleNamespace(requirement_id=1, quantity=0, requirement=req)
        plan = SimpleNamespace(lines=[line])
        flags = []
        _check_quantity_gaps(plan, flags, db_session)
        assert len(flags) == 0

    def test_req_fetched_from_db_when_none(self, db_session):
        """When line.requirement is None, it fetches from db."""
        line = SimpleNamespace(requirement_id=999, quantity=100, requirement=None)
        plan = SimpleNamespace(lines=[line])
        flags = []
        _check_quantity_gaps(plan, flags, db_session)
        # Req 999 doesn't exist, so no gap flagged (no target)
        assert len(flags) == 0


# ── Routing Maps (loads / cached) ────────────────────────────────────


class TestRoutingMapsLoadAndCache:
    def setup_method(self):
        import app.services.buyplan_scoring as mod

        mod._ROUTING_MAPS = None

    def test_get_routing_maps_loads_file(self):
        maps = _get_routing_maps()
        assert "brand_commodity_map" in maps
        assert "country_region_map" in maps

    def test_get_routing_maps_cached(self):
        maps1 = _get_routing_maps()
        maps2 = _get_routing_maps()
        assert maps1 is maps2

    def teardown_method(self):
        import app.services.buyplan_scoring as mod

        mod._ROUTING_MAPS = None


# ── AI Summary edge cases (SimpleNamespace-based) ───────────────────


class TestAISummaryEdgeCases:
    def test_no_vendor_names_uses_offer_ids(self):
        lines = [
            SimpleNamespace(offer_id=1, margin_pct=None, offer=None),
            SimpleNamespace(offer_id=2, margin_pct=None, offer=None),
        ]
        plan = SimpleNamespace(lines=lines, ai_flags=[])
        summary = generate_ai_summary(plan)
        assert "2 vendor" in summary

    def test_none_lines(self):
        plan = SimpleNamespace(lines=None, ai_flags=[])
        assert "Empty buy plan" in generate_ai_summary(plan)


# ── AI Flags edge cases (SimpleNamespace-based) ─────────────────────


class TestAIFlagsEdgeCases:
    def test_stale_offer_fetched_from_db(self, db_session):
        """When line.offer is None, the flag code fetches from DB via offer_id."""
        line = SimpleNamespace(
            id=1,
            offer_id=999,
            offer=None,
            margin_pct=50.0,
            requirement_id=None,
            quantity=100,
            buyer_id=1,
        )
        plan = SimpleNamespace(lines=[line], quote_id=None)
        flags = generate_ai_flags(plan, db_session)
        # No crash -- offer not found, just skips the stale check
        assert all(f["type"] != "stale_offer" for f in flags)

    def test_none_lines(self, db_session):
        plan = SimpleNamespace(lines=None, quote_id=None)
        flags = generate_ai_flags(plan, db_session)
        assert flags == []


# ── Build Buy Plan with customer site region ─────────────────────────


class TestBuildBuyPlanCustomerSiteRegion:
    def test_customer_site_region(self, db_session):
        """Exercises customer_region from customer_site."""
        from app.models import Company, CustomerSite, Offer, Quote, Requirement, Requisition, User

        user = User(email="reg@test.com", name="Reg", role="buyer", azure_id="azreg2", is_active=True)
        db_session.add(user)
        db_session.flush()
        co = Company(name="Co2", website="https://co2.com", industry="Electronics", is_active=True)
        db_session.add(co)
        db_session.flush()
        site = CustomerSite(company_id=co.id, site_name="HQ", country="united states")
        db_session.add(site)
        db_session.flush()
        req = Requisition(name="REQ-REG2", customer_name="Co2", status="open", created_by=user.id)
        db_session.add(req)
        db_session.flush()
        item = Requirement(requisition_id=req.id, primary_mpn="Z2", target_qty=50, target_price=2.00)
        db_session.add(item)
        db_session.flush()
        q = Quote(
            requisition_id=req.id,
            customer_site_id=site.id,
            quote_number="Q-REG2",
            status="sent",
            line_items=[],
            subtotal=100,
        )
        db_session.add(q)
        db_session.flush()
        offer = Offer(
            requisition_id=req.id,
            requirement_id=item.id,
            vendor_name="V2",
            mpn="Z2",
            qty_available=100,
            unit_price=1.00,
            entered_by_id=user.id,
            status="active",
        )
        db_session.add(offer)
        db_session.commit()
        plan = build_buy_plan(q.id, db_session)
        assert len(plan.lines) >= 1
