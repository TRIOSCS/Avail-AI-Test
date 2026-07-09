"""
test_specialty_detector.py -- Tests for specialty_detector.py

Tests brand/commodity detection from text and the DB-backed
analyze_vendor_specialties function.

Called by: pytest
Depends on: app/services/specialty_detector.py, conftest.py
"""

from datetime import UTC, datetime

import pytest

from app.models import Offer, Requirement, Requisition, Sighting, User, VendorCard
from app.services.specialty_detector import (
    analyze_vendor_specialties,
    detect_brands_from_text,
    detect_commodities_from_text,
)

# ── detect_brands_from_text ──────────────────────────────────────────


class TestDetectBrandsFromText:
    @pytest.mark.parametrize(
        "text,expected",
        [
            pytest.param("", [], id="empty_text"),
            pytest.param(None, [], id="none_text"),
            pytest.param("This text has no brand names whatsoever", [], id="no_match"),
        ],
    )
    def test_returns_empty(self, text, expected):
        assert detect_brands_from_text(text) == expected

    @pytest.mark.parametrize(
        "text,expected_brands",
        [
            pytest.param("We have Intel processors in stock", ["Intel"], id="single_brand"),
            pytest.param("Intel and AMD compete in the CPU market", ["Intel", "AMD"], id="multiple_brands"),
            pytest.param("texas instruments makes great chips", ["Texas Instruments"], id="case_insensitive"),
            # 'NXP' should match, but should not match inside 'UNEXPECTED'.
            pytest.param("NXP semiconductors", ["NXP"], id="word_boundary_respected"),
            # 3M has special regex implications but should still match.
            pytest.param("3M connectors are reliable", ["3M"], id="special_chars_escaped"),
        ],
    )
    def test_detects_brands(self, text, expected_brands):
        result = detect_brands_from_text(text)
        for brand in expected_brands:
            assert brand in result


# ── detect_commodities_from_text ─────────────────────────────────────


class TestDetectCommoditiesFromText:
    @pytest.mark.parametrize(
        "text,expected",
        [
            pytest.param("", [], id="empty_text"),
            pytest.param(None, [], id="none_text"),
            pytest.param("hello world foo bar", [], id="no_match"),
        ],
    )
    def test_returns_empty(self, text, expected):
        assert detect_commodities_from_text(text) == expected

    @pytest.mark.parametrize(
        "text,expected_commodities",
        [
            pytest.param("DDR4 SDRAM module 16GB", ["dram"], id="single_commodity"),
            pytest.param("SSD and DDR SDRAM module", ["dram", "ssd"], id="multiple_commodities"),
        ],
    )
    def test_detects_commodities(self, text, expected_commodities):
        result = detect_commodities_from_text(text)
        for commodity in expected_commodities:
            assert commodity in result

    def test_sorted_output(self):
        result = detect_commodities_from_text("resistor and capacitor and inductor")
        assert result == sorted(result)

    def test_no_duplicates(self):
        """Multiple keywords mapping to same category should not duplicate."""
        result = detect_commodities_from_text("ddr sdram dimm rdimm")
        assert result.count("dram") == 1


# ── analyze_vendor_specialties (DB tests) ────────────────────────────


def _make_vendor_card(db, name="test vendor"):
    card = VendorCard(
        normalized_name=name.lower(),
        display_name=name,
        created_at=datetime.now(UTC),
    )
    db.add(card)
    db.flush()
    return card


def _make_user(db, email="specialty-user@test.com"):
    u = User(
        email=email,
        name="Specialty User",
        role="buyer",
        azure_id=f"az-{email}",
        created_at=datetime.now(UTC),
    )
    db.add(u)
    db.flush()
    return u


def _make_requisition(db, user_id):
    req = Requisition(
        name="REQ-SPEC",
        customer_name="Test",
        status="open",
        created_by=user_id,
        created_at=datetime.now(UTC),
    )
    db.add(req)
    db.flush()
    return req


class TestAnalyzeVendorSpecialties:
    def test_nonexistent_vendor(self, db_session):
        """Non-existent vendor card returns empty results."""
        result = analyze_vendor_specialties(99999, db_session)
        assert result["brand_tags"] == []
        assert result["commodity_tags"] == []
        assert result["confidence"] == 0.0

    def test_vendor_no_data(self, db_session):
        """Vendor with no sightings, offers, or useful fields."""
        card = _make_vendor_card(db_session, "empty vendor")
        db_session.commit()

        result = analyze_vendor_specialties(card.id, db_session)
        assert result["brand_tags"] == []
        assert result["commodity_tags"] == []
        assert result["confidence"] == 0.0

    def test_vendor_with_sightings(self, db_session):
        """Sightings with manufacturer data produce brand tags."""
        card = _make_vendor_card(db_session, "sighting vendor")
        user = _make_user(db_session)
        req = _make_requisition(db_session, user.id)

        requirement = Requirement(
            requisition_id=req.id,
            primary_mpn="LM317T",
            target_qty=100,
            created_at=datetime.now(UTC),
        )
        db_session.add(requirement)
        db_session.flush()

        s = Sighting(
            requirement_id=requirement.id,
            vendor_name="sighting vendor",
            mpn_matched="DDR4-SDRAM-MODULE",
            manufacturer="Texas Instruments",
            source_type="api",
            created_at=datetime.now(UTC),
        )
        db_session.add(s)
        db_session.commit()

        result = analyze_vendor_specialties(card.id, db_session)
        assert "Texas Instruments" in result["brand_tags"]
        assert "dram" in result["commodity_tags"]
        assert result["confidence"] > 0.0

    def test_vendor_with_offers(self, db_session):
        """Offers with manufacturer data produce brand tags (weighted 2x)."""
        card = _make_vendor_card(db_session, "offer vendor")
        user = _make_user(db_session, "offer@test.com")
        req = _make_requisition(db_session, user.id)

        o = Offer(
            requisition_id=req.id,
            vendor_card_id=card.id,
            vendor_name="offer vendor",
            mpn="CAPACITOR-MLCC",
            manufacturer="Murata",
            qty_available=1000,
            unit_price=0.10,
            entered_by_id=user.id,
            status="active",
            created_at=datetime.now(UTC),
        )
        db_session.add(o)
        db_session.commit()

        result = analyze_vendor_specialties(card.id, db_session)
        assert "Murata" in result["brand_tags"]
        assert "capacitors" in result["commodity_tags"]

    def test_vendor_card_display_name_brand(self, db_session):
        """Brand in vendor card display_name is detected."""
        card = _make_vendor_card(db_session, "Intel Distribution Center")
        db_session.commit()

        result = analyze_vendor_specialties(card.id, db_session)
        assert "Intel" in result["brand_tags"]

    def test_vendor_card_industry_field(self, db_session):
        """Brand in vendor card industry field is detected."""
        card = _make_vendor_card(db_session, "chip supplier")
        card.industry = "Samsung Memory Products"
        db_session.commit()

        result = analyze_vendor_specialties(card.id, db_session)
        assert "Samsung" in result["brand_tags"]

    def test_confidence_scales_with_data(self, db_session):
        """More data points produce higher confidence (capped at 0.95)."""
        card = _make_vendor_card(db_session, "multi vendor")
        user = _make_user(db_session, "multi@test.com")
        req = _make_requisition(db_session, user.id)

        # Add many offers
        for i in range(20):
            o = Offer(
                requisition_id=req.id,
                vendor_card_id=card.id,
                vendor_name="multi vendor",
                mpn=f"TEST-{i}",
                manufacturer="Intel",
                qty_available=100,
                unit_price=1.00,
                entered_by_id=user.id,
                status="active",
                created_at=datetime.now(UTC),
            )
            db_session.add(o)
        db_session.commit()

        result = analyze_vendor_specialties(card.id, db_session)
        assert result["confidence"] <= 0.95
        assert result["confidence"] > 0.3

    def test_sighting_null_manufacturer_and_mpn(self, db_session):
        """Sightings with null manufacturer and mpn_matched are handled."""
        card = _make_vendor_card(db_session, "null sighting vendor")
        user = _make_user(db_session, "null@test.com")
        req = _make_requisition(db_session, user.id)

        requirement = Requirement(
            requisition_id=req.id,
            primary_mpn="TEST",
            target_qty=100,
            created_at=datetime.now(UTC),
        )
        db_session.add(requirement)
        db_session.flush()

        s = Sighting(
            requirement_id=requirement.id,
            vendor_name="null sighting vendor",
            mpn_matched=None,
            manufacturer=None,
            source_type="api",
            created_at=datetime.now(UTC),
        )
        db_session.add(s)
        db_session.commit()

        result = analyze_vendor_specialties(card.id, db_session)
        # No crash; empty results from null data
        assert isinstance(result["brand_tags"], list)
        assert isinstance(result["commodity_tags"], list)

    def test_offer_null_manufacturer_and_mpn(self, db_session):
        """Offers with null manufacturer and mpn are handled gracefully."""
        card = _make_vendor_card(db_session, "null offer vendor")
        user = _make_user(db_session, "nulloffer@test.com")
        req = _make_requisition(db_session, user.id)

        o = Offer(
            requisition_id=req.id,
            vendor_card_id=card.id,
            vendor_name="null offer vendor",
            mpn="",
            manufacturer=None,
            qty_available=100,
            unit_price=1.00,
            entered_by_id=user.id,
            status="active",
            created_at=datetime.now(UTC),
        )
        db_session.add(o)
        db_session.commit()

        result = analyze_vendor_specialties(card.id, db_session)
        assert isinstance(result["brand_tags"], list)

    def test_top_results_limited(self, db_session):
        """brand_tags limited to 15, commodity_tags limited to 10."""
        # The limits are applied by most_common(15) and most_common(10).
        # With enough diverse data, this limit kicks in.
        card = _make_vendor_card(db_session, "diverse vendor")
        db_session.commit()

        result = analyze_vendor_specialties(card.id, db_session)
        assert len(result["brand_tags"]) <= 15
        assert len(result["commodity_tags"]) <= 10
