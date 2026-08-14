"""Tests for QC fix: raw ``card.manufacturer =`` writes routed through the provenance ladder.

Every writer that used to assign MaterialCard.manufacturer directly now goes through
set_manufacturer() with an honest source, so the value carries provenance instead of
silently ranking at the legacy_backfill floor. Also covers the two GET endpoints that
used to write-and-commit on read (they no longer write), and the ladder rule that a
blank existing value ("") is treated as absent rather than a legacy-50 value.

Called by: pytest
Depends on: conftest fixtures (client, db_session, test_requisition)
"""

from datetime import UTC, datetime
from unittest.mock import patch

from app.models.intelligence import MaterialCard
from app.services.material_card_service import backfill_missing_manufacturers
from app.services.spec_tiers import SOURCE_TIER, set_manufacturer
from app.services.tagging_ai_classify import _apply_ai_results
from app.utils.normalization import normalize_mpn_key


def _make_card(db, mpn, manufacturer=None):
    card = MaterialCard(
        normalized_mpn=normalize_mpn_key(mpn),
        display_mpn=mpn,
        manufacturer=manufacturer,
        created_at=datetime.now(UTC),
    )
    db.add(card)
    db.commit()
    db.refresh(card)
    return card


class TestNewSourcesRegistered:
    def test_new_ladder_sources_have_expected_tiers(self):
        assert SOURCE_TIER["form_entry"] == 70
        assert SOURCE_TIER["sighting_consensus"] == 65
        assert SOURCE_TIER["sighting_reported"] == 65
        assert SOURCE_TIER["prefix_infer"] == 40


class TestAiClassifyRoutesThroughLadder:
    def test_apply_ai_results_stamps_claude_haiku_provenance(self, db_session):
        card = _make_card(db_session, "STM32F103")
        classified = [{"mpn": "STM32F103", "manufacturer": "STMicroelectronics", "category": "Miscellaneous"}]

        matched, unknown = _apply_ai_results(classified, [(card.id, "stm32f103")], db_session)
        db_session.commit()
        db_session.refresh(card)

        assert matched == 1
        assert card.manufacturer == "STMicroelectronics"
        assert card.manufacturer_source == "claude_haiku"
        assert card.manufacturer_tier == 40
        assert abs(card.manufacturer_confidence - 0.92) < 1e-9

    def test_apply_ai_results_never_touches_existing_manufacturer(self, db_session):
        card = _make_card(db_session, "K4A8G085WB", manufacturer="Samsung")
        card.manufacturer_source = "manual"
        card.manufacturer_tier = 100
        card.manufacturer_confidence = 1.0
        db_session.commit()

        _apply_ai_results(
            [{"mpn": "K4A8G085WB", "manufacturer": "SK Hynix", "category": "Miscellaneous"}],
            [(card.id, "k4a8g085wb")],
            db_session,
        )
        db_session.commit()
        db_session.refresh(card)

        assert card.manufacturer == "Samsung"
        assert card.manufacturer_source == "manual"
        assert card.manufacturer_tier == 100


class TestSightingConsensusProvenance:
    def test_consensus_backfill_stamps_sighting_consensus(self, db_session, test_requisition):
        from app.models.sourcing import Sighting
        from app.services.tagging_backfill import backfill_manufacturer_from_sightings

        card = _make_card(db_session, "BF-PROV-3")
        req_item = test_requisition.requirements[0]
        for i in range(3):
            db_session.add(
                Sighting(
                    requirement_id=req_item.id,
                    material_card_id=card.id,
                    vendor_name=f"Vendor{i}",
                    manufacturer="Texas Instruments",
                    mpn_matched="BF-PROV-3",
                    source_type="test",
                )
            )
        db_session.flush()

        result = backfill_manufacturer_from_sightings(db_session)
        db_session.refresh(card)

        assert result["total_tagged"] == 1
        assert card.manufacturer == "Texas Instruments"
        assert card.manufacturer_source == "sighting_consensus"
        assert card.manufacturer_tier == 65
        assert abs(card.manufacturer_confidence - 0.95) < 1e-9


class TestBulkPrefixBackfill:
    def test_backfill_stamps_prefix_infer_and_fills_blank_string(self, db_session):
        null_card = _make_card(db_session, "PFX-NULL", manufacturer=None)
        blank_card = _make_card(db_session, "PFX-BLANK", manufacturer="")

        with patch("app.services.material_card_service.infer_manufacturer", return_value="Texas Instruments"):
            updated = backfill_missing_manufacturers(db_session)
        db_session.commit()
        db_session.refresh(null_card)
        db_session.refresh(blank_card)

        assert updated == 2
        for card in (null_card, blank_card):
            assert card.manufacturer == "Texas Instruments"
            assert card.manufacturer_source == "prefix_infer"
            assert card.manufacturer_tier == 40
            assert abs(card.manufacturer_confidence - 0.6) < 1e-9


class TestLadderBlankIsAbsent:
    def test_blank_existing_value_loses_to_any_real_write(self, db_session):
        card = _make_card(db_session, "BLANK-1", manufacturer="")
        assert set_manufacturer(card, "Fujitsu", source="prefix_infer", confidence=0.6) is True
        assert card.manufacturer == "Fujitsu"
        assert card.manufacturer_source == "prefix_infer"

    def test_real_unprovenanced_value_still_beats_tier_40(self, db_session):
        card = _make_card(db_session, "LEGACY-1", manufacturer="IBM")
        assert set_manufacturer(card, "Lenovo", source="prefix_infer", confidence=0.9) is False
        assert card.manufacturer == "IBM"
        assert card.manufacturer_source is None  # a losing write never stamps provenance


class TestGetEndpointsNoLongerWrite:
    def test_get_material_by_id_does_not_backfill(self, client, db_session):
        card = _make_card(db_session, "READONLY1")
        resp = client.get(f"/api/materials/{card.id}")
        db_session.refresh(card)

        assert resp.status_code == 200
        assert resp.json()["manufacturer"] is None
        assert card.manufacturer is None
        assert card.manufacturer_source is None

    def test_get_material_by_mpn_does_not_backfill(self, client, db_session):
        card = _make_card(db_session, "READONLY2")
        resp = client.get("/api/materials/by-mpn/READONLY2")
        db_session.refresh(card)

        assert resp.status_code == 200
        assert card.manufacturer is None
        assert card.manufacturer_source is None


class TestResolveMaterialCardFormEntry:
    def test_resolve_stamps_form_entry_on_existing_empty_card(self, db_session):
        from app.search_service import resolve_material_card

        card = _make_card(db_session, "FORM-1")
        resolved = resolve_material_card("FORM-1", db_session, manufacturer="Analog Devices")

        assert resolved.id == card.id
        assert resolved.manufacturer == "Analog Devices"
        assert resolved.manufacturer_source == "form_entry"
        assert resolved.manufacturer_tier == 70

    def test_resolve_does_not_downgrade_manual_value(self, db_session):
        from app.search_service import resolve_material_card

        card = _make_card(db_session, "FORM-2", manufacturer="Samsung")
        card.manufacturer_source = "manual"
        card.manufacturer_tier = 100
        card.manufacturer_confidence = 1.0
        db_session.commit()

        resolved = resolve_material_card("FORM-2", db_session, manufacturer="SK Hynix")
        assert resolved.manufacturer == "Samsung"
        assert resolved.manufacturer_source == "manual"


class TestUpsertSightingReported:
    def test_upsert_stamps_sighting_reported_and_skips_garbage(self, db_session, test_requisition):
        from app.models.sourcing import Sighting
        from app.search_service import _upsert_material_card

        card = _make_card(db_session, "UPS-1")
        req_item = test_requisition.requirements[0]
        garbage = Sighting(
            requirement_id=req_item.id,
            material_card_id=card.id,
            vendor_name="VendorA",
            manufacturer="LF(T",  # garbage fragment — ladder rejects, loop must try the next sighting
            mpn_matched="UPS-1",
            source_type="test",
        )
        good = Sighting(
            requirement_id=req_item.id,
            material_card_id=card.id,
            vendor_name="VendorB",
            manufacturer="Fujitsu",
            mpn_matched="UPS-1",
            source_type="test",
        )
        db_session.add_all([garbage, good])
        db_session.flush()

        result = _upsert_material_card("UPS-1", [garbage, good], db_session, datetime.now(UTC))
        db_session.commit()
        db_session.refresh(card)

        assert result.id == card.id
        assert card.manufacturer == "Fujitsu"
        assert card.manufacturer_source == "sighting_reported"
        assert card.manufacturer_tier == 65
