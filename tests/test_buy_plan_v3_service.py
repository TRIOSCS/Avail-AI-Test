"""
test_buy_plan_v3_service.py — Tests for Buy Plan V3 AI Build Logic

Covers: score_offer, assign_buyer, build_buy_plan, generate_ai_summary,
        generate_ai_flags, _check_quantity_gaps, _parse_lead_time_days,
        _country_to_region, _get_routing_maps, _create_line, _build_lines_for_requirement

Called by: pytest
Depends on: conftest fixtures, buy_plan_v3_service module
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.models.buy_plan import BuyPlanLine, BuyPlanLineStatus, BuyPlanStatus, BuyPlanV3
from app.services.buy_plan_v3_service import (
    _check_quantity_gaps,
    _country_to_region,
    _create_line,
    _get_routing_maps,
    _parse_lead_time_days,
    assign_buyer,
    build_buy_plan,
    generate_ai_flags,
    generate_ai_summary,
    score_offer,
)


# ── Helpers ────────────────────────────────────────────────────────────


def _make_offer(**kw):
    defaults = {
        "id": 1, "unit_price": 0.50, "lead_time": "5 days",
        "qty_available": 1000, "status": "active", "manufacturer": None,
        "entered_by_id": None, "vendor_card": None, "vendor_name": "Acme",
        "requirement_id": 1, "created_at": datetime.now(timezone.utc),
    }
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def _make_requirement(**kw):
    defaults = {"id": 1, "target_qty": 1000, "target_price": 1.00, "requisition_id": 10}
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def _make_vendor_card(**kw):
    defaults = {
        "vendor_score": 75, "is_new_vendor": False, "hq_country": "united states",
        "total_pos": 10, "commodity_tags": ["semiconductors"],
    }
    defaults.update(kw)
    return SimpleNamespace(**defaults)


# ── _parse_lead_time_days ──────────────────────────────────────────────


class TestParseLeadTimeDays:
    def test_none_returns_none(self):
        assert _parse_lead_time_days(None) is None

    def test_empty_returns_none(self):
        assert _parse_lead_time_days("") is None

    def test_stock_returns_zero(self):
        for val in ("stock", "in stock", "immediate", "same day"):
            assert _parse_lead_time_days(val) == 0

    def test_days_extracts_last_number(self):
        assert _parse_lead_time_days("3-5 days") == 5

    def test_weeks_multiplied(self):
        assert _parse_lead_time_days("2 weeks") == 14

    def test_months_multiplied(self):
        assert _parse_lead_time_days("1 month") == 30

    def test_no_numbers_returns_none(self):
        assert _parse_lead_time_days("unknown") is None

    def test_single_number(self):
        assert _parse_lead_time_days("7 days") == 7


# ── _country_to_region / _get_routing_maps ────────────────────────────


class TestRoutingMaps:
    def setup_method(self):
        # Reset the cached routing maps so each test is clean
        import app.services.buy_plan_v3_service as mod
        mod._ROUTING_MAPS = None

    def test_get_routing_maps_loads_file(self):
        maps = _get_routing_maps()
        assert "brand_commodity_map" in maps
        assert "country_region_map" in maps

    def test_get_routing_maps_cached(self):
        maps1 = _get_routing_maps()
        maps2 = _get_routing_maps()
        assert maps1 is maps2

    def test_country_to_region_none(self):
        assert _country_to_region(None) is None

    def test_country_to_region_empty(self):
        assert _country_to_region("") is None

    def test_missing_file_returns_defaults(self):
        with patch("pathlib.Path.exists", return_value=False):
            import app.services.buy_plan_v3_service as mod
            mod._ROUTING_MAPS = None
            maps = _get_routing_maps()
            assert maps == {"brand_commodity_map": {}, "country_region_map": {}}
            mod._ROUTING_MAPS = None  # reset


# ── score_offer ───────────────────────────────────────────────────────


class TestScoreOffer:
    def test_full_info_returns_nonzero(self):
        offer = _make_offer(unit_price=0.80)
        req = _make_requirement(target_price=1.00)
        vc = _make_vendor_card()
        score = score_offer(offer, req, vc, "americas")
        assert 0 < score <= 100

    def test_no_price_info(self):
        offer = _make_offer(unit_price=None)
        req = _make_requirement(target_price=None)
        score = score_offer(offer, req, None)
        assert score >= 0

    def test_price_no_target(self):
        offer = _make_offer(unit_price=1.00)
        req = _make_requirement(target_price=None)
        score = score_offer(offer, req, None)
        assert score > 0

    def test_price_zero_actual(self):
        offer = _make_offer(unit_price=0)
        req = _make_requirement(target_price=1.00)
        score = score_offer(offer, req, None)
        assert score >= 0

    def test_vendor_card_known_no_score(self):
        vc = _make_vendor_card(vendor_score=None, is_new_vendor=False)
        offer = _make_offer(unit_price=1.00)
        req = _make_requirement(target_price=1.00)
        score = score_offer(offer, req, vc)
        assert score > 0

    def test_no_vendor_card(self):
        offer = _make_offer(unit_price=1.00)
        req = _make_requirement(target_price=1.00)
        score = score_offer(offer, req, None)
        assert score > 0

    def test_lead_time_brackets(self):
        req = _make_requirement(target_price=1.00)
        vc = _make_vendor_card()
        for lt, expected_min in [("stock", 80), ("5 days", 60), ("10 days", 50), ("20 days", 30), ("60 days", 0)]:
            offer = _make_offer(unit_price=1.00, lead_time=lt)
            score = score_offer(offer, req, vc, "americas")
            assert score >= expected_min, f"lead_time={lt} score={score}"

    def test_geography_match(self):
        req = _make_requirement(target_price=1.00)
        vc = _make_vendor_card(hq_country="united states")
        offer = _make_offer(unit_price=1.00)
        score_match = score_offer(offer, req, vc, "americas")
        score_no = score_offer(offer, req, vc, "apac")
        # Same region should score higher
        assert score_match >= score_no

    def test_geography_unknown(self):
        req = _make_requirement(target_price=1.00)
        vc = _make_vendor_card(hq_country=None)
        offer = _make_offer(unit_price=1.00)
        score = score_offer(offer, req, vc, "americas")
        assert score > 0

    def test_terms_established_vs_new(self):
        req = _make_requirement(target_price=1.00)
        vc_established = _make_vendor_card(total_pos=10)
        vc_known = _make_vendor_card(total_pos=0, is_new_vendor=False)
        vc_new = _make_vendor_card(total_pos=0, is_new_vendor=True)
        offer = _make_offer(unit_price=1.00)
        s1 = score_offer(offer, req, vc_established, "americas")
        s2 = score_offer(offer, req, vc_known, "americas")
        s3 = score_offer(offer, req, vc_new, "americas")
        assert s1 >= s2 >= s3

    def test_ratio_capped_at_100(self):
        """When target >> actual, price score caps at 100."""
        offer = _make_offer(unit_price=0.01)
        req = _make_requirement(target_price=100.00)
        score = score_offer(offer, req, None)
        assert score <= 100

    def test_vendor_score_capped_at_100(self):
        vc = _make_vendor_card(vendor_score=200)
        offer = _make_offer(unit_price=1.00)
        req = _make_requirement(target_price=1.00)
        score = score_offer(offer, req, vc)
        assert score <= 100


# ── assign_buyer ──────────────────────────────────────────────────────


class TestAssignBuyer:
    def test_vendor_ownership(self, db_session):
        from app.models import User
        buyer = User(email="b@test.com", name="B", role="buyer", azure_id="az1", is_active=True)
        db_session.add(buyer)
        db_session.commit()
        offer = _make_offer(entered_by_id=buyer.id)
        user, reason = assign_buyer(offer, None, db_session)
        assert user.id == buyer.id
        assert reason == "vendor_ownership"

    def test_no_buyers(self, db_session):
        offer = _make_offer(entered_by_id=None)
        user, reason = assign_buyer(offer, None, db_session)
        assert user is None
        assert reason == "no_buyers"

    def test_workload_assignment(self, db_session):
        from app.models import User
        b1 = User(email="b1@test.com", name="B1", role="buyer", azure_id="az2", is_active=True)
        b2 = User(email="b2@test.com", name="B2", role="trader", azure_id="az3", is_active=True)
        db_session.add_all([b1, b2])
        db_session.commit()
        offer = _make_offer(entered_by_id=None)
        user, reason = assign_buyer(offer, None, db_session)
        assert reason == "workload"
        assert user.id in (b1.id, b2.id)

    def test_entered_by_inactive_falls_through(self, db_session):
        from app.models import User
        inactive = User(email="gone@test.com", name="Gone", role="buyer", azure_id="az4", is_active=False)
        active = User(email="here@test.com", name="Here", role="buyer", azure_id="az5", is_active=True)
        db_session.add_all([inactive, active])
        db_session.commit()
        offer = _make_offer(entered_by_id=inactive.id)
        user, reason = assign_buyer(offer, None, db_session)
        assert user.id == active.id
        assert reason == "workload"

    def test_entered_by_sales_role_falls_through(self, db_session):
        from app.models import User
        sales = User(email="s@test.com", name="S", role="sales", azure_id="az6", is_active=True)
        buyer = User(email="buy@test.com", name="Buy", role="buyer", azure_id="az7", is_active=True)
        db_session.add_all([sales, buyer])
        db_session.commit()
        offer = _make_offer(entered_by_id=sales.id)
        user, reason = assign_buyer(offer, None, db_session)
        assert user.id == buyer.id
        assert reason == "workload"

    def test_commodity_tags_and_manufacturer(self, db_session):
        """Exercises the commodity/brand matching code path (currently falls through to workload)."""
        from app.models import User
        buyer = User(email="comm@test.com", name="Comm", role="buyer", azure_id="az8", is_active=True)
        db_session.add(buyer)
        db_session.commit()
        vc = _make_vendor_card(commodity_tags=["semiconductors"], hq_country="united states")
        offer = _make_offer(entered_by_id=None, manufacturer="Texas Instruments")
        user, reason = assign_buyer(offer, vc, db_session)
        assert reason == "workload"

    def test_geography_path(self, db_session):
        """Exercises the geography matching code path (currently falls through to workload)."""
        from app.models import User
        buyer = User(email="geo@test.com", name="Geo", role="buyer", azure_id="az9", is_active=True)
        db_session.add(buyer)
        db_session.commit()
        vc = _make_vendor_card(commodity_tags=None, hq_country="united states")
        offer = _make_offer(entered_by_id=None, manufacturer=None)
        user, reason = assign_buyer(offer, vc, db_session)
        assert reason == "workload"


# ── _create_line ──────────────────────────────────────────────────────


class TestCreateLine:
    def test_creates_with_margin(self):
        req = _make_requirement(target_price=1.00)
        offer = _make_offer(unit_price=0.50)
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
        req = _make_requirement(target_price=1.00)
        offer = _make_offer(unit_price=0.50)
        line = _create_line(req, offer, 100, 75.0, None, "no_buyers")
        assert line.buyer_id is None

    def test_no_prices(self):
        req = _make_requirement(target_price=None)
        offer = _make_offer(unit_price=None)
        line = _create_line(req, offer, 100, 50.0, None, "no_buyers")
        assert line.unit_cost is None
        assert line.unit_sell is None
        assert line.margin_pct is None


# ── generate_ai_summary ──────────────────────────────────────────────


class TestGenerateAiSummary:
    def test_empty_plan(self):
        plan = SimpleNamespace(lines=[], ai_flags=[])
        assert "Empty buy plan" in generate_ai_summary(plan)

    def test_single_line(self):
        line = SimpleNamespace(offer_id=1, margin_pct=40.0, offer=SimpleNamespace(vendor_name="Acme"))
        plan = SimpleNamespace(lines=[line], ai_flags=[])
        summary = generate_ai_summary(plan)
        assert "1 line" in summary
        assert "1 vendor" in summary
        assert "40.0%" in summary

    def test_multi_lines_with_flags(self):
        lines = [
            SimpleNamespace(offer_id=1, margin_pct=30.0, offer=SimpleNamespace(vendor_name="Acme")),
            SimpleNamespace(offer_id=2, margin_pct=50.0, offer=SimpleNamespace(vendor_name="Beta")),
        ]
        plan = SimpleNamespace(lines=lines, ai_flags=[{"type": "stale"}])
        summary = generate_ai_summary(plan)
        assert "2 lines" in summary
        assert "2 vendors" in summary
        assert "1 flag" in summary

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


# ── generate_ai_flags ────────────────────────────────────────────────


class TestGenerateAiFlags:
    def test_stale_offer(self, db_session):
        old_date = datetime.now(timezone.utc) - timedelta(days=30)
        offer = _make_offer(created_at=old_date)
        line = SimpleNamespace(
            id=1, offer_id=1, offer=offer, margin_pct=50.0,
            requirement_id=None, quantity=100,
        )
        plan = SimpleNamespace(lines=[line])
        flags = generate_ai_flags(plan, db_session)
        stale = [f for f in flags if f["type"] == "stale_offer"]
        assert len(stale) == 1
        assert "warning" in stale[0]["severity"]

    def test_low_margin_warning(self, db_session):
        line = SimpleNamespace(
            id=1, offer_id=None, offer=None, margin_pct=5.0,
            requirement_id=None, quantity=100,
        )
        plan = SimpleNamespace(lines=[line])
        flags = generate_ai_flags(plan, db_session)
        low = [f for f in flags if f["type"] == "low_margin"]
        assert len(low) == 1
        assert low[0]["severity"] == "warning"

    def test_negative_margin_critical(self, db_session):
        line = SimpleNamespace(
            id=1, offer_id=None, offer=None, margin_pct=-5.0,
            requirement_id=None, quantity=100,
        )
        plan = SimpleNamespace(lines=[line])
        flags = generate_ai_flags(plan, db_session)
        low = [f for f in flags if f["type"] == "low_margin"]
        assert low[0]["severity"] == "critical"

    def test_no_flags_for_good_plan(self, db_session):
        recent = datetime.now(timezone.utc) - timedelta(days=1)
        offer = _make_offer(created_at=recent)
        line = SimpleNamespace(
            id=1, offer_id=1, offer=offer, margin_pct=50.0,
            requirement_id=None, quantity=100,
        )
        plan = SimpleNamespace(lines=[line])
        flags = generate_ai_flags(plan, db_session)
        assert flags == []

    def test_stale_offer_fetched_from_db(self, db_session):
        """When line.offer is None, the flag code fetches from DB via offer_id."""
        line = SimpleNamespace(
            id=1, offer_id=999, offer=None, margin_pct=50.0,
            requirement_id=None, quantity=100,
        )
        plan = SimpleNamespace(lines=[line])
        flags = generate_ai_flags(plan, db_session)
        # No crash — offer not found, just skips the stale check
        assert all(f["type"] != "stale_offer" for f in flags)

    def test_none_lines(self, db_session):
        plan = SimpleNamespace(lines=None)
        flags = generate_ai_flags(plan, db_session)
        assert flags == []


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


# ── build_buy_plan (integration) ─────────────────────────────────────


class TestBuildBuyPlan:
    def _make_site(self, db_session):
        """Helper to create a CustomerSite (required FK for Quote)."""
        from app.models import Company, CustomerSite
        co = Company(name="TestCo", website="https://testco.com", industry="Electronics", is_active=True)
        db_session.add(co)
        db_session.flush()
        site = CustomerSite(company_id=co.id, site_name="HQ")
        db_session.add(site)
        db_session.flush()
        return site

    def test_quote_not_found(self, db_session):
        with pytest.raises(ValueError, match="not found"):
            build_buy_plan(99999, db_session)

    def test_no_requirements(self, db_session):
        from app.models import Quote, Requisition, User
        site = self._make_site(db_session)
        user = User(email="noreq@test.com", name="NoReq", role="buyer", azure_id="aznoreq", is_active=True)
        db_session.add(user)
        db_session.flush()
        req = Requisition(name="REQ-V3", customer_name="Test", status="open", created_by=user.id)
        db_session.add(req)
        db_session.flush()
        q = Quote(
            requisition_id=req.id, customer_site_id=site.id,
            quote_number="Q-V3-001", status="sent", line_items=[], subtotal=100,
        )
        db_session.add(q)
        db_session.commit()
        with pytest.raises(ValueError, match="No requirements"):
            build_buy_plan(q.id, db_session)

    def test_build_with_offers(self, db_session):
        from app.models import Offer, Quote, Requirement, Requisition, User
        site = self._make_site(db_session)
        buyer = User(email="bv3@test.com", name="BV3", role="buyer", azure_id="azv3", is_active=True)
        db_session.add(buyer)
        db_session.flush()
        req = Requisition(name="REQ-V3-2", customer_name="Test", status="open", created_by=buyer.id)
        db_session.add(req)
        db_session.flush()
        item = Requirement(requisition_id=req.id, primary_mpn="LM317T", target_qty=100, target_price=1.00)
        db_session.add(item)
        db_session.flush()
        q = Quote(
            requisition_id=req.id, customer_site_id=site.id,
            quote_number="Q-V3-2", status="sent", line_items=[], subtotal=100,
        )
        db_session.add(q)
        db_session.flush()
        offer = Offer(
            requisition_id=req.id, requirement_id=item.id,
            vendor_name="Arrow", mpn="LM317T",
            qty_available=200, unit_price=0.50,
            entered_by_id=buyer.id, status="active",
        )
        db_session.add(offer)
        db_session.commit()
        plan = build_buy_plan(q.id, db_session)
        assert plan.status == BuyPlanStatus.draft.value
        assert plan.quote_id == q.id
        assert len(plan.lines) == 1
        assert plan.lines[0].quantity == 100
        assert plan.ai_summary is not None

    def test_auto_split(self, db_session):
        """When no single offer covers qty, splits across offers."""
        from app.models import Offer, Quote, Requirement, Requisition, User
        site = self._make_site(db_session)
        buyer = User(email="split@test.com", name="Split", role="buyer", azure_id="azsplit", is_active=True)
        db_session.add(buyer)
        db_session.flush()
        req = Requisition(name="REQ-SPLIT", customer_name="Test", status="open", created_by=buyer.id)
        db_session.add(req)
        db_session.flush()
        item = Requirement(requisition_id=req.id, primary_mpn="X", target_qty=100, target_price=2.00)
        db_session.add(item)
        db_session.flush()
        q = Quote(
            requisition_id=req.id, customer_site_id=site.id,
            quote_number="Q-SPLIT", status="sent", line_items=[], subtotal=200,
        )
        db_session.add(q)
        db_session.flush()
        o1 = Offer(
            requisition_id=req.id, requirement_id=item.id,
            vendor_name="V1", mpn="X", qty_available=60, unit_price=1.00,
            entered_by_id=buyer.id, status="active",
        )
        o2 = Offer(
            requisition_id=req.id, requirement_id=item.id,
            vendor_name="V2", mpn="X", qty_available=80, unit_price=1.50,
            entered_by_id=buyer.id, status="active",
        )
        db_session.add_all([o1, o2])
        db_session.commit()
        plan = build_buy_plan(q.id, db_session)
        assert len(plan.lines) == 2
        total_qty = sum(l.quantity for l in plan.lines)
        assert total_qty == 100

    def test_no_active_offers(self, db_session):
        from app.models import Quote, Requirement, Requisition, User
        site = self._make_site(db_session)
        user = User(email="nooff@test.com", name="NoOff", role="buyer", azure_id="aznooff", is_active=True)
        db_session.add(user)
        db_session.flush()
        req = Requisition(name="REQ-NOOFF", customer_name="Test", status="open", created_by=user.id)
        db_session.add(req)
        db_session.flush()
        item = Requirement(requisition_id=req.id, primary_mpn="Y", target_qty=100, target_price=1.00)
        db_session.add(item)
        db_session.flush()
        q = Quote(
            requisition_id=req.id, customer_site_id=site.id,
            quote_number="Q-NOOFF", status="sent", line_items=[], subtotal=100,
        )
        db_session.add(q)
        db_session.commit()
        plan = build_buy_plan(q.id, db_session)
        assert len(plan.lines) == 0
        assert "Empty buy plan" in plan.ai_summary

    def test_customer_site_region(self, db_session):
        """Exercises customer_region from customer_site."""
        from app.models import Company, CustomerSite, Offer, Quote, Requirement, Requisition, User
        user = User(email="reg@test.com", name="Reg", role="buyer", azure_id="azreg", is_active=True)
        db_session.add(user)
        db_session.flush()
        co = Company(name="Co", website="https://co.com", industry="Electronics", is_active=True)
        db_session.add(co)
        db_session.flush()
        site = CustomerSite(company_id=co.id, site_name="HQ", country="united states")
        db_session.add(site)
        db_session.flush()
        req = Requisition(name="REQ-REG", customer_name="Co", status="open", created_by=user.id)
        db_session.add(req)
        db_session.flush()
        item = Requirement(requisition_id=req.id, primary_mpn="Z", target_qty=50, target_price=2.00)
        db_session.add(item)
        db_session.flush()
        q = Quote(
            requisition_id=req.id, customer_site_id=site.id,
            quote_number="Q-REG", status="sent", line_items=[], subtotal=100,
        )
        db_session.add(q)
        db_session.flush()
        offer = Offer(
            requisition_id=req.id, requirement_id=item.id,
            vendor_name="V", mpn="Z", qty_available=100, unit_price=1.00,
            entered_by_id=user.id, status="active",
        )
        db_session.add(offer)
        db_session.commit()
        plan = build_buy_plan(q.id, db_session)
        assert len(plan.lines) >= 1
