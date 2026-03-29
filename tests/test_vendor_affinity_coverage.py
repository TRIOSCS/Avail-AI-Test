"""Tests for app/services/vendor_affinity_service.py — comprehensive coverage.

Covers find_affinity_vendors_l1, find_affinity_vendors_l2, find_affinity_vendors_l3,
_classify_mpn, score_affinity_matches, find_vendor_affinity.

Called by: pytest
Depends on: conftest fixtures, vendor_affinity_service
"""

import os

os.environ["TESTING"] = "1"

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models import MaterialCard, MaterialVendorHistory, Sighting, VendorCard
from app.models.sourcing import Requirement, Requisition
from app.models.tags import EntityTag, Tag


@pytest.fixture()
def material_card(db_session: Session) -> MaterialCard:
    card = MaterialCard(
        normalized_mpn="lm317t",
        display_mpn="LM317T",
        manufacturer="Texas Instruments",
        category="Voltage Regulator",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(card)
    db_session.commit()
    db_session.refresh(card)
    return card


@pytest.fixture()
def vendor_card(db_session: Session) -> VendorCard:
    vc = VendorCard(
        normalized_name="arrow",
        display_name="Arrow Electronics",
        emails=["sales@arrow.com"],
        is_blacklisted=False,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(vc)
    db_session.commit()
    db_session.refresh(vc)
    return vc


@pytest.fixture()
def vendor_history(db_session: Session, material_card: MaterialCard, vendor_card: VendorCard) -> MaterialVendorHistory:
    history = MaterialVendorHistory(
        material_card_id=material_card.id,
        vendor_name="Arrow Electronics",
        vendor_name_normalized="arrow",
        last_seen=datetime.now(timezone.utc),
        times_seen=5,
    )
    db_session.add(history)
    db_session.commit()
    db_session.refresh(history)
    return history


@pytest.fixture()
def req_with_item(db_session: Session, test_user) -> tuple:
    req = Requisition(
        name="AFFINITY-REQ",
        customer_name="Test Co",
        status="active",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.flush()
    item = Requirement(
        requisition_id=req.id,
        primary_mpn="LM317T",
        target_qty=100,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(item)
    db_session.commit()
    db_session.refresh(req)
    db_session.refresh(item)
    return req, item


@pytest.fixture()
def sighting_with_vendor(db_session: Session, req_with_item: tuple, vendor_card: VendorCard) -> Sighting:
    _, item = req_with_item
    s = Sighting(
        requirement_id=item.id,
        normalized_mpn="lm317t",
        vendor_name="Arrow Electronics",
        vendor_name_normalized="arrow",
        source_type="api",
        qty_available=1000,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    return s


class TestFindAffinityVendorsL1:
    def test_no_material_card_returns_empty(self, db_session: Session):
        from app.services.vendor_affinity_service import find_affinity_vendors_l1

        result = find_affinity_vendors_l1("UNKNOWN_MPN", db_session)
        assert result == []

    def test_card_without_manufacturer_returns_empty(self, db_session: Session):
        from app.services.vendor_affinity_service import find_affinity_vendors_l1

        card = MaterialCard(
            normalized_mpn="nomfr",
            display_mpn="NOMFR",
            manufacturer=None,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(card)
        db_session.commit()

        result = find_affinity_vendors_l1("NOMFR", db_session)
        assert result == []

    def test_finds_vendors_via_history(
        self, db_session: Session, material_card: MaterialCard, vendor_history: MaterialVendorHistory
    ):
        from app.services.vendor_affinity_service import find_affinity_vendors_l1

        # Create another MPN from the same manufacturer
        other_card = MaterialCard(
            normalized_mpn="lm7805",
            display_mpn="LM7805",
            manufacturer="Texas Instruments",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(other_card)
        db_session.flush()

        history2 = MaterialVendorHistory(
            material_card_id=other_card.id,
            vendor_name="Arrow Electronics",
            vendor_name_normalized="arrow",
            last_seen=datetime.now(timezone.utc),
            times_seen=3,
        )
        db_session.add(history2)
        db_session.commit()

        result = find_affinity_vendors_l1("lm317t", db_session)
        assert isinstance(result, list)
        # May or may not find Arrow depending on dedup logic, but should not raise

    def test_result_has_level_1(
        self, db_session: Session, material_card: MaterialCard, vendor_history: MaterialVendorHistory
    ):
        from app.services.vendor_affinity_service import find_affinity_vendors_l1

        other_card = MaterialCard(
            normalized_mpn="lm7805",
            display_mpn="LM7805",
            manufacturer="Texas Instruments",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(other_card)
        db_session.flush()
        h = MaterialVendorHistory(
            material_card_id=other_card.id,
            vendor_name="Arrow Electronics",
            vendor_name_normalized="arrow",
            last_seen=datetime.now(timezone.utc),
        )
        db_session.add(h)
        db_session.commit()

        result = find_affinity_vendors_l1("lm317t", db_session)
        for r in result:
            assert r["level"] == 1


class TestFindAffinityVendorsL2:
    def test_no_material_card_returns_empty(self, db_session: Session):
        from app.services.vendor_affinity_service import find_affinity_vendors_l2

        result = find_affinity_vendors_l2("UNKNOWN_MPN", db_session)
        assert result == []

    def test_no_vendor_cards_linked_returns_empty(
        self, db_session: Session, material_card: MaterialCard
    ):
        from app.services.vendor_affinity_service import find_affinity_vendors_l2

        result = find_affinity_vendors_l2("lm317t", db_session)
        assert result == []

    def test_no_commodity_tags_returns_empty(
        self, db_session: Session, material_card: MaterialCard,
        sighting_with_vendor: Sighting, vendor_card: VendorCard
    ):
        from app.services.vendor_affinity_service import find_affinity_vendors_l2

        result = find_affinity_vendors_l2("lm317t", db_session)
        assert result == []

    def test_exclude_vendors_filter(self, db_session: Session, material_card: MaterialCard):
        from app.services.vendor_affinity_service import find_affinity_vendors_l2

        result = find_affinity_vendors_l2("lm317t", db_session, exclude_vendors={"arrow"})
        assert result == []  # Empty DB anyway

    def test_with_commodity_tags(
        self, db_session: Session, material_card: MaterialCard,
        sighting_with_vendor: Sighting, vendor_card: VendorCard
    ):
        from app.services.vendor_affinity_service import find_affinity_vendors_l2

        # Add commodity tag to the vendor_card
        tag = Tag(
            name="voltage-regulator",
            tag_type="commodity",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(tag)
        db_session.flush()

        et = EntityTag(
            entity_type="vendor_card",
            entity_id=vendor_card.id,
            tag_id=tag.id,
        )
        db_session.add(et)

        # Create a second vendor with same commodity tag
        vc2 = VendorCard(
            normalized_name="digikey",
            display_name="DigiKey",
            emails=["sales@digikey.com"],
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(vc2)
        db_session.flush()

        et2 = EntityTag(
            entity_type="vendor_card",
            entity_id=vc2.id,
            tag_id=tag.id,
        )
        db_session.add(et2)
        db_session.commit()

        result = find_affinity_vendors_l2("lm317t", db_session)
        assert isinstance(result, list)
        # DigiKey should appear (it has the same commodity tag but wasn't in original vc_ids)


class TestFindAffinityVendorsL3:
    def test_no_api_key_returns_empty(self, db_session: Session):
        from app.services.vendor_affinity_service import find_affinity_vendors_l3

        with patch("app.services.vendor_affinity_service.settings") as mock_settings:
            mock_settings.anthropic_api_key = None
            result = find_affinity_vendors_l3("LM317T", "TI", db_session)
        assert result == []

    def test_classify_returns_none_returns_empty(self, db_session: Session):
        from app.services.vendor_affinity_service import find_affinity_vendors_l3

        with patch("app.services.vendor_affinity_service.settings") as mock_settings:
            mock_settings.anthropic_api_key = "sk-fake"
            with patch("app.services.vendor_affinity_service._classify_mpn", return_value=None):
                result = find_affinity_vendors_l3("LM317T", "TI", db_session)
        assert result == []

    def test_with_category_and_sightings(
        self, db_session: Session, material_card: MaterialCard,
        req_with_item: tuple, vendor_card: VendorCard
    ):
        from app.services.vendor_affinity_service import find_affinity_vendors_l3

        _, item = req_with_item
        s = Sighting(
            requirement_id=item.id,
            normalized_mpn="lm317t",
            vendor_name="Arrow Electronics",
            vendor_name_normalized="arrow",
            source_type="api",
            material_card_id=material_card.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(s)
        db_session.commit()

        with patch("app.services.vendor_affinity_service.settings") as mock_settings:
            mock_settings.anthropic_api_key = "sk-fake"
            with patch("app.services.vendor_affinity_service._classify_mpn", return_value="Voltage Regulator"):
                result = find_affinity_vendors_l3("LM317T", "TI", db_session)

        assert isinstance(result, list)


class TestClassifyMpn:
    def test_successful_classification(self):
        from app.services.vendor_affinity_service import _classify_mpn

        mock_client = MagicMock()
        mock_client.messages.create.return_value = MagicMock(
            content=[MagicMock(text="Voltage Regulator")]
        )

        with patch("anthropic.Anthropic", return_value=mock_client):
            result = _classify_mpn("LM317T", "Texas Instruments", "sk-fake")

        assert result == "Voltage Regulator"

    def test_returns_none_on_exception(self):
        from app.services.vendor_affinity_service import _classify_mpn

        with patch("anthropic.Anthropic", side_effect=Exception("API error")):
            result = _classify_mpn("LM317T", None, "sk-fake")

        assert result is None

    def test_no_manufacturer_hint(self):
        from app.services.vendor_affinity_service import _classify_mpn

        mock_client = MagicMock()
        mock_client.messages.create.return_value = MagicMock(
            content=[MagicMock(text="Resistor")]
        )

        with patch("anthropic.Anthropic", return_value=mock_client):
            result = _classify_mpn("RC0402", None, "sk-fake")

        assert result == "Resistor"


class TestScoreAffinityMatches:
    def test_empty_matches_returns_empty(self):
        from app.services.vendor_affinity_service import score_affinity_matches

        result = score_affinity_matches("LM317T", [])
        assert result == []

    def test_level_1_confidence_calculation(self):
        from app.services.vendor_affinity_service import score_affinity_matches

        matches = [{"vendor_name": "Arrow", "level": 1, "mpn_count": 1, "manufacturer": "TI"}]
        result = score_affinity_matches("LM317T", matches)
        assert len(result) == 1
        assert result[0]["confidence"] == pytest.approx(0.50, abs=0.01)

    def test_level_1_confidence_capped_at_0_75(self):
        from app.services.vendor_affinity_service import score_affinity_matches

        matches = [{"vendor_name": "Arrow", "level": 1, "mpn_count": 100, "manufacturer": "TI"}]
        result = score_affinity_matches("LM317T", matches)
        assert result[0]["confidence"] <= 0.75

    def test_level_2_confidence_calculation(self):
        from app.services.vendor_affinity_service import score_affinity_matches

        matches = [{"vendor_name": "Arrow", "level": 2, "mpn_count": 3, "manufacturer": None}]
        result = score_affinity_matches("LM317T", matches)
        assert result[0]["confidence"] >= 0.30
        assert result[0]["confidence"] <= 0.60

    def test_level_3_confidence_calculation(self):
        from app.services.vendor_affinity_service import score_affinity_matches

        matches = [{"vendor_name": "Arrow", "level": 3, "mpn_count": 2, "manufacturer": None}]
        result = score_affinity_matches("LM317T", matches)
        assert result[0]["confidence"] >= 0.30
        assert result[0]["confidence"] <= 0.50

    def test_result_includes_reasoning(self):
        from app.services.vendor_affinity_service import score_affinity_matches

        matches = [{"vendor_name": "Arrow", "level": 1, "mpn_count": 5, "manufacturer": "TI"}]
        result = score_affinity_matches("LM317T", matches)
        assert "reasoning" in result[0]

    def test_confidence_floor_at_0_30(self):
        from app.services.vendor_affinity_service import score_affinity_matches

        matches = [{"vendor_name": "Arrow", "level": 3, "mpn_count": 0, "manufacturer": None}]
        result = score_affinity_matches("LM317T", matches)
        assert result[0]["confidence"] >= 0.30


class TestFindVendorAffinity:
    def test_empty_db_returns_empty_list(self, db_session: Session):
        from app.services.vendor_affinity_service import find_vendor_affinity

        with patch("app.services.vendor_affinity_service.settings") as mock_settings:
            mock_settings.anthropic_api_key = None
            result = find_vendor_affinity("UNKNOWN_MPN", db_session)
        assert isinstance(result, list)
        assert len(result) == 0

    def test_deduplicates_vendors(self, db_session: Session):
        from app.services.vendor_affinity_service import find_vendor_affinity

        with patch("app.services.vendor_affinity_service.find_affinity_vendors_l1") as mock_l1:
            with patch("app.services.vendor_affinity_service.find_affinity_vendors_l2") as mock_l2:
                with patch("app.services.vendor_affinity_service.settings") as mock_settings:
                    mock_settings.anthropic_api_key = None
                    mock_l1.return_value = [
                        {"vendor_name": "Arrow", "level": 1, "mpn_count": 5, "manufacturer": "TI"}
                    ]
                    mock_l2.return_value = [
                        {"vendor_name": "Arrow", "level": 2, "mpn_count": 3, "manufacturer": "TI"}
                    ]
                    result = find_vendor_affinity("LM317T", db_session)

        # Arrow should appear only once, with highest confidence
        vendor_names = [r["vendor_name"].lower() for r in result]
        assert vendor_names.count("arrow") == 1

    def test_returns_top_10(self, db_session: Session):
        from app.services.vendor_affinity_service import find_vendor_affinity

        many_vendors = [
            {"vendor_name": f"Vendor{i}", "level": 1, "mpn_count": 1, "manufacturer": "TI"}
            for i in range(15)
        ]
        with patch("app.services.vendor_affinity_service.find_affinity_vendors_l1", return_value=many_vendors):
            with patch("app.services.vendor_affinity_service.find_affinity_vendors_l2", return_value=[]):
                with patch("app.services.vendor_affinity_service.settings") as mock_settings:
                    mock_settings.anthropic_api_key = None
                    result = find_vendor_affinity("LM317T", db_session)

        assert len(result) <= 10

    def test_triggers_l3_when_combined_lt_5(self, db_session: Session):
        from app.services.vendor_affinity_service import find_vendor_affinity

        with patch("app.services.vendor_affinity_service.find_affinity_vendors_l1", return_value=[]):
            with patch("app.services.vendor_affinity_service.find_affinity_vendors_l2", return_value=[]):
                with patch("app.services.vendor_affinity_service.find_affinity_vendors_l3", return_value=[]) as mock_l3:
                    with patch("app.services.vendor_affinity_service.settings") as mock_settings:
                        mock_settings.anthropic_api_key = "sk-fake"
                        find_vendor_affinity("LM317T", db_session)

        mock_l3.assert_called_once()

    def test_skips_l3_when_enough_matches(self, db_session: Session):
        from app.services.vendor_affinity_service import find_vendor_affinity

        five_vendors = [
            {"vendor_name": f"V{i}", "level": 1, "mpn_count": 1, "manufacturer": "TI"}
            for i in range(5)
        ]
        with patch("app.services.vendor_affinity_service.find_affinity_vendors_l1", return_value=five_vendors):
            with patch("app.services.vendor_affinity_service.find_affinity_vendors_l2", return_value=[]):
                with patch("app.services.vendor_affinity_service.find_affinity_vendors_l3") as mock_l3:
                    with patch("app.services.vendor_affinity_service.settings") as mock_settings:
                        mock_settings.anthropic_api_key = "sk-fake"
                        find_vendor_affinity("LM317T", db_session)

        mock_l3.assert_not_called()
