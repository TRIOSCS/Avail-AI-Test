"""Tests for tagging data models — Tag, MaterialTag, EntityTag, TagThresholdConfig.

Verifies CRUD, unique constraints, cascade deletes, and seed data expectations.

Called by: pytest
Depends on: app.models.tags, tests.conftest (db_session, test_material_card)
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.tags import EntityTag, MaterialTag, Tag, TagThresholdConfig


# ── Helpers ────────────────────────────────────────────────────────────


def _make_tag(db, name="Texas Instruments", tag_type="brand"):
    t = Tag(name=name, tag_type=tag_type, created_at=datetime.now(timezone.utc))
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


# ── Tag CRUD ───────────────────────────────────────────────────────────


def test_tag_creation(db_session):
    brand = _make_tag(db_session, "NXP Semiconductors", "brand")
    commodity = _make_tag(db_session, "Capacitors", "commodity")

    assert brand.id is not None
    assert brand.name == "NXP Semiconductors"
    assert brand.tag_type == "brand"
    assert brand.created_at is not None

    assert commodity.tag_type == "commodity"
    assert commodity.name == "Capacitors"


def test_tag_unique_constraint(db_session):
    _make_tag(db_session, "Texas Instruments", "brand")
    with pytest.raises(IntegrityError):
        _make_tag(db_session, "Texas Instruments", "brand")


def test_tag_same_name_different_type(db_session):
    """Same name with different tag_type should be allowed."""
    _make_tag(db_session, "Sensors", "brand")
    _make_tag(db_session, "Sensors", "commodity")
    tags = db_session.query(Tag).filter_by(name="Sensors").all()
    assert len(tags) == 2


# ── MaterialTag ────────────────────────────────────────────────────────


def test_material_tag_creation(db_session, test_material_card):
    tag = _make_tag(db_session, "Texas Instruments", "brand")
    mt = MaterialTag(
        material_card_id=test_material_card.id,
        tag_id=tag.id,
        confidence=0.95,
        source="existing_data",
        classified_at=datetime.now(timezone.utc),
    )
    db_session.add(mt)
    db_session.commit()
    db_session.refresh(mt)

    assert mt.id is not None
    assert mt.tag.name == "Texas Instruments"
    assert mt.confidence == 0.95
    assert mt.source == "existing_data"


def test_material_tag_unique_constraint(db_session, test_material_card):
    tag = _make_tag(db_session)
    mt1 = MaterialTag(
        material_card_id=test_material_card.id,
        tag_id=tag.id,
        confidence=0.9,
        source="prefix_lookup",
    )
    db_session.add(mt1)
    db_session.commit()

    mt2 = MaterialTag(
        material_card_id=test_material_card.id,
        tag_id=tag.id,
        confidence=0.95,
        source="existing_data",
    )
    db_session.add(mt2)
    with pytest.raises(IntegrityError):
        db_session.commit()


# ── EntityTag ──────────────────────────────────────────────────────────


def test_entity_tag_creation(db_session):
    tag = _make_tag(db_session, "Murata", "brand")
    et = EntityTag(
        entity_type="company",
        entity_id=999,
        tag_id=tag.id,
        interaction_count=5.0,
        total_entity_interactions=20.0,
        is_visible=True,
        first_seen_at=datetime.now(timezone.utc),
        last_seen_at=datetime.now(timezone.utc),
    )
    db_session.add(et)
    db_session.commit()
    db_session.refresh(et)

    assert et.id is not None
    assert et.entity_type == "company"
    assert et.entity_id == 999
    assert et.interaction_count == 5.0
    assert et.is_visible is True


def test_entity_tag_unique_constraint(db_session):
    tag = _make_tag(db_session)
    et1 = EntityTag(entity_type="vendor_card", entity_id=1, tag_id=tag.id)
    db_session.add(et1)
    db_session.commit()

    et2 = EntityTag(entity_type="vendor_card", entity_id=1, tag_id=tag.id)
    db_session.add(et2)
    with pytest.raises(IntegrityError):
        db_session.commit()


# ── Cascade Deletes ────────────────────────────────────────────────────


def test_tag_cascade_delete_material_tags(db_session, test_material_card):
    tag = _make_tag(db_session, "ADI", "brand")
    mt = MaterialTag(
        material_card_id=test_material_card.id,
        tag_id=tag.id,
        confidence=0.9,
        source="prefix_lookup",
    )
    db_session.add(mt)
    db_session.commit()

    assert db_session.query(MaterialTag).count() == 1
    db_session.delete(tag)
    db_session.commit()
    assert db_session.query(MaterialTag).count() == 0


def test_tag_cascade_delete_entity_tags(db_session):
    tag = _make_tag(db_session, "Infineon", "brand")
    et = EntityTag(entity_type="company", entity_id=42, tag_id=tag.id)
    db_session.add(et)
    db_session.commit()

    assert db_session.query(EntityTag).count() == 1
    db_session.delete(tag)
    db_session.commit()
    assert db_session.query(EntityTag).count() == 0


# ── TagThresholdConfig ─────────────────────────────────────────────────


def test_threshold_config_creation(db_session):
    cfg = TagThresholdConfig(
        entity_type="vendor",
        tag_type="brand",
        min_count=2,
        min_percentage=0.05,
    )
    db_session.add(cfg)
    db_session.commit()
    db_session.refresh(cfg)

    assert cfg.id is not None
    assert cfg.min_count == 2
    assert cfg.min_percentage == 0.05


def test_threshold_config_unique_constraint(db_session):
    cfg1 = TagThresholdConfig(entity_type="vendor", tag_type="brand", min_count=2, min_percentage=0.05)
    db_session.add(cfg1)
    db_session.commit()

    cfg2 = TagThresholdConfig(entity_type="vendor", tag_type="brand", min_count=5, min_percentage=0.10)
    db_session.add(cfg2)
    with pytest.raises(IntegrityError):
        db_session.commit()


# ── Repr ───────────────────────────────────────────────────────────────


def test_tag_repr(db_session):
    tag = _make_tag(db_session, "Microchip", "brand")
    assert "Microchip" in repr(tag)
    assert "brand" in repr(tag)
