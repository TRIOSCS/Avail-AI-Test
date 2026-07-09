"""test_buyplan_builder_extra.py — Additional coverage for buyplan_builder.py.

Covers:
- Quote-not-found guard (line 55)
- No-requirements guard (line 83)
- No-offers path in _build_lines_for_requirement (line 140)
- Auto-split path (lines 160-178)
- generate_ai_summary edge cases (lines 222, 231, 250)
- generate_ai_flags: stale offer, low/negative margin (lines 288, 299)
- _check_better_offer: zero price early return, better offer found (lines 345, 359-374)
- _check_geo_mismatch: geo mismatch flag (lines 392-394)
- _check_quantity_gaps: quantity gap flag (lines 420-421)

Called by: pytest
Depends on: conftest fixtures, app/services/buyplan_builder.py
"""

import os

os.environ["TESTING"] = "1"

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from sqlalchemy.orm import Session

from app.models import Company, CustomerSite, Offer, Quote, Requirement, Requisition, User, VendorCard
from app.models.buy_plan import BuyPlan, BuyPlanLine, BuyPlanLineStatus, BuyPlanStatus
from app.services.buyplan_builder import (
    _check_better_offer,
    _check_geo_mismatch,
    _check_quantity_gaps,
    build_buy_plan,
    generate_ai_flags,
    generate_ai_summary,
)

# ── Shared helpers ────────────────────────────────────────────────────


def _make_user(db: Session, email="bbt@trioscs.com") -> User:
    u = User(email=email, name="BB Tester", role="buyer", azure_id=f"az-{email}", created_at=datetime.now(UTC))
    db.add(u)
    db.flush()
    return u


def _make_company(db: Session) -> Company:
    c = Company(name="BB Corp", is_active=True, created_at=datetime.now(UTC))
    db.add(c)
    db.flush()
    return c


def _make_site(db: Session, company: Company, country=None) -> CustomerSite:
    s = CustomerSite(company_id=company.id, site_name="HQ", country=country, created_at=datetime.now(UTC))
    db.add(s)
    db.flush()
    return s


def _make_requisition(db: Session, user: User, site: CustomerSite) -> Requisition:
    r = Requisition(
        name="REQ-BB", status="won", created_by=user.id, customer_site_id=site.id, created_at=datetime.now(UTC)
    )
    db.add(r)
    db.flush()
    return r


def _make_requirement(db: Session, req: Requisition, mpn="T123", qty=100, price=1.0) -> Requirement:
    r = Requirement(
        requisition_id=req.id,
        primary_mpn=mpn,
        target_qty=qty,
        target_price=price,
        created_at=datetime.now(UTC),
    )
    db.add(r)
    db.flush()
    return r


def _make_vendor(db: Session, name="Test Vendor", country=None) -> VendorCard:
    v = VendorCard(normalized_name=name.lower(), display_name=name, hq_country=country, created_at=datetime.now(UTC))
    db.add(v)
    db.flush()
    return v


def _make_offer(
    db: Session,
    req: Requisition,
    requirement: Requirement,
    vendor: VendorCard,
    *,
    qty=200,
    price=0.50,
    age_days=0,
    status="active",
) -> Offer:
    created = datetime.now(UTC) - timedelta(days=age_days)
    o = Offer(
        requisition_id=req.id,
        requirement_id=requirement.id,
        vendor_card_id=vendor.id,
        vendor_name=vendor.display_name,
        mpn=requirement.primary_mpn,
        qty_available=qty,
        unit_price=price,
        status=status,
        created_at=created,
    )
    db.add(o)
    db.flush()
    return o


def _make_quote(db: Session, req: Requisition, site: CustomerSite, user: User, status="won") -> Quote:
    q = Quote(
        requisition_id=req.id,
        customer_site_id=site.id,
        quote_number="Q-BB-001",
        status=status,
        created_by_id=user.id,
        created_at=datetime.now(UTC),
    )
    db.add(q)
    db.flush()
    return q


def _make_plan_with_line(
    db: Session,
    quote: Quote,
    req: Requisition,
    requirement: Requirement,
    offer: Offer,
    *,
    margin=30.0,
    buyer_id=None,
    qty=50,
) -> tuple[BuyPlan, BuyPlanLine]:
    plan = BuyPlan(
        quote_id=quote.id,
        requisition_id=req.id,
        status=BuyPlanStatus.DRAFT.value,
        created_at=datetime.now(UTC),
    )
    db.add(plan)
    db.flush()
    line = BuyPlanLine(
        buy_plan_id=plan.id,
        requirement_id=requirement.id,
        offer_id=offer.id,
        quantity=qty,
        unit_cost=0.50,
        unit_sell=1.0,
        margin_pct=margin,
        buyer_id=buyer_id,
        status=BuyPlanLineStatus.AWAITING_PO.value,
    )
    db.add(line)
    db.flush()
    return plan, line


# ── build_buy_plan guards ─────────────────────────────────────────────


class TestBuildBuyPlanGuards:
    def test_raises_when_quote_not_found(self, db_session):
        with pytest.raises(ValueError, match="not found"):
            build_buy_plan(999999, db_session)

    def test_raises_when_no_requirements(self, db_session):
        user = _make_user(db_session)
        company = _make_company(db_session)
        site = _make_site(db_session, company)
        req = _make_requisition(db_session, user, site)
        # No requirements added
        quote = _make_quote(db_session, req, site, user)
        with pytest.raises(ValueError, match="No requirements"):
            build_buy_plan(quote.id, db_session)


# ── _build_lines_for_requirement — no-offers path ────────────────────


class TestBuildLinesNoOffers:
    def test_returns_empty_when_no_active_offers(self, db_session):
        user = _make_user(db_session)
        company = _make_company(db_session)
        site = _make_site(db_session, company)
        req = _make_requisition(db_session, user, site)
        requirement = _make_requirement(db_session, req)
        vendor = _make_vendor(db_session)
        # Add offer with inactive status
        _make_offer(db_session, req, requirement, vendor, status="inactive")
        quote = _make_quote(db_session, req, site, user)
        plan = build_buy_plan(quote.id, db_session)
        assert plan is not None
        assert plan.lines == [] or plan.lines is None or len(plan.lines) == 0


# ── Auto-split path ───────────────────────────────────────────────────


class TestAutoSplit:
    def test_auto_splits_across_multiple_vendors(self, db_session):
        user = _make_user(db_session)
        company = _make_company(db_session)
        site = _make_site(db_session, company)
        req = _make_requisition(db_session, user, site)
        # Require 150 units
        requirement = _make_requirement(db_session, req, qty=150)
        v1 = _make_vendor(db_session, "Vendor Alpha")
        v2 = _make_vendor(db_session, "Vendor Beta")
        # Each vendor has only 80 available — neither covers the full qty
        _make_offer(db_session, req, requirement, v1, qty=80, price=0.40)
        _make_offer(db_session, req, requirement, v2, qty=80, price=0.45)
        quote = _make_quote(db_session, req, site, user)
        plan = build_buy_plan(quote.id, db_session)
        # Should have 2 split lines
        assert len(plan.lines) == 2


# ── generate_ai_summary ───────────────────────────────────────────────


class TestGenerateAiSummary:
    def test_empty_plan_returns_empty_message(self, db_session):
        plan = MagicMock()
        plan.lines = []
        plan.ai_flags = []
        result = generate_ai_summary(plan)
        assert "Empty buy plan" in result

    def test_single_line_singular_grammar(self, db_session):
        user = _make_user(db_session)
        company = _make_company(db_session)
        site = _make_site(db_session, company)
        req = _make_requisition(db_session, user, site)
        requirement = _make_requirement(db_session, req, qty=10)
        vendor = _make_vendor(db_session)
        offer = _make_offer(db_session, req, requirement, vendor, qty=50)
        quote = _make_quote(db_session, req, site, user)
        plan = build_buy_plan(quote.id, db_session)
        summary = generate_ai_summary(plan)
        # Single line → "1 line"
        assert "1 line" in summary

    def test_summary_includes_flags_count(self, db_session):
        plan = MagicMock()
        line = MagicMock()
        line.offer = MagicMock()
        line.offer.vendor_name = "ACME"
        line.offer_id = 1
        line.margin_pct = 30.0
        plan.lines = [line]
        plan.ai_flags = [{"type": "stale_offer"}, {"type": "low_margin"}]
        summary = generate_ai_summary(plan)
        assert "flag" in summary


# ── generate_ai_flags: stale and low margin ───────────────────────────


class TestGenerateAiFlagsExtra:
    def test_stale_offer_flag(self, db_session):
        user = _make_user(db_session)
        company = _make_company(db_session)
        site = _make_site(db_session, company)
        req = _make_requisition(db_session, user, site)
        requirement = _make_requirement(db_session, req, qty=10)
        vendor = _make_vendor(db_session)
        # Create offer 400 days old (default threshold is 180d)
        offer = _make_offer(db_session, req, requirement, vendor, qty=50, age_days=400)
        quote = _make_quote(db_session, req, site, user)
        plan, line = _make_plan_with_line(db_session, quote, req, requirement, offer, margin=30.0)
        flags = generate_ai_flags(plan, db_session)
        stale = [f for f in flags if f["type"] == "stale_offer"]
        assert len(stale) >= 1

    def test_negative_margin_is_critical(self, db_session):
        user = _make_user(db_session)
        company = _make_company(db_session)
        site = _make_site(db_session, company)
        req = _make_requisition(db_session, user, site)
        requirement = _make_requirement(db_session, req, qty=10)
        vendor = _make_vendor(db_session)
        offer = _make_offer(db_session, req, requirement, vendor, qty=50)
        quote = _make_quote(db_session, req, site, user)
        plan, line = _make_plan_with_line(db_session, quote, req, requirement, offer, margin=-5.0)
        flags = generate_ai_flags(plan, db_session)
        low = [f for f in flags if f["type"] == "low_margin"]
        assert any(f["severity"] == "critical" for f in low)


# ── _check_better_offer ────────────────────────────────────────────────


class TestCheckBetterOffer:
    def test_no_flag_when_offer_has_no_price(self, db_session):
        line = MagicMock()
        line.requirement_id = 1
        line.id = 1
        selected = MagicMock()
        selected.unit_price = None
        flags = []
        _check_better_offer(line, selected, 5.0, flags, db_session)
        assert flags == []

    def test_flags_better_offer(self, db_session):
        user = _make_user(db_session)
        company = _make_company(db_session)
        site = _make_site(db_session, company)
        req = _make_requisition(db_session, user, site)
        requirement = _make_requirement(db_session, req, qty=100)
        vendor1 = _make_vendor(db_session, "Expensive Co")
        vendor2 = _make_vendor(db_session, "Cheap Co")
        # Selected offer at $1.00
        selected_offer = _make_offer(db_session, req, requirement, vendor1, qty=100, price=1.00)
        # Cheaper alternative at $0.80 (20% cheaper, threshold 5%)
        _make_offer(db_session, req, requirement, vendor2, qty=100, price=0.80)
        db_session.flush()

        line = MagicMock()
        line.requirement_id = requirement.id
        line.id = None

        flags = []
        _check_better_offer(line, selected_offer, 5.0, flags, db_session)
        assert any(f["type"] == "better_offer" for f in flags)


# ── _check_geo_mismatch ────────────────────────────────────────────────


class TestCheckGeoMismatch:
    def test_no_flag_when_no_vendor_card(self, db_session):
        # Offer with no vendor_card and no vendor_name → early return, no flag
        offer = MagicMock()
        offer.vendor_card = None
        offer.vendor_name = None
        line = MagicMock()
        line.id = 1
        flags = []
        _check_geo_mismatch(line, offer, "americas", flags, db_session)
        assert flags == []

    def test_flags_cross_continent_mismatch(self, db_session):
        # Vendor in China (apac), customer in americas
        vendor = _make_vendor(db_session, "Asian Vendor", country="CN")
        offer = MagicMock()
        offer.vendor_card = vendor
        offer.vendor_name = "Asian Vendor"
        line = MagicMock()
        line.id = 1
        flags = []
        _check_geo_mismatch(line, offer, "americas", flags, db_session)
        assert any(f["type"] == "geo_mismatch" for f in flags)


# ── _check_quantity_gaps ────────────────────────────────────────────────


class TestCheckQuantityGaps:
    def test_flags_quantity_gap(self, db_session):
        user = _make_user(db_session)
        company = _make_company(db_session)
        site = _make_site(db_session, company)
        req = _make_requisition(db_session, user, site)
        requirement = _make_requirement(db_session, req, qty=100)
        vendor = _make_vendor(db_session)
        offer = _make_offer(db_session, req, requirement, vendor, qty=200)
        quote = _make_quote(db_session, req, site, user)
        plan = BuyPlan(
            quote_id=quote.id,
            requisition_id=req.id,
            status=BuyPlanStatus.DRAFT.value,
            created_at=datetime.now(UTC),
        )
        db_session.add(plan)
        db_session.flush()
        # Only allocate 40 out of 100 needed
        line = BuyPlanLine(
            buy_plan_id=plan.id,
            requirement_id=requirement.id,
            offer_id=offer.id,
            quantity=40,
            status=BuyPlanLineStatus.AWAITING_PO.value,
        )
        db_session.add(line)
        db_session.flush()
        flags = []
        _check_quantity_gaps(plan, flags, db_session)
        assert any(f["type"] == "quantity_gap" for f in flags)
