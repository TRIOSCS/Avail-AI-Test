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


# ── Admin Endpoints ────────────────────────────────────────────────────


def test_admin_status_endpoint(client, db_session):
    _make_card(db_session, "LM317T", manufacturer="Texas Instruments")
    seed_from_existing_manufacturers(db_session)

    resp = client.get("/api/admin/tagging/status")
    assert resp.status_code == 200
    data = resp.json()
    assert "total_material_cards" in data
    assert "tagged_count" in data
    assert "coverage_percentage" in data
    assert "top_brands" in data


def test_admin_backfill_endpoint(client):
    resp = client.post("/api/admin/tagging/backfill")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True


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


def test_admin_purge_unknown_endpoint(client):
    resp = client.post("/api/admin/tagging/purge-unknown")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True


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


def test_admin_analyze_prefixes_endpoint(client, db_session):
    _make_card(db_session, "NEWPREFIX001")

    resp = client.get("/api/admin/tagging/analyze-prefixes")
    assert resp.status_code == 200
    data = resp.json()
    assert "candidates" in data
