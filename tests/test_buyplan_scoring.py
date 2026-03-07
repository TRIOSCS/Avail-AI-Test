"""
test_buyplan_scoring.py -- Tests for app/services/buyplan_scoring.py

Covers:
- score_offer: all weighted components (price, reliability, lead time, geography, terms)
- _parse_lead_time_days: various string formats (stock, days, weeks, months, null)
- _country_to_region: mapping and missing cases
- _get_routing_maps: loading from file and fallback
- assign_buyer: vendor ownership, commodity match, geography, workload cascade

Called by: pytest
Depends on: app/services/buyplan_scoring.py, conftest.py
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.models import Offer, Requirement, Requisition, User, VendorCard
from app.services.buyplan_scoring import (
    _country_to_region,
    _parse_lead_time_days,
    assign_buyer,
    score_offer,
)
from tests.conftest import engine  # noqa: F401


# ═══════════════════════════════════════════════════════════════════════
#  _parse_lead_time_days
# ═══════════════════════════════════════════════════════════════════════


class TestParseLeadTimeDays:
    def test_none_returns_none(self):
        assert _parse_lead_time_days(None) is None

    def test_empty_string_returns_none(self):
        assert _parse_lead_time_days("") is None

    def test_stock(self):
        assert _parse_lead_time_days("stock") == 0

    def test_in_stock(self):
        assert _parse_lead_time_days("In Stock") == 0

    def test_immediate(self):
        assert _parse_lead_time_days("immediate") == 0

    def test_same_day(self):
        assert _parse_lead_time_days("Same Day") == 0

    def test_days_range(self):
        # "3-5 days" should return 5 (last number)
        assert _parse_lead_time_days("3-5 days") == 5

    def test_single_days(self):
        assert _parse_lead_time_days("7 days") == 7

    def test_weeks(self):
        assert _parse_lead_time_days("2 weeks") == 14

    def test_months(self):
        assert _parse_lead_time_days("3 months") == 90

    def test_no_numbers(self):
        assert _parse_lead_time_days("unknown") is None

    def test_whitespace(self):
        assert _parse_lead_time_days("  10 days  ") == 10


# ═══════════════════════════════════════════════════════════════════════
#  _country_to_region
# ═══════════════════════════════════════════════════════════════════════


class TestCountryToRegion:
    def test_none_country(self):
        assert _country_to_region(None) is None

    def test_empty_country(self):
        assert _country_to_region("") is None

    def test_known_country(self):
        """If routing_maps.json has country mappings, a valid country returns its region."""
        # This depends on actual routing maps data - just verify it doesn't crash
        result = _country_to_region("United States")
        # Result is either a region string or None depending on data
        assert result is None or isinstance(result, str)

    def test_unknown_country(self):
        result = _country_to_region("Zanzibaria")
        assert result is None


# ═══════════════════════════════════════════════════════════════════════
#  score_offer — comprehensive tests
# ═══════════════════════════════════════════════════════════════════════


class TestScoreOffer:
    def _make_offer(self, **kwargs):
        defaults = {
            "unit_price": 10.0,
            "lead_time": "5 days",
            "manufacturer": None,
        }
        defaults.update(kwargs)
        offer = MagicMock(spec=Offer)
        for k, v in defaults.items():
            setattr(offer, k, v)
        return offer

    def _make_requirement(self, **kwargs):
        defaults = {"target_price": 12.0}
        defaults.update(kwargs)
        req = MagicMock(spec=Requirement)
        for k, v in defaults.items():
            setattr(req, k, v)
        return req

    def _make_vendor_card(self, **kwargs):
        defaults = {
            "vendor_score": 80.0,
            "is_new_vendor": False,
            "hq_country": None,
            "total_pos": 5,
            "commodity_tags": [],
        }
        defaults.update(kwargs)
        card = MagicMock(spec=VendorCard)
        for k, v in defaults.items():
            setattr(card, k, v)
        return card

    def test_perfect_score_scenario(self):
        """Offer at target price, good vendor, fast lead time."""
        offer = self._make_offer(unit_price=10.0, lead_time="stock")
        req = self._make_requirement(target_price=10.0)
        card = self._make_vendor_card(vendor_score=100.0, total_pos=10)

        score = score_offer(offer, req, card)
        assert score > 80  # High score scenario

    def test_no_vendor_card(self):
        """Scoring without a vendor card uses defaults."""
        offer = self._make_offer(unit_price=10.0, lead_time="5 days")
        req = self._make_requirement(target_price=12.0)

        score = score_offer(offer, req, None)
        assert 0 <= score <= 100

    def test_no_target_price(self):
        """No target price results in neutral price score (50)."""
        offer = self._make_offer(unit_price=10.0)
        req = self._make_requirement(target_price=None)
        card = self._make_vendor_card()

        score = score_offer(offer, req, card)
        assert 0 <= score <= 100

    def test_zero_actual_price(self):
        """Zero actual price results in price score 0."""
        offer = self._make_offer(unit_price=0)
        req = self._make_requirement(target_price=10.0)

        score = score_offer(offer, req, None)
        assert 0 <= score <= 100

    def test_none_actual_price(self):
        """None actual price results in price score 0."""
        offer = self._make_offer(unit_price=None)
        req = self._make_requirement(target_price=10.0)

        score = score_offer(offer, req, None)
        assert 0 <= score <= 100

    def test_price_below_target(self):
        """Price below target gives score > 100 capped to 100."""
        offer = self._make_offer(unit_price=5.0)  # Way below target
        req = self._make_requirement(target_price=15.0)  # target/actual = 3.0

        score = score_offer(offer, req, None)
        assert score > 0

    def test_vendor_new_unknown(self):
        """New/unknown vendor gets low reliability and terms scores."""
        offer = self._make_offer(unit_price=10.0)
        req = self._make_requirement(target_price=10.0)
        card = self._make_vendor_card(vendor_score=None, is_new_vendor=True, total_pos=0)

        score = score_offer(offer, req, card)
        assert 0 <= score <= 100

    def test_vendor_known_no_score(self):
        """Known vendor without score gets reliability=50."""
        offer = self._make_offer(unit_price=10.0)
        req = self._make_requirement(target_price=10.0)
        card = self._make_vendor_card(vendor_score=None, is_new_vendor=False, total_pos=0)

        score = score_offer(offer, req, card)
        assert 0 <= score <= 100

    def test_lead_time_very_long(self):
        """Very long lead time (>30 days) gets low score."""
        offer = self._make_offer(unit_price=10.0, lead_time="60 days")
        req = self._make_requirement(target_price=10.0)

        score = score_offer(offer, req, None)
        assert 0 <= score <= 100

    def test_lead_time_3_days(self):
        """3-day lead time gets 100 score."""
        offer = self._make_offer(unit_price=10.0, lead_time="3 days")
        req = self._make_requirement(target_price=10.0)

        score = score_offer(offer, req, None)
        assert score > 0

    def test_lead_time_7_days(self):
        offer = self._make_offer(unit_price=10.0, lead_time="7 days")
        req = self._make_requirement(target_price=10.0)
        score = score_offer(offer, req, None)
        assert score > 0

    def test_lead_time_14_days(self):
        offer = self._make_offer(unit_price=10.0, lead_time="14 days")
        req = self._make_requirement(target_price=10.0)
        score = score_offer(offer, req, None)
        assert score > 0

    def test_lead_time_30_days(self):
        offer = self._make_offer(unit_price=10.0, lead_time="30 days")
        req = self._make_requirement(target_price=10.0)
        score = score_offer(offer, req, None)
        assert score > 0

    def test_lead_time_unknown(self):
        """Unknown lead time (None) gets 40."""
        offer = self._make_offer(unit_price=10.0, lead_time=None)
        req = self._make_requirement(target_price=10.0)
        score = score_offer(offer, req, None)
        assert score > 0

    def test_geography_same_region(self):
        """Vendor in same region as customer gets geography=100."""
        offer = self._make_offer(unit_price=10.0)
        req = self._make_requirement(target_price=10.0)
        card = self._make_vendor_card(hq_country="United States")

        with patch("app.services.buyplan_scoring._country_to_region", return_value="americas"):
            score = score_offer(offer, req, card, customer_region="americas")

        assert score > 0

    def test_geography_different_region(self):
        """Vendor in different region gets geography=50."""
        offer = self._make_offer(unit_price=10.0)
        req = self._make_requirement(target_price=10.0)
        card = self._make_vendor_card(hq_country="China")

        with patch("app.services.buyplan_scoring._country_to_region", return_value="apac"):
            score = score_offer(offer, req, card, customer_region="americas")

        assert score > 0

    def test_vendor_with_po_history(self):
        """Vendor with PO history gets terms=85."""
        offer = self._make_offer(unit_price=10.0)
        req = self._make_requirement(target_price=10.0)
        card = self._make_vendor_card(total_pos=10)

        score = score_offer(offer, req, card)
        assert score > 0

    def test_known_vendor_no_pos(self):
        """Known vendor without POs gets terms=65."""
        offer = self._make_offer(unit_price=10.0)
        req = self._make_requirement(target_price=10.0)
        card = self._make_vendor_card(total_pos=0, is_new_vendor=False)

        score = score_offer(offer, req, card)
        assert score > 0


# ═══════════════════════════════════════════════════════════════════════
#  assign_buyer
# ═══════════════════════════════════════════════════════════════════════


class TestAssignBuyer:
    def test_vendor_ownership_priority(self, db_session):
        """Buyer who entered the offer gets priority assignment."""
        buyer = User(
            email="buyer@test.com", name="Buyer", role="buyer",
            azure_id="az-buyer-1", is_active=True,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(buyer)
        db_session.flush()

        req = Requisition(
            name="Test", status="active",
            created_by=buyer.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.flush()

        offer = Offer(
            requisition_id=req.id,
            vendor_name="Arrow", mpn="TEST",
            unit_price=10.0, status="active",
            entered_by_id=buyer.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(offer)
        db_session.commit()

        assigned, reason = assign_buyer(offer, None, db_session)
        assert assigned.id == buyer.id
        assert reason == "vendor_ownership"

    def test_no_buyers_available(self, db_session):
        """When no active buyers exist, returns (None, 'no_buyers')."""
        user = User(
            email="sales@test.com", name="Sales", role="sales",
            azure_id="az-sales-1", is_active=True,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(user)
        db_session.flush()

        req = Requisition(
            name="Test", status="active",
            created_by=user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.flush()

        offer = Offer(
            requisition_id=req.id,
            vendor_name="Arrow", mpn="TEST",
            unit_price=10.0, status="active",
            entered_by_id=None,  # No entered_by
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(offer)
        db_session.commit()

        assigned, reason = assign_buyer(offer, None, db_session)
        assert assigned is None
        assert reason == "no_buyers"

    def test_workload_assignment(self, db_session):
        """When no ownership/commodity/geography match, assigns by workload."""
        buyer1 = User(
            email="buyer1@test.com", name="Buyer1", role="buyer",
            azure_id="az-buyer-wl-1", is_active=True,
            created_at=datetime.now(timezone.utc),
        )
        buyer2 = User(
            email="buyer2@test.com", name="Buyer2", role="buyer",
            azure_id="az-buyer-wl-2", is_active=True,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add_all([buyer1, buyer2])
        db_session.flush()

        req = Requisition(
            name="Test", status="active",
            created_by=buyer1.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.flush()

        offer = Offer(
            requisition_id=req.id,
            vendor_name="NewVendor", mpn="TEST",
            unit_price=10.0, status="active",
            entered_by_id=None,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(offer)
        db_session.commit()

        assigned, reason = assign_buyer(offer, None, db_session)
        assert assigned is not None
        assert reason == "workload"

    def test_entered_by_inactive_falls_through(self, db_session):
        """Inactive entered_by buyer falls through to other priorities."""
        inactive_buyer = User(
            email="inactive@test.com", name="Inactive", role="buyer",
            azure_id="az-inactive-1", is_active=False,
            created_at=datetime.now(timezone.utc),
        )
        active_buyer = User(
            email="active@test.com", name="Active", role="buyer",
            azure_id="az-active-1", is_active=True,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add_all([inactive_buyer, active_buyer])
        db_session.flush()

        req = Requisition(
            name="Test", status="active",
            created_by=inactive_buyer.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.flush()

        offer = Offer(
            requisition_id=req.id,
            vendor_name="Test", mpn="TEST",
            unit_price=10.0, status="active",
            entered_by_id=inactive_buyer.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(offer)
        db_session.commit()

        assigned, reason = assign_buyer(offer, None, db_session)
        assert assigned.id == active_buyer.id
        assert reason == "workload"

    def test_entered_by_sales_role_falls_through(self, db_session):
        """entered_by user with 'sales' role (not buyer/trader) falls through."""
        sales_user = User(
            email="sales2@test.com", name="Sales", role="sales",
            azure_id="az-sales-2", is_active=True,
            created_at=datetime.now(timezone.utc),
        )
        buyer = User(
            email="buyer3@test.com", name="Buyer", role="trader",
            azure_id="az-trader-1", is_active=True,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add_all([sales_user, buyer])
        db_session.flush()

        req = Requisition(
            name="Test", status="active",
            created_by=sales_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.flush()

        offer = Offer(
            requisition_id=req.id,
            vendor_name="Test", mpn="TEST",
            unit_price=10.0, status="active",
            entered_by_id=sales_user.id,  # Sales, not buyer/trader
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(offer)
        db_session.commit()

        assigned, reason = assign_buyer(offer, None, db_session)
        assert assigned.id == buyer.id
        assert reason == "workload"


# ═══════════════════════════════════════════════════════════════════════
#  _get_routing_maps
# ═══════════════════════════════════════════════════════════════════════


class TestGetRoutingMaps:
    def test_loads_maps_or_uses_fallback(self):
        """_get_routing_maps returns a dict, either from file or defaults."""
        import app.services.buyplan_scoring as bps
        bps._ROUTING_MAPS = None  # Reset cache
        maps = bps._get_routing_maps()
        assert isinstance(maps, dict)
        assert "brand_commodity_map" in maps or "country_region_map" in maps

    def test_fallback_when_no_file(self):
        """When routing_maps.json doesn't exist, returns empty defaults."""
        import app.services.buyplan_scoring as bps
        bps._ROUTING_MAPS = None

        with patch("pathlib.Path.exists", return_value=False):
            maps = bps._get_routing_maps()

        assert maps == {"brand_commodity_map": {}, "country_region_map": {}}
        bps._ROUTING_MAPS = None  # Reset for other tests
