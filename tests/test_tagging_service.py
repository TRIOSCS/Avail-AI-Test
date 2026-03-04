"""Tests for tagging classification service — prefix lookup, waterfall, upsert, visibility.

Called by: pytest
Depends on: app.services.tagging, app.services.prefix_lookup, app.models.tags
"""

from datetime import datetime, timezone

from app.models.tags import EntityTag, MaterialTag, Tag, TagThresholdConfig
from app.services.prefix_lookup import lookup_manufacturer_by_prefix
from app.services.tagging import (
    classify_material_card,
    get_or_create_brand_tag,
    propagate_tags_to_entity,
    recalculate_entity_tag_visibility,
    tag_material_card,
)


# ── Helpers ────────────────────────────────────────────────────────────


def _make_tag(db, name="Texas Instruments", tag_type="brand"):
    t = Tag(name=name, tag_type=tag_type, created_at=datetime.now(timezone.utc))
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def _seed_thresholds(db):
    """Seed default threshold config rows (entity_types match propagate_tags_to_entity)."""
    for et, tt, mc, mp in [
        ("vendor_card", "brand", 2, 0.05),
        ("vendor_card", "commodity", 3, 0.05),
        ("customer_site", "brand", 3, 0.05),
        ("customer_site", "commodity", 3, 0.05),
        ("company", "brand", 2, 0.05),
        ("company", "commodity", 3, 0.05),
    ]:
        db.add(TagThresholdConfig(entity_type=et, tag_type=tt, min_count=mc, min_percentage=mp))
    db.commit()


# ── Prefix Lookup ──────────────────────────────────────────────────────


def test_prefix_lookup_known_prefix():
    mfr, conf = lookup_manufacturer_by_prefix("tps65217")
    assert mfr == "Texas Instruments"
    assert conf == 0.9


def test_prefix_lookup_long_prefix():
    mfr, conf = lookup_manufacturer_by_prefix("atmega328p")
    assert mfr == "Microchip Technology"
    assert conf == 0.9


def test_prefix_lookup_unknown():
    mfr, conf = lookup_manufacturer_by_prefix("zzzxxx123")
    assert mfr is None
    assert conf == 0.0


def test_prefix_lookup_short_prefix_skipped():
    """2-char prefixes are now skipped (below min confidence floor)."""
    mfr, conf = lookup_manufacturer_by_prefix("ad5292")
    # AD is a 2-char prefix — now returns None (skip) instead of 0.70
    # But ADM/ADP/ADG are 3-char, so "adm..." would match. "ad5292" has no 3+ match.
    assert mfr is None
    assert conf == 0.0


def test_prefix_lookup_stm32():
    mfr, conf = lookup_manufacturer_by_prefix("stm32f407vgt6")
    assert mfr == "STMicroelectronics"
    assert conf == 0.9


def test_prefix_lookup_most_specific_wins():
    """STM32 prefix (5 chars) should beat ST prefix (2 chars)."""
    mfr, _ = lookup_manufacturer_by_prefix("stm32l476rg")
    assert mfr == "STMicroelectronics"


def test_prefix_lookup_nordic():
    mfr, conf = lookup_manufacturer_by_prefix("nrf52840")
    assert mfr == "Nordic Semiconductor"
    assert conf == 0.9


def test_prefix_lookup_espressif():
    mfr, conf = lookup_manufacturer_by_prefix("esp32s3")
    assert mfr == "Espressif Systems"
    assert conf == 0.9


def test_prefix_lookup_ftdi_2char_skipped():
    """FT is a 2-char prefix — now skipped (below min confidence floor)."""
    mfr, conf = lookup_manufacturer_by_prefix("ft232r")
    assert mfr is None
    assert conf == 0.0


def test_prefix_lookup_silicon_labs_2char_skipped():
    """SI is a 2-char prefix — now skipped (below min confidence floor)."""
    mfr, conf = lookup_manufacturer_by_prefix("si5351")
    assert mfr is None
    assert conf == 0.0


def test_new_prefix_entries():
    """Verify new prefix entries from Phase 2 expansion match correctly."""
    from app.services.prefix_lookup import lookup_manufacturer_by_prefix

    # Renesas
    mfr, conf = lookup_manufacturer_by_prefix("R7FA2E1A93CFM")
    assert mfr == "Renesas Electronics"

    # Bourns
    mfr, conf = lookup_manufacturer_by_prefix("SRR1260A-100M")
    assert mfr == "Bourns"

    # GigaDevice
    mfr, conf = lookup_manufacturer_by_prefix("GD25Q128CSIG")
    assert mfr == "GigaDevice"

    # Monolithic Power — MP is 2-char prefix, now skipped
    mfr, conf = lookup_manufacturer_by_prefix("MP2315GJ")
    assert mfr is None  # 2-char prefix below confidence floor

    # Realtek
    mfr, conf = lookup_manufacturer_by_prefix("RTL8211F")
    assert mfr == "Realtek"

    # Semtech
    mfr, conf = lookup_manufacturer_by_prefix("SX1276IMLTRT")
    assert mfr == "Semtech"

    # Macronix
    mfr, conf = lookup_manufacturer_by_prefix("MX25L12835F")
    assert mfr == "Macronix"


# ── classify_material_card ─────────────────────────────────────────────


def test_classify_with_existing_manufacturer():
    result = classify_material_card("lm317t", "NXP", None)
    assert result["brand"]["name"] == "NXP"
    assert result["brand"]["source"] == "existing_data"
    assert result["brand"]["confidence"] == 0.95


def test_classify_with_prefix_match():
    result = classify_material_card("tps65217", None, None)
    assert result["brand"]["name"] == "Texas Instruments"
    assert result["brand"]["source"] == "prefix_lookup"


def test_classify_no_match():
    result = classify_material_card("unknown123xyz", None, None)
    assert result["brand"] is None


def test_classify_with_category():
    result = classify_material_card("test123", "NXP", "Microcontroller")
    assert result["commodity"]["name"] == "Microcontrollers (MCU)"
    assert result["commodity"]["source"] == "existing_data"


def test_classify_category_substring_match():
    result = classify_material_card("test123", None, "3.3V LDO Voltage Regulator")
    assert result["commodity"]["name"] == "Power Management ICs"


def test_classify_unknown_category():
    result = classify_material_card("test123", None, "Something Weird")
    assert result["commodity"] is None


# ── get_or_create_brand_tag ────────────────────────────────────────────


def test_get_or_create_brand_tag_new(db_session):
    tag = get_or_create_brand_tag("Texas Instruments", db_session)
    db_session.commit()
    assert tag.id is not None
    assert tag.name == "Texas Instruments"
    assert tag.tag_type == "brand"


def test_get_or_create_brand_tag_dedup(db_session):
    tag1 = get_or_create_brand_tag("Texas Instruments", db_session)
    db_session.commit()
    tag2 = get_or_create_brand_tag("TEXAS INSTRUMENTS", db_session)
    db_session.commit()
    assert tag1.id == tag2.id


def test_get_or_create_brand_tag_different_brands(db_session):
    tag1 = get_or_create_brand_tag("NXP", db_session)
    db_session.commit()
    tag2 = get_or_create_brand_tag("STMicroelectronics", db_session)
    db_session.commit()
    assert tag1.id != tag2.id


# ── tag_material_card ──────────────────────────────────────────────────


def test_tag_material_card_creates_records(db_session, test_material_card):
    tag = _make_tag(db_session)
    result = tag_material_card(
        test_material_card.id,
        [{"tag_id": tag.id, "source": "existing_data", "confidence": 0.95}],
        db_session,
    )
    db_session.commit()
    assert len(result) == 1
    assert result[0].confidence == 0.95


def test_tag_material_card_upsert_higher_confidence_wins(db_session, test_material_card):
    tag = _make_tag(db_session)
    tag_material_card(
        test_material_card.id,
        [{"tag_id": tag.id, "source": "prefix_lookup", "confidence": 0.7}],
        db_session,
    )
    db_session.commit()

    tag_material_card(
        test_material_card.id,
        [{"tag_id": tag.id, "source": "existing_data", "confidence": 0.95}],
        db_session,
    )
    db_session.commit()

    mt = db_session.query(MaterialTag).filter_by(material_card_id=test_material_card.id).first()
    assert mt.confidence == 0.95
    assert mt.source == "existing_data"


def test_tag_material_card_upsert_lower_confidence_ignored(db_session, test_material_card):
    tag = _make_tag(db_session)
    tag_material_card(
        test_material_card.id,
        [{"tag_id": tag.id, "source": "existing_data", "confidence": 0.95}],
        db_session,
    )
    db_session.commit()

    tag_material_card(
        test_material_card.id,
        [{"tag_id": tag.id, "source": "prefix_lookup", "confidence": 0.7}],
        db_session,
    )
    db_session.commit()

    mt = db_session.query(MaterialTag).filter_by(material_card_id=test_material_card.id).first()
    assert mt.confidence == 0.95
    assert mt.source == "existing_data"


# ── recalculate_entity_tag_visibility ──────────────────────────────────


def test_visibility_both_gates_pass(db_session):
    _seed_thresholds(db_session)
    tag = _make_tag(db_session, "Murata", "brand")

    # vendor_card brand threshold: min_count=2, min_percentage=0.05
    et = EntityTag(entity_type="vendor_card", entity_id=1, tag_id=tag.id, interaction_count=5.0)
    db_session.add(et)
    db_session.commit()

    recalculate_entity_tag_visibility("vendor_card", 1, db_session)
    db_session.commit()

    db_session.refresh(et)
    assert et.is_visible is True
    assert et.total_entity_interactions == 5.0


def test_visibility_gate1_only(db_session):
    """Count passes but percentage fails → not visible."""
    _seed_thresholds(db_session)
    tag1 = _make_tag(db_session, "Small Brand", "brand")
    tag2 = _make_tag(db_session, "Big Brand", "brand")

    # vendor_card brand: min_count=2, min_percentage=0.05
    # small: 2 interactions, big: 98 → small is 2% which is < 5%
    et1 = EntityTag(entity_type="vendor_card", entity_id=1, tag_id=tag1.id, interaction_count=2.0)
    et2 = EntityTag(entity_type="vendor_card", entity_id=1, tag_id=tag2.id, interaction_count=98.0)
    db_session.add_all([et1, et2])
    db_session.commit()

    recalculate_entity_tag_visibility("vendor_card", 1, db_session)
    db_session.commit()

    db_session.refresh(et1)
    db_session.refresh(et2)
    assert et1.is_visible is False  # 2/100 = 2% < 5%
    assert et2.is_visible is True   # 98/100 = 98% >= 5%


def test_visibility_gate2_only(db_session):
    """Percentage passes but count fails → not visible."""
    _seed_thresholds(db_session)
    tag = _make_tag(db_session, "Rare Brand", "brand")

    # vendor_card brand: min_count=2, min_percentage=0.05
    # 1 interaction = 100% but count < 2
    et = EntityTag(entity_type="vendor_card", entity_id=1, tag_id=tag.id, interaction_count=1.0)
    db_session.add(et)
    db_session.commit()

    recalculate_entity_tag_visibility("vendor_card", 1, db_session)
    db_session.commit()

    db_session.refresh(et)
    assert et.is_visible is False  # count=1 < min_count=2


def test_visibility_respects_config(db_session):
    """Different thresholds per entity_type/tag_type."""
    _seed_thresholds(db_session)
    tag = _make_tag(db_session, "NXP", "brand")

    # customer_site brand: min_count=3, min_percentage=0.05
    et = EntityTag(entity_type="customer_site", entity_id=1, tag_id=tag.id, interaction_count=2.0)
    db_session.add(et)
    db_session.commit()

    recalculate_entity_tag_visibility("customer_site", 1, db_session)
    db_session.commit()

    db_session.refresh(et)
    assert et.is_visible is False  # count=2 < min_count=3 for customer_site/brand


def test_visibility_no_tags_no_error(db_session):
    """Empty entity → no error."""
    _seed_thresholds(db_session)
    recalculate_entity_tag_visibility("vendor_card", 999, db_session)


# ── propagate_tags_to_entity ───────────────────────────────────────────


def test_propagate_creates_entity_tags(db_session, test_material_card):
    _seed_thresholds(db_session)
    tag = _make_tag(db_session)
    tag_material_card(
        test_material_card.id,
        [{"tag_id": tag.id, "source": "existing_data", "confidence": 0.95}],
        db_session,
    )
    db_session.commit()

    propagate_tags_to_entity("vendor_card", 1, test_material_card.id, 1.0, db_session)
    db_session.commit()

    et = db_session.query(EntityTag).filter_by(entity_type="vendor_card", entity_id=1).first()
    assert et is not None
    assert et.interaction_count == 1.0
    assert et.first_seen_at is not None


def test_propagate_increments_existing(db_session, test_material_card):
    _seed_thresholds(db_session)
    tag = _make_tag(db_session)
    tag_material_card(
        test_material_card.id,
        [{"tag_id": tag.id, "source": "existing_data", "confidence": 0.95}],
        db_session,
    )
    db_session.commit()

    propagate_tags_to_entity("vendor_card", 1, test_material_card.id, 1.0, db_session)
    db_session.commit()
    propagate_tags_to_entity("vendor_card", 1, test_material_card.id, 1.0, db_session)
    db_session.commit()

    et = db_session.query(EntityTag).filter_by(entity_type="vendor_card", entity_id=1).first()
    assert et.interaction_count == 2.0


def test_propagate_with_weight_half(db_session, test_material_card):
    _seed_thresholds(db_session)
    tag = _make_tag(db_session)
    tag_material_card(
        test_material_card.id,
        [{"tag_id": tag.id, "source": "existing_data", "confidence": 0.95}],
        db_session,
    )
    db_session.commit()

    propagate_tags_to_entity("vendor_card", 1, test_material_card.id, 0.5, db_session)
    db_session.commit()

    et = db_session.query(EntityTag).filter_by(entity_type="vendor_card", entity_id=1).first()
    assert et.interaction_count == 0.5


def test_propagate_recalculates_visibility(db_session, test_material_card):
    _seed_thresholds(db_session)
    tag = _make_tag(db_session)
    tag_material_card(
        test_material_card.id,
        [{"tag_id": tag.id, "source": "existing_data", "confidence": 0.95}],
        db_session,
    )
    db_session.commit()

    # After 3 propagations with weight 1.0, vendor_card brand min_count=2 should be met
    for _ in range(3):
        propagate_tags_to_entity("vendor_card", 1, test_material_card.id, 1.0, db_session)
        db_session.commit()

    et = db_session.query(EntityTag).filter_by(entity_type="vendor_card", entity_id=1).first()
    assert et.is_visible is True


def test_propagate_untagged_material_no_error(db_session, test_material_card):
    """Material with no tags → no error, no entity_tags created."""
    propagate_tags_to_entity("vendor_card", 1, test_material_card.id, 1.0, db_session)
    db_session.commit()

    count = db_session.query(EntityTag).count()
    assert count == 0


def test_propagate_skips_low_confidence_tags(db_session, test_material_card):
    """Tags with confidence < 0.90 are not propagated to entities."""
    _seed_thresholds(db_session)
    tag = _make_tag(db_session)
    tag_material_card(
        test_material_card.id,
        [{"tag_id": tag.id, "source": "ai_classified", "confidence": 0.85}],
        db_session,
    )
    db_session.commit()

    propagate_tags_to_entity("vendor_card", 1, test_material_card.id, 1.0, db_session)
    db_session.commit()

    count = db_session.query(EntityTag).count()
    assert count == 0
