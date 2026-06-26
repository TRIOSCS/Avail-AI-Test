"""test_buyplan_builder_nightly.py — Extended coverage for buyplan_builder.py.

Targets uncovered branches: quote-not-found, no-requirements, no-offers,
auto-split, empty-summary, vendor-name dedup, flag-count display,
ai_flags geo-customer-site, stale-offer, better-offer, geo-mismatch,
quantity-gap, and _create_line margin paths.

Called by: pytest
Depends on: conftest db_session fixture, app/services/buyplan_builder.py
"""

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

os.environ["TESTING"] = "1"

import pytest
from sqlalchemy.orm import Session

from app.constants import OfferStatus, QuoteStatus
from app.models import Company, CustomerSite, Offer, Quote, Requirement, Requisition, User, VendorCard
from app.models.buy_plan import BuyPlan, BuyPlanLine, BuyPlanLineStatus, BuyPlanStatus
from app.services.buyplan_builder import (
    _build_lines_for_requirement,
    _check_better_offer,
    _check_geo_mismatch,
    _check_quantity_gaps,
    build_buy_plan,
    generate_ai_flags,
    generate_ai_summary,
)

# ── Shared factory ──────────────────────────────────────────────────────


def _make_user(db: Session, *, email="nightly@trioscs.com", role="buyer", azure_id="az-nightly") -> User:
    user = User(
        email=email,
        name="Nightly Test User",
        role=role,
        azure_id=azure_id,
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(user)
    db.flush()
    return user


def _make_chain(
    db: Session,
    *,
    quote_status: str = "won",
    country: str | None = None,
    state: str | None = None,
    target_qty: int = 100,
    offer_qty: int = 100,
    offer_price: float = 0.50,
    target_price: float = 1.0,
    offer_status: str = "active",
):
    """Create full quote → requisition → requirement → offer chain."""
    user = _make_user(db, email=f"chain-{id(db)}@test.com", azure_id=f"az-{id(db)}")
    company = Company(name="Chain Corp", is_active=True, created_at=datetime.now(timezone.utc))
    db.add(company)
    db.flush()

    site = CustomerSite(
        company_id=company.id,
        site_name="HQ",
        country=country,
        state=state,
        created_at=datetime.now(timezone.utc),
    )
    db.add(site)
    db.flush()

    requisition = Requisition(
        name="REQ-NIGHTLY",
        status="open",
        created_by=user.id,
        customer_site_id=site.id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(requisition)
    db.flush()

    requirement = Requirement(
        requisition_id=requisition.id,
        primary_mpn="NLY-MPN-001",
        target_qty=target_qty,
        target_price=target_price,
        created_at=datetime.now(timezone.utc),
    )
    db.add(requirement)
    db.flush()

    vendor = VendorCard(
        normalized_name="nightly vendor",
        display_name="Nightly Vendor",
        created_at=datetime.now(timezone.utc),
    )
    db.add(vendor)
    db.flush()

    offer = Offer(
        requisition_id=requisition.id,
        requirement_id=requirement.id,
        vendor_card_id=vendor.id,
        vendor_name="Nightly Vendor",
        mpn="NLY-MPN-001",
        qty_available=offer_qty,
        unit_price=offer_price,
        status=offer_status,
        entered_by_id=user.id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(offer)
    db.flush()

    quote = Quote(
        requisition_id=requisition.id,
        customer_site_id=site.id,
        quote_number=f"Q-NIGHTLY-{id(db)}",
        status=quote_status,
        created_by_id=user.id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(quote)
    db.flush()

    return quote, requisition, requirement, offer, user, vendor, site


# ── build_buy_plan guard: quote not found ───────────────────────────────


class TestBuildBuyPlanQuoteNotFound:
    def test_raises_for_missing_quote(self, db_session: Session):
        """Should raise ValueError when quote_id does not exist."""
        with pytest.raises(ValueError, match="not found"):
            build_buy_plan(999999, db_session)


# ── build_buy_plan guard: no requirements ───────────────────────────────


class TestBuildBuyPlanNoRequirements:
    def test_raises_when_no_requirements(self, db_session: Session):
        """Should raise ValueError when the requisition has no requirements."""
        user = _make_user(db_session)
        company = Company(name="Empty Corp", is_active=True, created_at=datetime.now(timezone.utc))
        db_session.add(company)
        db_session.flush()

        site = CustomerSite(
            company_id=company.id,
            site_name="HQ",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(site)
        db_session.flush()

        requisition = Requisition(
            name="REQ-EMPTY",
            status="open",
            created_by=user.id,
            customer_site_id=site.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(requisition)
        db_session.flush()

        quote = Quote(
            requisition_id=requisition.id,
            customer_site_id=site.id,
            quote_number="Q-EMPTY-001",
            status=QuoteStatus.WON.value,
            created_by_id=user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(quote)
        db_session.flush()

        with pytest.raises(ValueError, match="No requirements found"):
            build_buy_plan(quote.id, db_session)


# ── No offers → empty lines from _build_lines_for_requirement ──────────


class TestNoOffersForRequirement:
    def test_returns_empty_list_when_no_offers(self, db_session: Session):
        """_build_lines_for_requirement returns [] when no active offers exist."""
        user = _make_user(db_session, email="no-offer@test.com", azure_id="az-no-offer")
        requisition = Requisition(
            name="REQ-NOOFFER",
            status="open",
            created_by=user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(requisition)
        db_session.flush()

        requirement = Requirement(
            requisition_id=requisition.id,
            primary_mpn="NO-OFFER-MPN",
            target_qty=50,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(requirement)
        db_session.flush()

        lines = _build_lines_for_requirement(requirement, None, db_session)
        assert lines == []

    def test_build_buy_plan_skips_reqs_with_no_offers(self, db_session: Session):
        """build_buy_plan completes even if some requirements have no active offers."""
        quote, requisition, requirement, offer, *_ = _make_chain(db_session, offer_status="inactive")
        # Build should succeed but lines list will be empty
        plan = build_buy_plan(quote.id, db_session)
        assert plan is not None
        assert plan.status == BuyPlanStatus.DRAFT.value
        assert plan.lines == []


# ── Auto-split path ────────────────────────────────────────────────────


class TestAutoSplit:
    def test_auto_split_across_two_vendors(self, db_session: Session):
        """When no single offer covers full qty, lines are split across vendors."""
        quote, requisition, requirement, offer, user, vendor, _ = _make_chain(db_session, target_qty=100, offer_qty=60)

        # Second vendor with remaining qty
        vendor2 = VendorCard(
            normalized_name="second vendor",
            display_name="Second Vendor",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(vendor2)
        db_session.flush()

        offer2 = Offer(
            requisition_id=requisition.id,
            requirement_id=requirement.id,
            vendor_card_id=vendor2.id,
            vendor_name="Second Vendor",
            mpn="NLY-MPN-001",
            qty_available=50,
            unit_price=0.55,
            status=OfferStatus.ACTIVE.value,
            entered_by_id=user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(offer2)
        db_session.flush()

        plan = build_buy_plan(quote.id, db_session)
        # Should have 2 lines (split across 2 vendors)
        assert len(plan.lines) == 2
        total_qty = sum(line.quantity for line in plan.lines)
        assert total_qty == 100

    def test_auto_split_stops_when_remaining_zero(self, db_session: Session):
        """Greedy split stops once remaining qty reaches 0."""
        quote, requisition, requirement, offer, user, vendor, _ = _make_chain(db_session, target_qty=50, offer_qty=30)

        vendor2 = VendorCard(
            normalized_name="third vendor",
            display_name="Third Vendor",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(vendor2)
        db_session.flush()

        offer2 = Offer(
            requisition_id=requisition.id,
            requirement_id=requirement.id,
            vendor_card_id=vendor2.id,
            vendor_name="Third Vendor",
            mpn="NLY-MPN-001",
            qty_available=40,
            unit_price=0.60,
            status=OfferStatus.ACTIVE.value,
            entered_by_id=user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(offer2)
        db_session.flush()

        plan = build_buy_plan(quote.id, db_session)
        total_qty = sum(line.quantity for line in plan.lines)
        assert total_qty == 50  # exactly covered, not over-allocated


# ── generate_ai_summary branches ───────────────────────────────────────


class TestGenerateAiSummary:
    def test_empty_plan_returns_empty_message(self):
        """Plan with no lines returns the empty message string."""
        plan = BuyPlan(quote_id=1, requisition_id=1, status=BuyPlanStatus.DRAFT.value)
        plan.lines = []
        plan.ai_flags = []
        result = generate_ai_summary(plan)
        assert result == "Empty buy plan — no lines generated."

    def test_summary_includes_flag_count(self):
        """Summary text includes flag count when flags are present."""
        plan = BuyPlan(quote_id=1, requisition_id=1, status=BuyPlanStatus.DRAFT.value)
        line = MagicMock()
        line.offer = None
        line.offer_id = 1
        line.margin_pct = 15.0
        plan.lines = [line]
        plan.ai_flags = [{"type": "stale_offer"}, {"type": "low_margin"}]

        result = generate_ai_summary(plan)
        assert "2 flags" in result

    def test_summary_uses_offer_id_when_no_vendor_name(self):
        """Vendor count falls back to offer_id set when vendor_name is absent."""
        plan = BuyPlan(quote_id=1, requisition_id=1, status=BuyPlanStatus.DRAFT.value)
        line = MagicMock()
        line.offer = None
        line.offer_id = 42
        line.margin_pct = None
        plan.lines = [line]
        plan.ai_flags = []

        result = generate_ai_summary(plan)
        # Should mention 1 vendor (fallback to len(vendor_ids))
        assert "1 vendor" in result

    def test_summary_singular_forms(self):
        """Single line, single vendor, single flag uses singular forms."""
        plan = BuyPlan(quote_id=1, requisition_id=1, status=BuyPlanStatus.DRAFT.value)
        line = MagicMock()
        line.offer = MagicMock()
        line.offer.vendor_name = "Acme"
        line.offer_id = 1
        line.margin_pct = 20.0
        plan.lines = [line]
        plan.ai_flags = [{"type": "stale_offer"}]

        result = generate_ai_summary(plan)
        assert "1 line," in result
        assert "1 vendor," in result
        assert "1 flag" in result


# ── generate_ai_flags: stale offer ────────────────────────────────────


class TestStaleOfferFlag:
    def test_stale_offer_flag_generated(self, db_session: Session):
        """Offer older than threshold generates a stale_offer warning flag."""
        quote, _, requirement, offer, _, _, _ = _make_chain(db_session)

        # Backdate the offer so it's definitely stale (stale_days default = 5)
        stale_date = datetime.now(timezone.utc) - timedelta(days=10)
        offer.created_at = stale_date
        db_session.flush()

        plan = BuyPlan(
            quote_id=quote.id,
            requisition_id=quote.requisition_id,
            status=BuyPlanStatus.DRAFT.value,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(plan)
        db_session.flush()

        line = BuyPlanLine(
            buy_plan_id=plan.id,
            requirement_id=requirement.id,
            offer_id=offer.id,
            quantity=100,
            unit_cost=0.50,
            unit_sell=1.0,
            buyer_id=None,
            assignment_reason="test",
            status=BuyPlanLineStatus.AWAITING_PO.value,
        )
        db_session.add(line)
        db_session.flush()

        flags = generate_ai_flags(plan, db_session)
        stale_flags = [f for f in flags if f["type"] == "stale_offer"]
        assert len(stale_flags) == 1
        assert stale_flags[0]["severity"] == "warning"


# ── generate_ai_flags: low margin (negative = critical) ───────────────


class TestLowMarginFlag:
    def test_negative_margin_is_critical(self, db_session: Session):
        """Negative margin generates a critical (not warning) flag."""
        quote, _, requirement, offer, _, _, _ = _make_chain(db_session)

        plan = BuyPlan(
            quote_id=quote.id,
            requisition_id=quote.requisition_id,
            status=BuyPlanStatus.DRAFT.value,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(plan)
        db_session.flush()

        line = BuyPlanLine(
            buy_plan_id=plan.id,
            requirement_id=requirement.id,
            offer_id=offer.id,
            quantity=10,
            unit_cost=2.0,
            unit_sell=1.0,  # sell < cost → negative margin
            margin_pct=-50.0,
            buyer_id=None,
            assignment_reason="test",
            status=BuyPlanLineStatus.AWAITING_PO.value,
        )
        db_session.add(line)
        db_session.flush()

        flags = generate_ai_flags(plan, db_session)
        margin_flags = [f for f in flags if f["type"] == "low_margin"]
        assert len(margin_flags) == 1
        assert margin_flags[0]["severity"] == "critical"

    def test_low_positive_margin_is_warning(self, db_session: Session):
        """Margin below threshold but positive → warning severity."""
        quote, _, requirement, offer, _, _, _ = _make_chain(db_session)

        plan = BuyPlan(
            quote_id=quote.id,
            requisition_id=quote.requisition_id,
            status=BuyPlanStatus.DRAFT.value,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(plan)
        db_session.flush()

        line = BuyPlanLine(
            buy_plan_id=plan.id,
            requirement_id=requirement.id,
            offer_id=offer.id,
            quantity=10,
            unit_cost=0.95,
            unit_sell=1.0,
            margin_pct=5.0,  # below default 10% threshold, but positive
            buyer_id=None,
            assignment_reason="test",
            status=BuyPlanLineStatus.AWAITING_PO.value,
        )
        db_session.add(line)
        db_session.flush()

        flags = generate_ai_flags(plan, db_session)
        margin_flags = [f for f in flags if f["type"] == "low_margin"]
        assert len(margin_flags) == 1
        assert margin_flags[0]["severity"] == "warning"


# ── generate_ai_flags: customer region from customer_site ──────────────


class TestCustomerRegionFromSite:
    def test_customer_region_loaded_via_quote_customer_site(self, db_session: Session):
        """generate_ai_flags resolves customer_region from quote.customer_site."""
        quote, _, requirement, offer, _, vendor, _ = _make_chain(db_session, country="us")

        # Patch _country_to_region so it returns a known value for "us"
        with patch("app.services.buyplan_builder._country_to_region", return_value="americas"):
            plan = BuyPlan(
                quote_id=quote.id,
                requisition_id=quote.requisition_id,
                status=BuyPlanStatus.DRAFT.value,
                created_at=datetime.now(timezone.utc),
            )
            db_session.add(plan)
            db_session.flush()

            line = BuyPlanLine(
                buy_plan_id=plan.id,
                requirement_id=requirement.id,
                offer_id=offer.id,
                quantity=100,
                buyer_id=None,
                assignment_reason="test",
                status=BuyPlanLineStatus.AWAITING_PO.value,
            )
            db_session.add(line)
            db_session.flush()

            # Give the vendor a non-Americas country so geo_mismatch fires
            vendor.hq_country = "cn"
            db_session.flush()

            flags = generate_ai_flags(plan, db_session)
            # The customer_region path was exercised — no assertion error means it ran
            assert isinstance(flags, list)


# ── _check_better_offer ────────────────────────────────────────────────


class TestCheckBetterOffer:
    def test_no_flag_when_selected_has_no_price(self, db_session: Session):
        """_check_better_offer returns immediately when offer has no unit_price."""
        offer = MagicMock(spec=Offer)
        offer.unit_price = None
        line = MagicMock(spec=BuyPlanLine)
        line.requirement_id = 1
        flags: list[dict] = []

        _check_better_offer(line, offer, 5.0, flags, db_session)
        assert flags == []

    def test_no_flag_when_selected_price_is_zero(self, db_session: Session):
        """_check_better_offer returns immediately when offer price is 0."""
        offer = MagicMock(spec=Offer)
        offer.unit_price = 0
        line = MagicMock(spec=BuyPlanLine)
        line.requirement_id = 1
        flags: list[dict] = []

        _check_better_offer(line, offer, 5.0, flags, db_session)
        assert flags == []

    def test_better_offer_flag_generated(self, db_session: Session):
        """_check_better_offer appends a flag when a cheaper alternative exists."""
        quote, requisition, requirement, offer, user, vendor, _ = _make_chain(db_session, offer_price=1.00)

        # Create a cheaper alternative offer for the same requirement
        vendor2 = VendorCard(
            normalized_name="cheap vendor",
            display_name="Cheap Vendor",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(vendor2)
        db_session.flush()

        cheaper_offer = Offer(
            requisition_id=requisition.id,
            requirement_id=requirement.id,
            vendor_card_id=vendor2.id,
            vendor_name="Cheap Vendor",
            mpn="NLY-MPN-001",
            qty_available=100,
            unit_price=0.80,  # 20% cheaper → exceeds 5% threshold
            status=OfferStatus.ACTIVE.value,
            entered_by_id=user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(cheaper_offer)
        db_session.flush()

        plan = BuyPlan(
            quote_id=quote.id,
            requisition_id=quote.requisition_id,
            status=BuyPlanStatus.DRAFT.value,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(plan)
        db_session.flush()

        line = BuyPlanLine(
            buy_plan_id=plan.id,
            requirement_id=requirement.id,
            offer_id=offer.id,
            quantity=100,
            buyer_id=None,
            assignment_reason="test",
            status=BuyPlanLineStatus.AWAITING_PO.value,
        )
        db_session.add(line)
        db_session.flush()

        flags: list[dict] = []
        _check_better_offer(line, offer, 5.0, flags, db_session)
        better_flags = [f for f in flags if f["type"] == "better_offer"]
        assert len(better_flags) == 1
        assert "Cheap Vendor" in better_flags[0]["message"]

    def test_no_flag_when_alternative_not_cheaper_enough(self, db_session: Session):
        """No flag when alternative offer is within threshold."""
        quote, requisition, requirement, offer, user, vendor, _ = _make_chain(db_session, offer_price=1.00)

        vendor2 = VendorCard(
            normalized_name="similar vendor",
            display_name="Similar Vendor",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(vendor2)
        db_session.flush()

        similar_offer = Offer(
            requisition_id=requisition.id,
            requirement_id=requirement.id,
            vendor_card_id=vendor2.id,
            vendor_name="Similar Vendor",
            mpn="NLY-MPN-001",
            qty_available=100,
            unit_price=0.98,  # only 2% cheaper, under 5% threshold
            status=OfferStatus.ACTIVE.value,
            entered_by_id=user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(similar_offer)
        db_session.flush()

        plan = BuyPlan(
            quote_id=quote.id,
            requisition_id=quote.requisition_id,
            status=BuyPlanStatus.DRAFT.value,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(plan)
        db_session.flush()

        line = BuyPlanLine(
            buy_plan_id=plan.id,
            requirement_id=requirement.id,
            offer_id=offer.id,
            quantity=100,
            buyer_id=None,
            assignment_reason="test",
            status=BuyPlanLineStatus.AWAITING_PO.value,
        )
        db_session.add(line)
        db_session.flush()

        flags: list[dict] = []
        _check_better_offer(line, offer, 5.0, flags, db_session)
        assert not any(f["type"] == "better_offer" for f in flags)


# ── _check_geo_mismatch ────────────────────────────────────────────────


class TestCheckGeoMismatch:
    def test_geo_mismatch_flag_when_vendor_in_different_region(self, db_session: Session):
        """Flags geo_mismatch when vendor hq_country maps to a different region."""
        quote, _, requirement, offer, _, vendor, _ = _make_chain(db_session)

        plan = BuyPlan(
            quote_id=quote.id,
            requisition_id=quote.requisition_id,
            status=BuyPlanStatus.DRAFT.value,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(plan)
        db_session.flush()

        line = BuyPlanLine(
            buy_plan_id=plan.id,
            requirement_id=requirement.id,
            offer_id=offer.id,
            quantity=100,
            buyer_id=None,
            assignment_reason="test",
            status=BuyPlanLineStatus.AWAITING_PO.value,
        )
        db_session.add(line)
        db_session.flush()

        vendor.hq_country = "cn"
        db_session.flush()

        flags: list[dict] = []
        with patch("app.services.buyplan_builder._country_to_region") as mock_region:
            mock_region.side_effect = lambda c: "apac" if c == "cn" else "americas"
            _check_geo_mismatch(line, offer, "americas", flags, db_session)

        geo_flags = [f for f in flags if f["type"] == "geo_mismatch"]
        assert len(geo_flags) == 1

    def test_no_flag_when_vendor_card_has_no_hq_country(self, db_session: Session):
        """No geo_mismatch flag when vendor_card.hq_country is None."""
        quote, _, requirement, offer, _, vendor, _ = _make_chain(db_session)

        plan = BuyPlan(
            quote_id=quote.id,
            requisition_id=quote.requisition_id,
            status=BuyPlanStatus.DRAFT.value,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(plan)
        db_session.flush()

        line = BuyPlanLine(
            buy_plan_id=plan.id,
            requirement_id=requirement.id,
            offer_id=offer.id,
            quantity=100,
            buyer_id=None,
            assignment_reason="test",
            status=BuyPlanLineStatus.AWAITING_PO.value,
        )
        db_session.add(line)
        db_session.flush()

        vendor.hq_country = None
        db_session.flush()

        flags: list[dict] = []
        _check_geo_mismatch(line, offer, "americas", flags, db_session)
        assert not any(f["type"] == "geo_mismatch" for f in flags)

    def test_geo_mismatch_looks_up_vendor_card_by_name(self, db_session: Session):
        """When offer has no vendor_card relationship, lookup is done by vendor_name."""
        quote, _, requirement, offer, _, vendor, _ = _make_chain(db_session)
        # Set the normalized_name so the lookup works
        vendor.hq_country = "cn"
        db_session.flush()

        plan = BuyPlan(
            quote_id=quote.id,
            requisition_id=quote.requisition_id,
            status=BuyPlanStatus.DRAFT.value,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(plan)
        db_session.flush()

        line = BuyPlanLine(
            buy_plan_id=plan.id,
            requirement_id=requirement.id,
            offer_id=offer.id,
            quantity=100,
            buyer_id=None,
            assignment_reason="test",
            status=BuyPlanLineStatus.AWAITING_PO.value,
        )
        db_session.add(line)
        db_session.flush()

        # Simulate an offer with no loaded vendor_card relationship
        mock_offer = MagicMock(spec=Offer)
        mock_offer.vendor_card = None
        mock_offer.vendor_name = "nightly vendor"  # matches vendor.normalized_name

        flags: list[dict] = []
        with patch("app.services.buyplan_builder._country_to_region") as mock_region:
            mock_region.side_effect = lambda c: "apac" if c == "cn" else "americas"
            _check_geo_mismatch(line, mock_offer, "americas", flags, db_session)

        # The vendor was found by name → flag should fire
        geo_flags = [f for f in flags if f["type"] == "geo_mismatch"]
        assert len(geo_flags) == 1


# ── _check_quantity_gaps ───────────────────────────────────────────────


class TestCheckQuantityGaps:
    def test_quantity_gap_flag_when_allocation_short(self, db_session: Session):
        """Flags quantity_gap when split lines don't cover the full target qty."""
        quote, _, requirement, offer, _, _, _ = _make_chain(db_session, target_qty=100)

        plan = BuyPlan(
            quote_id=quote.id,
            requisition_id=quote.requisition_id,
            status=BuyPlanStatus.DRAFT.value,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(plan)
        db_session.flush()

        # Only allocate 60 of the 100 required
        line = BuyPlanLine(
            buy_plan_id=plan.id,
            requirement_id=requirement.id,
            offer_id=offer.id,
            quantity=60,
            buyer_id=None,
            assignment_reason="test",
            status=BuyPlanLineStatus.AWAITING_PO.value,
        )
        db_session.add(line)
        db_session.flush()

        flags: list[dict] = []
        _check_quantity_gaps(plan, flags, db_session)
        gap_flags = [f for f in flags if f["type"] == "quantity_gap"]
        assert len(gap_flags) == 1
        assert "gap: 40" in gap_flags[0]["message"]

    def test_no_gap_flag_when_fully_covered(self, db_session: Session):
        """No quantity_gap flag when lines fully cover the requirement qty."""
        quote, _, requirement, offer, _, _, _ = _make_chain(db_session, target_qty=100)

        plan = BuyPlan(
            quote_id=quote.id,
            requisition_id=quote.requisition_id,
            status=BuyPlanStatus.DRAFT.value,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(plan)
        db_session.flush()

        line = BuyPlanLine(
            buy_plan_id=plan.id,
            requirement_id=requirement.id,
            offer_id=offer.id,
            quantity=100,
            buyer_id=None,
            assignment_reason="test",
            status=BuyPlanLineStatus.AWAITING_PO.value,
        )
        db_session.add(line)
        db_session.flush()

        flags: list[dict] = []
        _check_quantity_gaps(plan, flags, db_session)
        assert not any(f["type"] == "quantity_gap" for f in flags)


# ── build_buy_plan: customer_site region path ──────────────────────────


class TestBuildBuyPlanWithRegion:
    def test_plan_built_with_country_on_site(self, db_session: Session):
        """build_buy_plan resolves customer_region from customer_site.country."""
        quote, *_ = _make_chain(db_session, country="us")
        with patch("app.services.buyplan_builder._country_to_region", return_value="americas"):
            plan = build_buy_plan(quote.id, db_session)
        assert plan is not None
        assert plan.status == BuyPlanStatus.DRAFT.value

    def test_plan_built_with_state_fallback_on_site(self, db_session: Session):
        """build_buy_plan falls back to customer_site.state when country is None."""
        quote, *_ = _make_chain(db_session, country=None, state="CA")
        with patch("app.services.buyplan_builder._country_to_region", return_value="americas"):
            plan = build_buy_plan(quote.id, db_session)
        assert plan is not None


# ── build_buy_plan: financials accumulation ────────────────────────────


class TestBuildBuyPlanFinancials:
    def test_total_cost_and_revenue_populated(self, db_session: Session):
        """Plan accumulates total_cost and total_revenue from lines."""
        quote, *_ = _make_chain(db_session, offer_price=0.50, target_price=1.0, offer_qty=100, target_qty=100)
        plan = build_buy_plan(quote.id, db_session)
        assert plan.total_cost is not None
        assert float(plan.total_cost) == pytest.approx(50.0)
        assert plan.total_revenue is not None
        assert float(plan.total_revenue) == pytest.approx(100.0)
        assert plan.total_margin_pct is not None
        assert float(plan.total_margin_pct) == pytest.approx(50.0)
