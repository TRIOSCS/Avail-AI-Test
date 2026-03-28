"""Tests for tagging backfill service and admin endpoints.

Called by: pytest
Depends on: app.services.tagging_backfill, app.routers.tagging_admin, app.models
"""

from datetime import datetime, timezone

from app.models.intelligence import MaterialCard
from app.models.tags import MaterialTag, Tag
from app.services.tagging_backfill import run_prefix_backfill, seed_from_existing_manufacturers

# ── Helpers ────────────────────────────────────────────────────────────


def _seed_commodity_tags(db):
    """Seed commodity taxonomy tags for tests that need them."""
    from datetime import timezone as tz

    for name in ["Power Management ICs", "Capacitors", "Microcontrollers (MCU)", "Miscellaneous"]:
        db.add(Tag(name=name, tag_type="commodity", created_at=datetime.now(tz.utc)))
    db.commit()


def _make_card(db, mpn, manufacturer=None, category=None):
    card = MaterialCard(
        normalized_mpn=mpn.lower(),
        display_mpn=mpn,
        manufacturer=manufacturer,
        category=category,
        created_at=datetime.now(timezone.utc),
    )
    db.add(card)
    db.commit()
    db.refresh(card)
    return card


# ── seed_from_existing_manufacturers ───────────────────────────────────


def test_seed_from_existing_manufacturers(db_session):
    _make_card(db_session, "LM317T", manufacturer="Texas Instruments")
    _make_card(db_session, "STM32F4", manufacturer="STMicroelectronics")

    result = seed_from_existing_manufacturers(db_session)

    assert result["total_seeded"] == 2
    assert result["unique_brands_created"] == 2
    assert db_session.query(MaterialTag).count() == 2


def test_seed_idempotent(db_session):
    _make_card(db_session, "LM317T", manufacturer="Texas Instruments")

    result1 = seed_from_existing_manufacturers(db_session)
    result2 = seed_from_existing_manufacturers(db_session)

    assert result1["total_seeded"] == 1
    assert result2["total_seeded"] == 0
    assert db_session.query(MaterialTag).count() == 1


def test_seed_skips_empty_manufacturer(db_session):
    _make_card(db_session, "UNKNOWN1", manufacturer="")
    _make_card(db_session, "UNKNOWN2", manufacturer=None)

    result = seed_from_existing_manufacturers(db_session)
    assert result["total_seeded"] == 0


def test_seed_with_category(db_session):
    _seed_commodity_tags(db_session)
    _make_card(db_session, "LM317T", manufacturer="TI", category="Voltage Regulator")

    result = seed_from_existing_manufacturers(db_session)
    assert result["total_seeded"] == 1

    # Should have both brand and commodity tags
    tags = db_session.query(MaterialTag).all()
    assert len(tags) == 2
    tag_types = {db_session.get(Tag, mt.tag_id).tag_type for mt in tags}
    assert "brand" in tag_types
    assert "commodity" in tag_types


# ── run_prefix_backfill ────────────────────────────────────────────────


def test_prefix_backfill_processes_untagged(db_session):
    _make_card(db_session, "TPS65217")  # TPS → Texas Instruments
    _make_card(db_session, "ATMEGA328P")  # ATMEGA → Microchip

    result = run_prefix_backfill(db_session)

    assert result["total_processed"] == 2
    assert result["total_matched"] == 2
    assert result["total_unmatched"] == 0


def test_prefix_backfill_skips_already_tagged(db_session):
    card = _make_card(db_session, "TPS65217")

    # Tag manually
    tag = Tag(name="TI", tag_type="brand", created_at=datetime.now(timezone.utc))
    db_session.add(tag)
    db_session.flush()
    mt = MaterialTag(material_card_id=card.id, tag_id=tag.id, confidence=0.9, source="manual")
    db_session.add(mt)
    db_session.commit()

    result = run_prefix_backfill(db_session)
    assert result["total_processed"] == 0


def test_prefix_backfill_batch_boundaries(db_session):
    """Process across multiple batches."""
    for i in range(5):
        _make_card(db_session, f"TPS{65000 + i}")

    result = run_prefix_backfill(db_session, batch_size=2)

    assert result["total_processed"] == 5
    assert result["total_matched"] == 5


def test_backfill_empty_database(db_session):
    result = run_prefix_backfill(db_session)

    assert result["total_processed"] == 0
    assert result["total_matched"] == 0
    assert result["total_unmatched"] == 0


def test_prefix_backfill_unmatched_parts(db_session):
    _make_card(db_session, "ZZZXYZ123")  # No prefix match

    result = run_prefix_backfill(db_session)

    assert result["total_processed"] == 1
    assert result["total_matched"] == 0
    assert result["total_unmatched"] == 1


# ── purge_unknown_tags ─────────────────────────────────────────────────


def test_purge_unknown_tags_removes_low_confidence(db_session):
    """Purge deletes Unknown brand tags at <=0.30 confidence."""
    from app.services.tagging_backfill import purge_unknown_tags

    card = _make_card(db_session, "INTERNAL001")
    unknown_tag = Tag(name="Unknown", tag_type="brand", created_at=datetime.now(timezone.utc))
    db_session.add(unknown_tag)
    db_session.flush()
    mt = MaterialTag(material_card_id=card.id, tag_id=unknown_tag.id, confidence=0.30, source="ai_classified")
    db_session.add(mt)
    db_session.commit()

    result = purge_unknown_tags(db_session)

    assert result["total_purged"] == 1
    assert result["tag_deleted"] is True
    assert db_session.query(MaterialTag).count() == 0
    assert db_session.query(Tag).filter(Tag.name == "Unknown", Tag.tag_type == "brand").first() is None


def test_purge_unknown_tags_keeps_higher_confidence(db_session):
    """Purge does NOT delete Unknown tags above 0.30 confidence."""
    from app.services.tagging_backfill import purge_unknown_tags

    card = _make_card(db_session, "REALPART001")
    unknown_tag = Tag(name="Unknown", tag_type="brand", created_at=datetime.now(timezone.utc))
    db_session.add(unknown_tag)
    db_session.flush()
    mt = MaterialTag(material_card_id=card.id, tag_id=unknown_tag.id, confidence=0.50, source="manual")
    db_session.add(mt)
    db_session.commit()

    result = purge_unknown_tags(db_session)

    assert result["total_purged"] == 0
    assert result["tag_deleted"] is False
    assert db_session.query(MaterialTag).count() == 1


def test_purge_unknown_tags_no_tag_exists(db_session):
    """Purge handles case where no Unknown brand tag exists."""
    from app.services.tagging_backfill import purge_unknown_tags

    result = purge_unknown_tags(db_session)

    assert result["total_purged"] == 0
    assert result["tag_deleted"] is False


def test_purge_unknown_tags_batch_processing(db_session):
    """Purge processes in batches correctly."""
    from app.services.tagging_backfill import purge_unknown_tags

    unknown_tag = Tag(name="Unknown", tag_type="brand", created_at=datetime.now(timezone.utc))
    db_session.add(unknown_tag)
    db_session.flush()

    for i in range(5):
        card = _make_card(db_session, f"JUNK{i:03d}")
        mt = MaterialTag(material_card_id=card.id, tag_id=unknown_tag.id, confidence=0.30, source="ai_classified")
        db_session.add(mt)
    db_session.commit()

    result = purge_unknown_tags(db_session, batch_size=2)

    assert result["total_purged"] == 5
    assert result["tag_deleted"] is True


# ── analyze_untagged_prefixes ──────────────────────────────────────────


def test_analyze_untagged_prefixes(db_session):
    from app.services.tagging_backfill import analyze_untagged_prefixes

    # Create cards with unmatched prefix patterns
    for i in range(10):
        _make_card(db_session, f"ZZTOP{i:03d}")
    for i in range(3):
        _make_card(db_session, f"RAREPART{i}")

    results = analyze_untagged_prefixes(db_session)

    # ZZTOP should appear (10 occurrences > 5 threshold)
    prefixes = [r["prefix"] for r in results]
    assert any("ZZ" in p for p in prefixes)


def test_analyze_untagged_prefixes_empty(db_session):
    from app.services.tagging_backfill import analyze_untagged_prefixes

    results = analyze_untagged_prefixes(db_session)
    assert results == []


# ═══════════════════════════════════════════════════════════════════════
#  backfill_manufacturer_from_sightings — lines 204-299
# ═══════════════════════════════════════════════════════════════════════


def test_backfill_mfr_sightings_no_untagged(db_session):
    """No untagged cards → early return with zeros."""
    from app.services.tagging_backfill import backfill_manufacturer_from_sightings

    result = backfill_manufacturer_from_sightings(db_session)
    assert result == {"total_processed": 0, "total_tagged": 0, "total_skipped": 0}


def test_backfill_mfr_sightings_consensus_3_plus(db_session, test_requisition):
    """3+ sightings same manufacturer → sighting_consensus at 0.95."""
    from app.services.tagging_backfill import backfill_manufacturer_from_sightings

    card = _make_card(db_session, "BF-CONSENSUS-3")
    req_item = test_requisition.requirements[0]

    for i in range(3):
        from app.models.sourcing import Sighting

        s = Sighting(
            requirement_id=req_item.id,
            material_card_id=card.id,
            vendor_name=f"Vendor{i}",
            manufacturer="Texas Instruments",
            mpn_matched="BF-CONSENSUS-3",
            source_type="test",
        )
        db_session.add(s)
    db_session.flush()

    result = backfill_manufacturer_from_sightings(db_session)
    assert result["total_tagged"] == 1
    assert result["total_skipped"] == 0
    db_session.refresh(card)
    assert card.manufacturer == "Texas Instruments"


def test_backfill_mfr_sightings_2_agree(db_session, test_requisition):
    """2 sightings same manufacturer → sighting_consensus at 0.90."""
    from app.services.tagging_backfill import backfill_manufacturer_from_sightings

    card = _make_card(db_session, "BF-TWOVOTE")
    req_item = test_requisition.requirements[0]

    for i in range(2):
        from app.models.sourcing import Sighting

        s = Sighting(
            requirement_id=req_item.id,
            material_card_id=card.id,
            vendor_name=f"Vendor{i}",
            manufacturer="Analog Devices",
            mpn_matched="BF-TWOVOTE",
            source_type="test",
        )
        db_session.add(s)
    db_session.flush()

    result = backfill_manufacturer_from_sightings(db_session)
    assert result["total_tagged"] == 1


def test_backfill_mfr_sightings_single_skipped(db_session, test_requisition):
    """Single sighting with single distinct source → skipped (below 0.90 floor)."""
    from app.services.tagging_backfill import backfill_manufacturer_from_sightings

    card = _make_card(db_session, "BF-SINGLE")
    req_item = test_requisition.requirements[0]

    from app.models.sourcing import Sighting

    s = Sighting(
        requirement_id=req_item.id,
        material_card_id=card.id,
        vendor_name="Vendor1",
        manufacturer="OnSemi",
        mpn_matched="BF-SINGLE",
        source_type="test",
    )
    db_session.add(s)
    db_session.flush()

    result = backfill_manufacturer_from_sightings(db_session)
    assert result["total_skipped"] == 1
    assert result["total_tagged"] == 0


def test_backfill_mfr_sightings_junk_filtered(db_session, test_requisition):
    """Junk manufacturers ('Unknown', 'N/A', etc.) are filtered out."""
    from app.services.tagging_backfill import backfill_manufacturer_from_sightings

    card = _make_card(db_session, "BF-JUNK")
    req_item = test_requisition.requirements[0]

    from app.models.sourcing import Sighting

    for junk in ["Unknown", "N/A", "Various"]:
        s = Sighting(
            requirement_id=req_item.id,
            material_card_id=card.id,
            vendor_name="Vendor",
            manufacturer=junk,
            mpn_matched="BF-JUNK",
            source_type="test",
        )
        db_session.add(s)
    db_session.flush()

    result = backfill_manufacturer_from_sightings(db_session)
    assert result["total_skipped"] == 1


def test_backfill_mfr_sightings_no_sightings(db_session):
    """Card with no sightings → skipped."""
    from app.services.tagging_backfill import backfill_manufacturer_from_sightings

    _make_card(db_session, "BF-NOSIGHT")

    result = backfill_manufacturer_from_sightings(db_session)
    assert result["total_skipped"] == 1


def test_backfill_mfr_sightings_keeps_existing_manufacturer(db_session, test_requisition):
    """If card.manufacturer already set, don't overwrite."""
    from app.services.tagging_backfill import backfill_manufacturer_from_sightings

    card = _make_card(db_session, "BF-KEEPMFR", manufacturer="Original Corp")
    req_item = test_requisition.requirements[0]

    from app.models.sourcing import Sighting

    for i in range(3):
        s = Sighting(
            requirement_id=req_item.id,
            material_card_id=card.id,
            vendor_name=f"Vendor{i}",
            manufacturer="Different Corp",
            mpn_matched="BF-KEEPMFR",
            source_type="test",
        )
        db_session.add(s)
    db_session.flush()

    backfill_manufacturer_from_sightings(db_session)
    db_session.refresh(card)
    assert card.manufacturer == "Original Corp"


def test_backfill_mfr_sightings_distinct_sources_triggers_consensus(db_session, test_requisition):
    """2+ distinct manufacturers (even with count=1 each) → sighting_consensus at
    0.90."""
    from app.services.tagging_backfill import backfill_manufacturer_from_sightings

    card = _make_card(db_session, "BF-MULTISRC")
    req_item = test_requisition.requirements[0]

    from app.models.sourcing import Sighting

    s1 = Sighting(
        requirement_id=req_item.id,
        material_card_id=card.id,
        vendor_name="VendorA",
        manufacturer="TI",
        mpn_matched="BF-MULTISRC",
        source_type="test",
    )
    s2 = Sighting(
        requirement_id=req_item.id,
        material_card_id=card.id,
        vendor_name="VendorB",
        manufacturer="Analog Devices",
        mpn_matched="BF-MULTISRC",
        source_type="test",
    )
    db_session.add_all([s1, s2])
    db_session.flush()

    result = backfill_manufacturer_from_sightings(db_session)
    assert result["total_tagged"] == 1


def test_backfill_mfr_sightings_batch_processing(db_session, test_requisition):
    """Processes cards across batch boundaries."""
    from app.services.tagging_backfill import backfill_manufacturer_from_sightings

    req_item = test_requisition.requirements[0]

    from app.models.sourcing import Sighting

    for i in range(3):
        card = _make_card(db_session, f"BF-BATCH{i}")
        for j in range(3):
            s = Sighting(
                requirement_id=req_item.id,
                material_card_id=card.id,
                vendor_name=f"Vendor{j}",
                manufacturer="TI",
                mpn_matched=f"BF-BATCH{i}",
                source_type="test",
            )
            db_session.add(s)
    db_session.flush()

    result = backfill_manufacturer_from_sightings(db_session, batch_size=2)
    assert result["total_processed"] == 3
    assert result["total_tagged"] == 3


# ═══════════════════════════════════════════════════════════════════════
#  repair_entity_tag_visibility — lines 414-442
# ═══════════════════════════════════════════════════════════════════════


def test_repair_visibility_no_entity_tags(db_session):
    """No entity tags → early return with zeros."""
    from app.services.tagging_backfill import repair_entity_tag_visibility

    result = repair_entity_tag_visibility(db_session)
    assert result == {"total_entities": 0, "total_tags_updated": 0, "now_visible": 0, "now_hidden": 0}


def test_repair_visibility_processes_entities(db_session):
    """All distinct entity (type, id) pairs are recalculated."""
    from unittest.mock import patch

    from app.models.tags import EntityTag
    from app.services.tagging_backfill import repair_entity_tag_visibility

    tag = Tag(name="RepairBrand", tag_type="brand", created_at=datetime.now(timezone.utc))
    db_session.add(tag)
    db_session.flush()

    et1 = EntityTag(
        entity_type="vendor_card",
        entity_id=1,
        tag_id=tag.id,
        interaction_count=5,
        total_entity_interactions=10,
        is_visible=False,
    )
    et2 = EntityTag(
        entity_type="company",
        entity_id=2,
        tag_id=tag.id,
        interaction_count=3,
        total_entity_interactions=10,
        is_visible=True,
    )
    db_session.add_all([et1, et2])
    db_session.commit()

    with patch("app.services.tagging.recalculate_entity_tag_visibility") as mock_recalc:
        result = repair_entity_tag_visibility(db_session)

    assert result["total_entities"] == 2
    assert mock_recalc.call_count == 2


def test_repair_visibility_counts_visible_hidden(db_session):
    """Result includes now_visible and now_hidden counts."""
    from unittest.mock import patch

    from app.models.tags import EntityTag
    from app.services.tagging_backfill import repair_entity_tag_visibility

    tag = Tag(name="CountBrand", tag_type="brand", created_at=datetime.now(timezone.utc))
    db_session.add(tag)
    db_session.flush()

    # Create 2 visible, 1 hidden entity tag
    for i, visible in enumerate([True, True, False]):
        et = EntityTag(
            entity_type="vendor_card",
            entity_id=i + 10,
            tag_id=tag.id,
            interaction_count=5,
            total_entity_interactions=10,
            is_visible=visible,
        )
        db_session.add(et)
    db_session.commit()

    with patch("app.services.tagging.recalculate_entity_tag_visibility"):
        result = repair_entity_tag_visibility(db_session)

    assert result["total_entities"] == 3
    assert result["now_visible"] == 2
    assert result["now_hidden"] == 1
    assert result["total_tags_updated"] == 3
