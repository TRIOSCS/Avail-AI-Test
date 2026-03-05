"""Tests for tagging API endpoints — tag listing, entity tags, material card tags.

Called by: pytest
Depends on: app.routers.tags, app.routers.tagging_admin, app.models.tags
"""

from datetime import datetime, timezone

from app.models.tags import EntityTag, MaterialTag, Tag

# ── Helpers ────────────────────────────────────────────────────────────


def _make_tag(db, name="Texas Instruments", tag_type="brand"):
    t = Tag(name=name, tag_type=tag_type, created_at=datetime.now(timezone.utc))
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def _make_material_tag(db, material_card_id, tag_id, source="existing_data", confidence=0.95):
    mt = MaterialTag(
        material_card_id=material_card_id,
        tag_id=tag_id,
        confidence=confidence,
        source=source,
        classified_at=datetime.now(timezone.utc),
    )
    db.add(mt)
    db.commit()
    db.refresh(mt)
    return mt


def _make_entity_tag(db, entity_type, entity_id, tag_id, count=5.0, visible=True):
    et = EntityTag(
        entity_type=entity_type,
        entity_id=entity_id,
        tag_id=tag_id,
        interaction_count=count,
        total_entity_interactions=count,
        is_visible=visible,
        first_seen_at=datetime.now(timezone.utc),
        last_seen_at=datetime.now(timezone.utc),
    )
    db.add(et)
    db.commit()
    db.refresh(et)
    return et


# ── GET /api/tags ──────────────────────────────────────────────────────


def test_get_tags_returns_all(client, db_session):
    _make_tag(db_session, "NXP", "brand")
    _make_tag(db_session, "Capacitors", "commodity")

    resp = client.get("/api/tags/")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 2
    names = {t["name"] for t in data["items"]}
    assert "NXP" in names
    assert "Capacitors" in names


def test_get_tags_filter_by_type(client, db_session):
    _make_tag(db_session, "TI Brand", "brand")
    _make_tag(db_session, "Resistors", "commodity")

    resp = client.get("/api/tags/?tag_type=brand")
    assert resp.status_code == 200
    data = resp.json()
    for item in data["items"]:
        assert item["tag_type"] == "brand"


# ── GET /api/tags/{tag_id}/entities ────────────────────────────────────


def test_get_tag_entities_visible_only(client, db_session):
    tag = _make_tag(db_session, "Murata", "brand")
    _make_entity_tag(db_session, "vendor_card", 1, tag.id, visible=True)
    _make_entity_tag(db_session, "vendor_card", 2, tag.id, visible=False)

    resp = client.get(f"/api/tags/{tag.id}/entities")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["items"][0]["entity_id"] == 1


# ── GET /api/tags/entities/{entity_type}/{entity_id} ───────────────────


def test_get_entity_tags_sorted_by_count(client, db_session):
    tag1 = _make_tag(db_session, "Small Brand", "brand")
    tag2 = _make_tag(db_session, "Big Brand", "brand")
    _make_entity_tag(db_session, "vendor_card", 1, tag1.id, count=5.0)
    _make_entity_tag(db_session, "vendor_card", 1, tag2.id, count=50.0)

    resp = client.get("/api/tags/entities/vendor_card/1")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    assert data[0]["tag"]["name"] == "Big Brand"  # Higher count first
    assert data[1]["tag"]["name"] == "Small Brand"


# ── GET /api/tags/material-cards/{material_card_id} ────────────────────


def test_get_material_card_tags(client, db_session, test_material_card):
    tag = _make_tag(db_session)
    _make_material_tag(db_session, test_material_card.id, tag.id)

    resp = client.get(f"/api/tags/material-cards/{test_material_card.id}")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["tag"]["name"] == "Texas Instruments"
    assert data[0]["confidence"] == 0.95
    assert data[0]["source"] == "existing_data"


def test_get_material_card_tags_empty(client, db_session, test_material_card):
    resp = client.get(f"/api/tags/material-cards/{test_material_card.id}")
    assert resp.status_code == 200
    assert resp.json() == []


def test_get_material_card_tags_hides_low_confidence(client, db_session, test_material_card):
    """Tags with confidence < 0.7 are hidden from the API response."""
    tag = _make_tag(db_session)
    _make_material_tag(db_session, test_material_card.id, tag.id, confidence=0.3)

    resp = client.get(f"/api/tags/material-cards/{test_material_card.id}")
    assert resp.status_code == 200
    assert resp.json() == []


# ── Vendor detail includes tags ────────────────────────────────────────


def test_vendor_detail_includes_tags(client, db_session, test_vendor_card):
    tag = _make_tag(db_session, "Arrow Brand", "brand")
    _make_entity_tag(db_session, "vendor_card", test_vendor_card.id, tag.id)

    resp = client.get(f"/api/vendors/{test_vendor_card.id}")
    assert resp.status_code == 200
    data = resp.json()
    assert "tags" in data
    assert len(data["tags"]) == 1
    assert data["tags"][0]["tag_name"] == "Arrow Brand"


# ── Company detail includes tags ───────────────────────────────────────


def test_company_detail_includes_tags(client, db_session, test_company):
    tag = _make_tag(db_session, "Semiconductor Co", "brand")
    _make_entity_tag(db_session, "company", test_company.id, tag.id)

    resp = client.get(f"/api/companies/{test_company.id}")
    assert resp.status_code == 200
    data = resp.json()
    assert "tags" in data
    assert len(data["tags"]) == 1
    assert data["tags"][0]["tag_name"] == "Semiconductor Co"
