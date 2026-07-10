"""test_material_card_service.py — Tests for material card serialization, inference, and
merge.

Covers: manufacturer inference, backfill, card serialization, and card merge logic.

Called by: pytest
Depends on: app.services.material_card_service, conftest fixtures
"""

from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from app.models import (
    MaterialCard,
    MaterialVendorHistory,
    Offer,
    Requirement,
    Requisition,
    Sighting,
)
from app.models.tags import MaterialTag, Tag
from app.services.material_card_service import (
    backfill_missing_manufacturers,
    infer_manufacturer,
    merge_material_cards,
    serialize_material_card,
)

# -- Factories ----------------------------------------------------------------


def _make_material_card(db: Session, normalized_mpn: str, manufacturer=None, **kw) -> MaterialCard:
    mc = MaterialCard(
        normalized_mpn=normalized_mpn,
        display_mpn=kw.get("display_mpn", normalized_mpn.upper()),
        manufacturer=manufacturer,
        description=kw.get("description"),
        search_count=kw.get("search_count", 0),
        created_at=datetime.now(UTC),
    )
    db.add(mc)
    db.flush()
    return mc


def _make_vendor_history(db: Session, material_card_id: int, vendor_name: str, **kw) -> MaterialVendorHistory:
    vh = MaterialVendorHistory(
        material_card_id=material_card_id,
        vendor_name=vendor_name,
        vendor_name_normalized=vendor_name.lower(),
        source_type=kw.get("source_type", "api_sighting"),
        is_authorized=kw.get("is_authorized", False),
        first_seen=kw.get("first_seen", datetime(2025, 1, 1, tzinfo=UTC)),
        last_seen=kw.get("last_seen", datetime(2025, 6, 1, tzinfo=UTC)),
        times_seen=kw.get("times_seen", 1),
        last_qty=kw.get("last_qty"),
        last_price=kw.get("last_price"),
        last_currency=kw.get("last_currency", "USD"),
        created_at=datetime.now(UTC),
    )
    db.add(vh)
    db.flush()
    return vh


def _make_sighting(db: Session, requirement_id: int, material_card_id: int, vendor_name: str, **kw) -> Sighting:
    s = Sighting(
        requirement_id=requirement_id,
        material_card_id=material_card_id,
        vendor_name=vendor_name,
        qty_available=kw.get("qty_available", 100),
        unit_price=kw.get("unit_price", 1.0),
        source_type=kw.get("source_type", "api"),
        is_unavailable=kw.get("is_unavailable", False),
        created_at=datetime.now(UTC),
    )
    db.add(s)
    db.flush()
    return s


def _make_tag(db: Session, name: str, tag_type: str = "brand") -> Tag:
    t = Tag(name=name, tag_type=tag_type)
    db.add(t)
    db.flush()
    return t


def _make_material_tag(
    db: Session, material_card_id: int, tag_id: int, confidence: float, source: str = "ai_classified"
) -> MaterialTag:
    mt = MaterialTag(
        material_card_id=material_card_id,
        tag_id=tag_id,
        confidence=confidence,
        source=source,
    )
    db.add(mt)
    db.flush()
    return mt


# -- TestInferManufacturer ----------------------------------------------------


class TestInferManufacturer:
    def test_finds_manufacturer_by_prefix(self, db_session: Session):
        # Card MPN must be >= MIN_MPN_PREFIX_LENGTH+1 (7) to be found by prefix walk
        _make_material_card(db_session, "lm317tx", manufacturer="Texas Instruments")
        result = infer_manufacturer(db_session, "lm317txx")
        assert result == "Texas Instruments"

    def test_returns_none_for_short_mpn(self, db_session: Session):
        _make_material_card(db_session, "lm3", manufacturer="TI")
        result = infer_manufacturer(db_session, "lm317t")  # prefix walk: lm317, lm31 — too short
        assert result is None  # no match at prefix length >= 6+1

    def test_returns_none_when_no_match(self, db_session: Session):
        result = infer_manufacturer(db_session, "xyz12345")
        assert result is None

    def test_skips_empty_manufacturer(self, db_session: Session):
        _make_material_card(db_session, "abc1234", manufacturer="")
        result = infer_manufacturer(db_session, "abc12345")
        assert result is None


# -- TestBackfillMissingManufacturers -----------------------------------------


class TestBackfillMissingManufacturers:
    def test_backfills_null_manufacturers(self, db_session: Session):
        # Donor card with 7+ char MPN that serves as prefix match
        _make_material_card(db_session, "lm317tx", manufacturer="Texas Instruments")
        target = _make_material_card(db_session, "lm317txx", manufacturer=None)
        count = backfill_missing_manufacturers(db_session)
        db_session.commit()

        assert count == 1
        db_session.refresh(target)
        assert target.manufacturer == "Texas Instruments"

    def test_skips_already_populated(self, db_session: Session):
        _make_material_card(db_session, "lm317tx", manufacturer="Texas Instruments")
        _make_material_card(db_session, "lm317txx", manufacturer="Existing Mfg")
        count = backfill_missing_manufacturers(db_session)
        assert count == 0

    def test_returns_update_count(self, db_session: Session):
        _make_material_card(db_session, "lm317tx", manufacturer="TI")
        _make_material_card(db_session, "lm317txx", manufacturer=None)
        _make_material_card(db_session, "lm317txyz", manufacturer="")
        count = backfill_missing_manufacturers(db_session)
        assert count == 2


# -- TestSerializeMaterialCard ------------------------------------------------


class TestSerializeMaterialCard:
    def test_basic_serialization(self, db_session: Session, test_material_card):
        result = serialize_material_card(test_material_card, db_session)
        assert result["id"] == test_material_card.id
        assert result["normalized_mpn"] == "lm317t"
        assert result["manufacturer"] == "Texas Instruments"
        assert result["vendor_history"] == []
        assert result["sightings"] == []
        assert result["offers"] == []
        assert result["tags"] == []

    def test_includes_vendor_history(self, db_session: Session, test_material_card):
        _make_vendor_history(db_session, test_material_card.id, "Arrow Electronics")
        db_session.commit()

        result = serialize_material_card(test_material_card, db_session)
        assert len(result["vendor_history"]) == 1
        assert result["vendor_history"][0]["vendor_name"] == "Arrow Electronics"
        assert result["vendor_count"] == 1

    def test_filters_unavailable_sightings(self, db_session: Session, test_material_card, test_requisition):
        req_item = test_requisition.requirements[0]
        _make_sighting(db_session, req_item.id, test_material_card.id, "Good Vendor", is_unavailable=False)
        _make_sighting(db_session, req_item.id, test_material_card.id, "Bad Vendor", is_unavailable=True)
        db_session.commit()

        result = serialize_material_card(test_material_card, db_session)
        assert len(result["sightings"]) == 1
        assert result["sightings"][0]["vendor_name"] == "Good Vendor"

    def test_filters_low_confidence_tags(self, db_session: Session, test_material_card):
        tag_high = _make_tag(db_session, "Semiconductors", "commodity")
        tag_low = _make_tag(db_session, "Passive", "commodity")
        _make_material_tag(db_session, test_material_card.id, tag_high.id, 0.85)
        _make_material_tag(db_session, test_material_card.id, tag_low.id, 0.50)
        db_session.commit()

        result = serialize_material_card(test_material_card, db_session)
        assert len(result["tags"]) == 1
        assert result["tags"][0]["name"] == "Semiconductors"


# -- TestMergeMaterialCards ---------------------------------------------------


class TestMergeMaterialCards:
    @patch("app.services.audit_service.log_audit")
    def test_merge_repoints_requirements_sightings_offers(self, mock_audit, db_session: Session, test_user):
        source = _make_material_card(db_session, "lm317t-src")
        target = _make_material_card(db_session, "lm317t-tgt")

        req = Requisition(
            name="MergeTest",
            status="open",
            created_by=test_user.id,
            created_at=datetime.now(UTC),
        )
        db_session.add(req)
        db_session.flush()

        item = Requirement(
            requisition_id=req.id,
            primary_mpn="LM317T",
            material_card_id=source.id,
            target_qty=100,
            created_at=datetime.now(UTC),
        )
        db_session.add(item)
        db_session.flush()

        sighting = _make_sighting(db_session, item.id, source.id, "Vendor A")
        offer = Offer(
            requisition_id=req.id,
            material_card_id=source.id,
            vendor_name="Vendor A",
            mpn="LM317T",
            status="active",
            created_at=datetime.now(UTC),
        )
        db_session.add(offer)
        db_session.flush()

        result = merge_material_cards(db_session, source.id, target.id, "admin@test.com")
        db_session.commit()

        assert result["reassigned"]["requirements"] == 1
        assert result["reassigned"]["sightings"] == 1
        assert result["reassigned"]["offers"] == 1

        db_session.refresh(item)
        assert item.material_card_id == target.id

    @pytest.mark.parametrize(
        "source_id_self,target_id,match",
        [
            (True, "self", "Cannot merge a card with itself"),
            (False, "card", "Source card 99999 not found"),
            (True, "missing", "Target card 99999 not found"),
        ],
        ids=["same_id", "source_not_found", "target_not_found"],
    )
    def test_invalid_merge_raises(self, db_session: Session, test_material_card, source_id_self, target_id, match):
        source = test_material_card.id if source_id_self else 99999
        target = {"self": test_material_card.id, "card": test_material_card.id, "missing": 99999}[target_id]
        with pytest.raises(ValueError, match=match):
            merge_material_cards(db_session, source, target, "x@test.com")

    @patch("app.services.audit_service.log_audit")
    def test_merge_vendor_histories_combined(self, mock_audit, db_session: Session):
        source = _make_material_card(db_session, "merge-src")
        target = _make_material_card(db_session, "merge-tgt")

        _make_vendor_history(
            db_session,
            source.id,
            "Arrow Electronics",
            times_seen=3,
            first_seen=datetime(2024, 1, 1, tzinfo=UTC),
            last_seen=datetime(2025, 12, 1, tzinfo=UTC),
            last_qty=500,
            last_price=1.5,
        )
        tvh = _make_vendor_history(
            db_session,
            target.id,
            "Arrow Electronics",
            times_seen=2,
            first_seen=datetime(2025, 3, 1, tzinfo=UTC),
            last_seen=datetime(2025, 6, 1, tzinfo=UTC),
        )
        db_session.commit()

        result = merge_material_cards(db_session, source.id, target.id, "admin@test.com")
        db_session.commit()

        assert result["vendor_histories_merged"] == 1
        db_session.refresh(tvh)
        assert tvh.times_seen == 5  # 3 + 2
        # SQLite strips timezone info, so compare naive datetimes
        assert tvh.first_seen.year == 2024 and tvh.first_seen.month == 1  # earlier
        assert tvh.last_seen.year == 2025 and tvh.last_seen.month == 12  # later
        assert tvh.last_qty == 500  # from later source
        assert float(tvh.last_price) == 1.5

    @patch("app.services.audit_service.log_audit")
    def test_merge_vendor_history_moved_when_no_match(self, mock_audit, db_session: Session):
        source = _make_material_card(db_session, "move-src")
        target = _make_material_card(db_session, "move-tgt")

        _make_vendor_history(db_session, source.id, "Unique Vendor")
        db_session.commit()

        result = merge_material_cards(db_session, source.id, target.id, "admin@test.com")

        # Result should report 1 VH moved (before commit, which may cascade-delete on SQLite)
        assert result["vendor_histories_moved"] == 1
        assert result["vendor_histories_merged"] == 0

    @patch("app.services.audit_service.log_audit")
    def test_merge_fills_missing_metadata(self, mock_audit, db_session: Session):
        source = _make_material_card(db_session, "meta-src", manufacturer="TI", description="Voltage regulator")
        target = _make_material_card(db_session, "meta-tgt", manufacturer=None)
        source.search_count = 5
        target.search_count = 3
        db_session.flush()

        merge_material_cards(db_session, source.id, target.id, "admin@test.com")
        db_session.commit()

        db_session.refresh(target)
        assert target.manufacturer == "TI"
        # The carried value arrives WITH provenance (the source card's stored
        # provenance — legacy floor here) — never provenance-less.
        assert target.manufacturer_source == "legacy_backfill"
        assert target.manufacturer_tier == 50
        assert target.description == "Voltage regulator"
        assert target.search_count == 8  # 5 + 3

    @patch("app.services.audit_service.log_audit")
    def test_merge_carries_brand_and_ladders_manufacturer_with_source_provenance(self, mock_audit, db_session: Session):
        # The source card holds tier-95 trio maker evidence + a brand; the target holds
        # an unprovenanced legacy OEM value (floor 50). The merge must arbitrate through
        # the F1 ladder with the SOURCE card's STORED provenance — a fill-when-empty copy
        # would silently destroy the trio evidence (the source card is deleted below it).
        source = _make_material_card(db_session, "prov-src", manufacturer="Seagate Technology")
        source.manufacturer_source = "trio_source"
        source.manufacturer_confidence = 0.9
        source.manufacturer_tier = 95
        source.brand = "IBM"
        source.brand_source = "desc_parse"
        source.brand_confidence = 0.85
        source.brand_tier = 83
        target = _make_material_card(db_session, "prov-tgt", manufacturer="IBM")  # legacy, NULL provenance
        db_session.flush()

        merge_material_cards(db_session, source.id, target.id, "admin@test.com")
        db_session.commit()

        db_session.refresh(target)
        assert target.manufacturer == "Seagate Technology"  # 95 beat the legacy-50 OEM value
        assert target.manufacturer_source == "trio_source"
        assert target.manufacturer_tier == 95
        assert target.manufacturer_confidence == 0.9
        assert target.brand == "IBM"  # brand is carried, not dropped with the card
        assert target.brand_source == "desc_parse"
        assert target.brand_tier == 83

    @patch("app.services.audit_service.log_audit")
    def test_merge_does_not_let_legacy_source_clobber_higher_tier_target(self, mock_audit, db_session: Session):
        # Reverse arbitration: the TARGET holds a manual (100) correction; the source's
        # unprovenanced value (floor 50) must lose — the ladder owns the decision in
        # both directions, not fill-when-empty semantics.
        source = _make_material_card(db_session, "rev-src", manufacturer="IBM")
        target = _make_material_card(db_session, "rev-tgt", manufacturer="Kingston Technology")
        target.manufacturer_source = "manual"
        target.manufacturer_confidence = 1.0
        target.manufacturer_tier = 100
        db_session.flush()

        merge_material_cards(db_session, source.id, target.id, "admin@test.com")
        db_session.commit()

        db_session.refresh(target)
        assert target.manufacturer == "Kingston Technology"  # manual-100 resisted
        assert target.manufacturer_source == "manual"

    @patch("app.services.audit_service.log_audit")
    def test_merge_deletes_source_card(self, mock_audit, db_session: Session):
        source = _make_material_card(db_session, "del-src")
        target = _make_material_card(db_session, "del-tgt")
        source_id = source.id
        db_session.commit()

        merge_material_cards(db_session, source_id, target.id, "admin@test.com")
        db_session.commit()

        assert db_session.get(MaterialCard, source_id) is None
