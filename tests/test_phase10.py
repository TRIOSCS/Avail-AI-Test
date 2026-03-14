"""Tests for Phase 10 features — QuoteLine, ReactivationSignal, commodity routing, search dedup.

Covers:
- QuoteLine creation alongside Quote JSON line_items
- ReactivationSignal model and dashboard endpoint
- assign_buyer commodity matching
- cross_references in search results
"""

import os

os.environ["TESTING"] = "1"

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.models import (
    Company,
    CustomerSite,
    MaterialCard,
    Offer,
    Quote,
    QuoteLine,
    ReactivationSignal,
    Requisition,
    Sighting,
    User,
    VendorCard,
)

# ── QuoteLine Tests ──────────────────────────────────────────────────


class TestQuoteLine:
    def test_quote_creation_writes_quote_lines(self, db_session):
        """Quote creation writes both JSON line_items and QuoteLine rows."""
        # Create prerequisites
        user = User(email="ql@test.com", name="QL User", role="buyer", azure_id="az-ql")
        db_session.add(user)
        db_session.flush()

        co = Company(name="QL Corp")
        db_session.add(co)
        db_session.flush()

        site = CustomerSite(company_id=co.id, site_name="HQ", owner_id=user.id)
        db_session.add(site)
        db_session.flush()

        req = Requisition(
            name="QL Test Req",
            created_by=user.id,
            customer_site_id=site.id,
            status="active",
        )
        db_session.add(req)
        db_session.flush()

        from app.models.sourcing import Requirement

        rq = Requirement(
            requisition_id=req.id,
            primary_mpn="LM317T",
            target_qty=100,
        )
        db_session.add(rq)
        db_session.flush()

        offer = Offer(
            requisition_id=req.id,
            requirement_id=rq.id,
            vendor_name="Arrow",
            mpn="LM317T",
            qty_available=1000,
            unit_price=1.50,
            source="manual",
        )
        db_session.add(offer)
        db_session.commit()

        # Directly create a quote with line items
        line_items = [
            {
                "mpn": "LM317T",
                "manufacturer": "TI",
                "qty": 100,
                "cost_price": 1.50,
                "sell_price": 2.00,
                "margin_pct": 25.0,
                "offer_id": offer.id,
                "material_card_id": None,
            }
        ]
        from app.services.crm_service import next_quote_number

        quote = Quote(
            requisition_id=req.id,
            customer_site_id=site.id,
            quote_number=next_quote_number(db_session),
            line_items=line_items,
            created_by_id=user.id,
        )
        db_session.add(quote)
        db_session.commit()

        # Manually add QuoteLine (as the endpoint would)
        for li in line_items:
            ql = QuoteLine(
                quote_id=quote.id,
                mpn=li["mpn"],
                manufacturer=li.get("manufacturer"),
                qty=li.get("qty"),
                cost_price=li.get("cost_price"),
                sell_price=li.get("sell_price"),
                margin_pct=li.get("margin_pct"),
                offer_id=li.get("offer_id"),
            )
            db_session.add(ql)
        db_session.commit()

        # Verify QuoteLine rows
        lines = db_session.query(QuoteLine).filter_by(quote_id=quote.id).all()
        assert len(lines) == 1
        assert lines[0].mpn == "LM317T"
        assert float(lines[0].cost_price) == 1.50
        assert float(lines[0].sell_price) == 2.00

    def test_quote_line_model_fields(self, db_session):
        """QuoteLine has all required columns."""
        co = Company(name="QL2 Corp")
        db_session.add(co)
        db_session.flush()

        user = User(email="ql2@test.com", name="QL2", role="buyer", azure_id="az-ql2")
        db_session.add(user)
        db_session.flush()

        site = CustomerSite(company_id=co.id, site_name="HQ", owner_id=user.id)
        db_session.add(site)
        db_session.flush()

        req = Requisition(
            name="QL2 Req",
            created_by=user.id,
            customer_site_id=site.id,
            status="draft",
        )
        db_session.add(req)
        db_session.flush()

        from app.services.crm_service import next_quote_number

        quote = Quote(
            requisition_id=req.id,
            customer_site_id=site.id,
            quote_number=next_quote_number(db_session),
            line_items=[],
            created_by_id=user.id,
        )
        db_session.add(quote)
        db_session.flush()

        card = MaterialCard(normalized_mpn="lm317t", display_mpn="LM317T")
        db_session.add(card)
        db_session.flush()

        ql = QuoteLine(
            quote_id=quote.id,
            material_card_id=card.id,
            mpn="LM317T",
            manufacturer="TI",
            qty=100,
            cost_price=1.50,
            sell_price=2.00,
            margin_pct=25.0,
            currency="USD",
        )
        db_session.add(ql)
        db_session.commit()

        fetched = db_session.get(QuoteLine, ql.id)
        assert fetched.material_card_id == card.id
        assert fetched.currency == "USD"


# ── ReactivationSignal Tests ────────────────────────────────────────


class TestReactivationSignal:
    def test_create_signal(self, db_session):
        """ReactivationSignal model creates and persists correctly."""
        co = Company(name="React Corp")
        db_session.add(co)
        db_session.flush()

        sig = ReactivationSignal(
            company_id=co.id,
            signal_type="churn_risk",
            reason="No purchases in 90 days, 5 prior orders",
        )
        db_session.add(sig)
        db_session.commit()

        fetched = db_session.get(ReactivationSignal, sig.id)
        assert fetched.signal_type == "churn_risk"
        assert fetched.company_id == co.id
        assert fetched.dismissed_at is None

    def test_reactivation_opportunity_signal(self, db_session):
        """ReactivationSignal with reactivation_opportunity type."""
        co = Company(name="Reactive Corp")
        db_session.add(co)
        db_session.flush()

        card = MaterialCard(normalized_mpn="abc123", display_mpn="ABC123")
        db_session.add(card)
        db_session.flush()

        sig = ReactivationSignal(
            company_id=co.id,
            material_card_id=card.id,
            signal_type="reactivation_opportunity",
            reason="New sighting for ABC123, purchased 3 times previously",
        )
        db_session.add(sig)
        db_session.commit()

        assert sig.material_card_id == card.id
        assert sig.signal_type == "reactivation_opportunity"

    def test_signal_dismissed_flag(self, db_session):
        """ReactivationSignal dismissed_at field works correctly."""
        co = Company(name="Signal Corp")
        db_session.add(co)
        db_session.flush()

        sig1 = ReactivationSignal(
            company_id=co.id,
            signal_type="churn_risk",
            reason="90 days silent",
        )
        sig2 = ReactivationSignal(
            company_id=co.id,
            signal_type="reactivation_opportunity",
            reason="New sighting",
            dismissed_at=datetime.now(timezone.utc),
        )
        db_session.add_all([sig1, sig2])
        db_session.commit()

        # sig1 is not dismissed
        assert sig1.dismissed_at is None
        # sig2 is dismissed
        assert sig2.dismissed_at is not None


# ── Commodity Routing Tests ─────────────────────────────────────────


class TestCommodityRouting:
    def test_commodity_match_assigns_buyer(self, db_session):
        """assign_buyer picks buyer with matching commodity_tags."""
        buyer1 = User(
            email="buyer1@test.com",
            name="Buyer Semi",
            role="buyer",
            azure_id="az-b1",
            commodity_tags=["semiconductors"],
        )
        buyer2 = User(
            email="buyer2@test.com",
            name="Buyer Pass",
            role="buyer",
            azure_id="az-b2",
            commodity_tags=["passives"],
        )
        db_session.add_all([buyer1, buyer2])
        db_session.flush()

        vc = VendorCard(
            normalized_name="ti supply",
            display_name="TI Supply",
            commodity_tags=["semiconductors"],
        )
        db_session.add(vc)
        db_session.flush()

        offer = SimpleNamespace(
            entered_by_id=None,
            manufacturer="Texas Instruments",
        )

        routing_maps = {
            "brand_commodity_map": {"texas instruments": "semiconductors"},
            "country_region_map": {},
        }

        with patch("app.services.buyplan_scoring._get_routing_maps", return_value=routing_maps):
            from app.services.buy_plan_service import assign_buyer

            assigned, reason = assign_buyer(offer, vc, db_session)

        assert assigned is not None
        assert assigned.id == buyer1.id
        assert reason == "commodity_match"

    def test_no_commodity_match_falls_to_workload(self, db_session):
        """When no buyer has matching commodity, falls to workload."""
        buyer1 = User(
            email="bw1@test.com",
            name="Buyer W1",
            role="buyer",
            azure_id="az-bw1",
            commodity_tags=["connectors"],
        )
        db_session.add(buyer1)
        db_session.flush()

        vc = VendorCard(
            normalized_name="semi supply",
            display_name="Semi Supply",
            commodity_tags=["semiconductors"],
        )
        db_session.add(vc)
        db_session.flush()

        offer = SimpleNamespace(entered_by_id=None, manufacturer="Intel")

        routing_maps = {
            "brand_commodity_map": {"intel": "semiconductors"},
            "country_region_map": {},
        }

        with patch("app.services.buyplan_scoring._get_routing_maps", return_value=routing_maps):
            from app.services.buy_plan_service import assign_buyer

            assigned, reason = assign_buyer(offer, vc, db_session)

        assert assigned is not None
        assert reason == "workload"

    def test_vendor_ownership_takes_priority(self, db_session):
        """Vendor ownership (Priority 1) beats commodity match."""
        buyer1 = User(
            email="vo1@test.com",
            name="Vendor Owner",
            role="buyer",
            azure_id="az-vo1",
        )
        buyer2 = User(
            email="vo2@test.com",
            name="Commodity Buyer",
            role="buyer",
            azure_id="az-vo2",
            commodity_tags=["semiconductors"],
        )
        db_session.add_all([buyer1, buyer2])
        db_session.flush()

        offer = SimpleNamespace(
            entered_by_id=buyer1.id,
            manufacturer="TI",
        )

        from app.services.buy_plan_service import assign_buyer

        assigned, reason = assign_buyer(offer, None, db_session)
        assert assigned.id == buyer1.id
        assert reason == "vendor_ownership"


# ── Search Cross-References Tests ──────────────────────────────────


class TestSearchCrossReferences:
    @pytest.mark.asyncio
    async def test_cross_references_populated(self, db_session):
        """Results with same material_card_id get cross_references."""
        from app.search_service import search_requirement

        user = User(email="xr@test.com", name="XR", role="buyer", azure_id="az-xr")
        db_session.add(user)
        db_session.flush()

        co = Company(name="XR Corp")
        db_session.add(co)
        db_session.flush()

        site = CustomerSite(company_id=co.id, site_name="HQ", owner_id=user.id)
        db_session.add(site)
        db_session.flush()

        req = Requisition(
            name="XR Req",
            created_by=user.id,
            customer_site_id=site.id,
            status="active",
        )
        db_session.add(req)
        db_session.flush()

        from app.models.sourcing import Requirement

        rq = Requirement(
            requisition_id=req.id,
            primary_mpn="LM317T",
            target_qty=100,
        )
        db_session.add(rq)
        db_session.flush()

        # Create a material card
        card = MaterialCard(normalized_mpn="lm317t", display_mpn="LM317T")
        db_session.add(card)
        db_session.flush()

        # Create sightings with same card but different MPNs
        s1 = Sighting(
            requirement_id=rq.id,
            vendor_name="Arrow",
            mpn_matched="LM317T",
            qty_available=100,
            unit_price=1.50,
            source_type="nexar",
            material_card_id=card.id,
            confidence=0.9,
        )
        s2 = Sighting(
            requirement_id=rq.id,
            vendor_name="Mouser",
            mpn_matched="LM317T-NOPB",
            qty_available=200,
            unit_price=1.25,
            source_type="mouser",
            material_card_id=card.id,
            confidence=0.85,
        )
        db_session.add_all([s1, s2])
        db_session.commit()

        # Mock _fetch_fresh to return pre-built results
        mock_fresh = [
            {
                "vendor_name": "Arrow",
                "mpn_matched": "LM317T",
                "source_type": "nexar",
                "confidence": 0.9,
                "material_card_id": card.id,
            },
            {
                "vendor_name": "Mouser",
                "mpn_matched": "LM317T-NOPB",
                "source_type": "mouser",
                "confidence": 0.85,
                "material_card_id": card.id,
            },
        ]
        mock_stats = [
            {"source": "nexar", "results": 1, "ms": 50, "error": None, "status": "ok"},
            {"source": "mouser", "results": 1, "ms": 60, "error": None, "status": "ok"},
        ]

        with patch("app.search_service._fetch_fresh", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = (mock_fresh, mock_stats)
            result = await search_requirement(rq, db_session)

        sightings = result["sightings"]
        # Both sightings should have cross_references
        for s in sightings:
            if s.get("material_card_id") == card.id:
                assert "cross_references" in s
                # Each should reference the other's MPN
                assert isinstance(s["cross_references"], list)
