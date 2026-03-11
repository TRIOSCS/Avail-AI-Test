"""
test_buyplan_builder_guards.py — Tests for Phase 4 validation guards in buyplan_builder.py

Covers:
- Quote status validation (must be won or sent)
- Duplicate buy plan prevention
- No-buyer critical AI flag

Called by: pytest
Depends on: conftest fixtures, app/services/buyplan_builder.py
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session

from app.models import Company, CustomerSite, Offer, Quote, Requirement, Requisition, User, VendorCard
from app.models.buy_plan import BuyPlanLineStatus, BuyPlanStatus, BuyPlanV3
from app.services.buyplan_builder import build_buy_plan, generate_ai_flags


# ── Helpers ────────────────────────────────────────────────────────────


def _setup_quote_with_offer(db: Session, *, quote_status="won"):
    """Create a full quote → requisition → requirement → offer chain."""
    user = User(
        email="builder-test@trioscs.com", name="Builder Test", role="sales",
        azure_id="az-builder-test", created_at=datetime.now(timezone.utc),
    )
    db.add(user)
    db.flush()

    company = Company(
        name="Test Corp", is_active=True, created_at=datetime.now(timezone.utc),
    )
    db.add(company)
    db.flush()

    site = CustomerSite(
        company_id=company.id, site_name="HQ",
        created_at=datetime.now(timezone.utc),
    )
    db.add(site)
    db.flush()

    req = Requisition(
        name="REQ-BUILDER-TEST", status="won", created_by=user.id,
        customer_site_id=site.id, created_at=datetime.now(timezone.utc),
    )
    db.add(req)
    db.flush()

    requirement = Requirement(
        requisition_id=req.id, primary_mpn="TEST-MPN",
        target_qty=100, target_price=1.0,
        created_at=datetime.now(timezone.utc),
    )
    db.add(requirement)
    db.flush()

    vendor = VendorCard(
        normalized_name="test vendor", display_name="Test Vendor",
        created_at=datetime.now(timezone.utc),
    )
    db.add(vendor)
    db.flush()

    offer = Offer(
        requisition_id=req.id, requirement_id=requirement.id,
        vendor_card_id=vendor.id, vendor_name="Test Vendor",
        mpn="TEST-MPN", qty_available=100, unit_price=0.50,
        status="active", entered_by_id=user.id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(offer)
    db.flush()

    quote = Quote(
        requisition_id=req.id, customer_site_id=site.id,
        quote_number="Q-BUILD-001", status=quote_status,
        created_by_id=user.id, created_at=datetime.now(timezone.utc),
    )
    db.add(quote)
    db.flush()

    return quote, req, requirement, offer, user


# ── Quote status validation ──────────────────────────────────────────


class TestBuildBuyPlanQuoteStatus:
    def test_rejects_draft_quote(self, db_session):
        """Should not build buy plan from a draft quote."""
        quote, *_ = _setup_quote_with_offer(db_session, quote_status="draft")
        with pytest.raises(ValueError, match="must be won or sent"):
            build_buy_plan(quote.id, db_session)

    def test_rejects_lost_quote(self, db_session):
        """Should not build buy plan from a lost quote."""
        quote, *_ = _setup_quote_with_offer(db_session, quote_status="lost")
        with pytest.raises(ValueError, match="must be won or sent"):
            build_buy_plan(quote.id, db_session)

    def test_accepts_won_quote(self, db_session):
        """Should build buy plan from a won quote."""
        quote, *_ = _setup_quote_with_offer(db_session, quote_status="won")
        plan = build_buy_plan(quote.id, db_session)
        assert plan is not None
        assert plan.status == BuyPlanStatus.draft.value

    def test_accepts_sent_quote(self, db_session):
        """Should build buy plan from a sent quote."""
        quote, *_ = _setup_quote_with_offer(db_session, quote_status="sent")
        plan = build_buy_plan(quote.id, db_session)
        assert plan is not None


# ── Duplicate prevention ─────────────────────────────────────────────


class TestBuildBuyPlanDuplicate:
    def test_rejects_duplicate(self, db_session):
        """Should not build a second buy plan for the same quote."""
        quote, req, *_ = _setup_quote_with_offer(db_session)

        # Create first plan
        existing = BuyPlanV3(
            quote_id=quote.id, requisition_id=req.id,
            status=BuyPlanStatus.draft.value,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(existing)
        db_session.flush()

        with pytest.raises(ValueError, match="already exists"):
            build_buy_plan(quote.id, db_session)

    def test_allows_after_cancel(self, db_session):
        """Should allow building a new plan if old one is cancelled."""
        quote, req, *_ = _setup_quote_with_offer(db_session)

        cancelled = BuyPlanV3(
            quote_id=quote.id, requisition_id=req.id,
            status=BuyPlanStatus.cancelled.value,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(cancelled)
        db_session.flush()

        plan = build_buy_plan(quote.id, db_session)
        assert plan is not None


# ── No-buyer AI flag ─────────────────────────────────────────────────


class TestNoBuyerFlag:
    def test_no_buyer_generates_critical_flag(self, db_session):
        """Lines with no buyer should get a critical AI flag."""
        quote, req, requirement, offer, user = _setup_quote_with_offer(db_session)

        plan = BuyPlanV3(
            quote_id=quote.id, requisition_id=req.id,
            status=BuyPlanStatus.draft.value,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(plan)
        db_session.flush()

        from app.models.buy_plan import BuyPlanLine

        line = BuyPlanLine(
            buy_plan_id=plan.id, requirement_id=requirement.id,
            offer_id=offer.id, quantity=100,
            unit_cost=0.50, unit_sell=1.0,
            buyer_id=None, assignment_reason="no_buyers",
            status=BuyPlanLineStatus.awaiting_po.value,
        )
        db_session.add(line)
        db_session.flush()

        flags = generate_ai_flags(plan, db_session)
        no_buyer_flags = [f for f in flags if f["type"] == "no_buyer"]
        assert len(no_buyer_flags) == 1
        assert no_buyer_flags[0]["severity"] == "critical"
