"""Tests for app/routers/tags.py — tag query endpoints.

Targets missing branches to bring coverage from 48% to 85%+.

Called by: pytest
Depends on: conftest.py fixtures, app.models.tags, app.routers.tags
"""

import os

os.environ["TESTING"] = "1"

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.intelligence import MaterialCard
from app.models.tags import EntityTag, MaterialTag, Tag
from tests.conftest import engine  # noqa: F401


def _make_tag(db: Session, name: str, tag_type: str = "brand") -> Tag:
    tag = Tag(name=name, tag_type=tag_type)
    db.add(tag)
    db.flush()
    return tag


class TestListTags:
    """Tests for GET /api/tags/ list endpoint."""

    def test_list_tags_returns_empty_items(self, client, db_session: Session):
        """Returns empty items list when no tags exist."""
        resp = client.get("/api/tags/")
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data
        assert data["total"] == 0
        assert data["items"] == []

    def test_list_tags_returns_created_tags(self, client, db_session: Session):
        """Returns all tags with correct structure."""
        _make_tag(db_session, "Texas Instruments", "brand")
        _make_tag(db_session, "Microcontroller", "commodity")
        db_session.commit()

        resp = client.get("/api/tags/")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        assert len(data["items"]) == 2
        names = {item["name"] for item in data["items"]}
        assert "Texas Instruments" in names
        assert "Microcontroller" in names

    def test_list_tags_filter_by_type(self, client, db_session: Session):
        """Filter tags by tag_type."""
        _make_tag(db_session, "Texas Instruments", "brand")
        _make_tag(db_session, "Microcontroller", "commodity")
        db_session.commit()

        resp = client.get("/api/tags/", params={"tag_type": "brand"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["name"] == "Texas Instruments"

    def test_list_tags_search_by_name(self, client, db_session: Session):
        """Search tags by name using q parameter."""
        _make_tag(db_session, "Texas Instruments", "brand")
        _make_tag(db_session, "NXP Semiconductors", "brand")
        db_session.commit()

        resp = client.get("/api/tags/", params={"q": "Texas"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["name"] == "Texas Instruments"

    def test_list_tags_pagination(self, client, db_session: Session):
        """Pagination via limit and offset."""
        for i in range(5):
            _make_tag(db_session, f"Tag-{i}", "brand")
        db_session.commit()

        resp = client.get("/api/tags/", params={"limit": 2, "offset": 0})
        assert resp.status_code == 200
        data = resp.json()
        assert data["limit"] == 2
        assert data["offset"] == 0
        assert len(data["items"]) == 2

    def test_list_tags_case_insensitive_search(self, client, db_session: Session):
        """Name search is case-insensitive."""
        _make_tag(db_session, "Capacitor", "commodity")
        db_session.commit()

        resp = client.get("/api/tags/", params={"q": "capacitor"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1


class TestGetTagEntities:
    """Tests for GET /api/tags/{tag_id}/entities endpoint."""

    def test_returns_empty_when_no_entities(self, client, db_session: Session):
        """Returns empty list when tag has no entity tags."""
        tag = _make_tag(db_session, "Resistor", "commodity")
        db_session.commit()

        resp = client.get(f"/api/tags/{tag.id}/entities")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["items"] == []

    def test_returns_visible_entity_tags(self, client, db_session: Session):
        """Returns only is_visible=True entity tags."""
        tag = _make_tag(db_session, "Capacitor", "commodity")
        db_session.commit()

        # Visible entity tag
        et_visible = EntityTag(
            entity_type="vendor_card",
            entity_id=1,
            tag_id=tag.id,
            interaction_count=5.0,
            total_entity_interactions=10.0,
            is_visible=True,
            first_seen_at=datetime.now(timezone.utc),
            last_seen_at=datetime.now(timezone.utc),
        )
        # Hidden entity tag
        et_hidden = EntityTag(
            entity_type="vendor_card",
            entity_id=2,
            tag_id=tag.id,
            interaction_count=1.0,
            total_entity_interactions=10.0,
            is_visible=False,
        )
        db_session.add_all([et_visible, et_hidden])
        db_session.commit()

        resp = client.get(f"/api/tags/{tag.id}/entities")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["entity_id"] == 1

    def test_entity_tag_response_fields(self, client, db_session: Session):
        """Entity tag response includes all expected fields."""
        tag = _make_tag(db_session, "Inductor", "commodity")
        db_session.commit()

        now = datetime.now(timezone.utc)
        et = EntityTag(
            entity_type="company",
            entity_id=42,
            tag_id=tag.id,
            interaction_count=8.0,
            total_entity_interactions=20.0,
            is_visible=True,
            first_seen_at=now,
            last_seen_at=now,
        )
        db_session.add(et)
        db_session.commit()

        resp = client.get(f"/api/tags/{tag.id}/entities")
        assert resp.status_code == 200
        data = resp.json()
        item = data["items"][0]
        assert item["entity_type"] == "company"
        assert item["entity_id"] == 42
        assert item["interaction_count"] == 8.0
        assert item["is_visible"] is True
        assert item["tag"]["name"] == "Inductor"


class TestGetEntityTags:
    """Tests for GET /api/tags/entities/{entity_type}/{entity_id} endpoint."""

    def test_returns_empty_for_unknown_entity(self, client, db_session: Session):
        """Returns empty list for an entity with no tags."""
        resp = client.get("/api/tags/entities/vendor_card/99999")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_returns_visible_tags_for_entity(self, client, db_session: Session):
        """Returns visible tags for a specific entity."""
        tag1 = _make_tag(db_session, "Analog Devices", "brand")
        tag2 = _make_tag(db_session, "IC", "commodity")
        db_session.commit()

        et1 = EntityTag(
            entity_type="vendor_card",
            entity_id=5,
            tag_id=tag1.id,
            interaction_count=10.0,
            total_entity_interactions=15.0,
            is_visible=True,
        )
        et2 = EntityTag(
            entity_type="vendor_card",
            entity_id=5,
            tag_id=tag2.id,
            interaction_count=2.0,
            total_entity_interactions=15.0,
            is_visible=False,  # Hidden — should NOT appear
        )
        db_session.add_all([et1, et2])
        db_session.commit()

        resp = client.get("/api/tags/entities/vendor_card/5")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["tag"]["name"] == "Analog Devices"

    def test_entity_tags_sorted_by_interaction_count(self, client, db_session: Session):
        """Entity tags are sorted by interaction_count descending."""
        tag1 = _make_tag(db_session, "LowTag", "commodity")
        tag2 = _make_tag(db_session, "HighTag", "commodity")
        db_session.commit()

        et1 = EntityTag(
            entity_type="company",
            entity_id=7,
            tag_id=tag1.id,
            interaction_count=2.0,
            total_entity_interactions=10.0,
            is_visible=True,
        )
        et2 = EntityTag(
            entity_type="company",
            entity_id=7,
            tag_id=tag2.id,
            interaction_count=9.0,
            total_entity_interactions=10.0,
            is_visible=True,
        )
        db_session.add_all([et1, et2])
        db_session.commit()

        resp = client.get("/api/tags/entities/company/7")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert data[0]["tag"]["name"] == "HighTag"
        assert data[1]["tag"]["name"] == "LowTag"


class TestGetMaterialCardTags:
    """Tests for GET /api/tags/material-cards/{material_card_id} endpoint."""

    def test_returns_empty_for_card_with_no_tags(self, client, db_session: Session):
        """Returns empty list for a material card with no tags."""
        mc = MaterialCard(
            normalized_mpn="test-mpn-001",
            display_mpn="TEST-MPN-001",
            search_count=0,
        )
        db_session.add(mc)
        db_session.commit()

        resp = client.get(f"/api/tags/material-cards/{mc.id}")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_returns_high_confidence_tags_only(self, client, db_session: Session):
        """Returns only material tags with confidence >= 0.7."""
        mc = MaterialCard(
            normalized_mpn="lm317t-tag-test",
            display_mpn="LM317T",
            search_count=5,
        )
        db_session.add(mc)
        db_session.flush()

        tag_high = _make_tag(db_session, "TI", "brand")
        tag_low = _make_tag(db_session, "Regulator", "commodity")
        db_session.flush()

        mt_high = MaterialTag(
            material_card_id=mc.id,
            tag_id=tag_high.id,
            confidence=0.95,
            source="ai_classified",
            classified_at=datetime.now(timezone.utc),
        )
        mt_low = MaterialTag(
            material_card_id=mc.id,
            tag_id=tag_low.id,
            confidence=0.5,  # Below threshold — should be excluded
            source="prefix_lookup",
        )
        db_session.add_all([mt_high, mt_low])
        db_session.commit()

        resp = client.get(f"/api/tags/material-cards/{mc.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["tag"]["name"] == "TI"
        assert data[0]["confidence"] == 0.95
        assert data[0]["source"] == "ai_classified"

    def test_material_tag_response_fields(self, client, db_session: Session):
        """Material tag response includes tag details, confidence, source,
        classified_at."""
        mc = MaterialCard(
            normalized_mpn="ne555-tag-test",
            display_mpn="NE555",
            search_count=1,
        )
        db_session.add(mc)
        db_session.flush()

        tag = _make_tag(db_session, "Signetics", "brand")
        db_session.flush()

        now = datetime.now(timezone.utc)
        mt = MaterialTag(
            material_card_id=mc.id,
            tag_id=tag.id,
            confidence=0.80,
            source="nexar",
            classified_at=now,
        )
        db_session.add(mt)
        db_session.commit()

        resp = client.get(f"/api/tags/material-cards/{mc.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        item = data[0]
        assert item["tag"]["name"] == "Signetics"
        assert item["tag"]["tag_type"] == "brand"
        assert item["confidence"] == 0.80
        assert item["source"] == "nexar"
        assert item["classified_at"] is not None

    def test_boundary_confidence_070_included(self, client, db_session: Session):
        """Tag with exactly 0.7 confidence is included."""
        mc = MaterialCard(
            normalized_mpn="boundary-conf-test",
            display_mpn="BOUNDARY",
            search_count=0,
        )
        db_session.add(mc)
        db_session.flush()

        tag = _make_tag(db_session, "BoundaryTag", "commodity")
        db_session.flush()

        mt = MaterialTag(
            material_card_id=mc.id,
            tag_id=tag.id,
            confidence=0.70,
            source="existing_data",
        )
        db_session.add(mt)
        db_session.commit()

        resp = client.get(f"/api/tags/material-cards/{mc.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1

    def test_tag_with_parent_id(self, client, db_session: Session):
        """Tag with a parent_id is returned with parent_id in response."""
        parent = _make_tag(db_session, "Semiconductors", "commodity")
        db_session.flush()
        child = Tag(name="MOSFETs", tag_type="commodity", parent_id=parent.id)
        db_session.add(child)
        db_session.flush()

        mc = MaterialCard(
            normalized_mpn="mosfet-parent-test",
            display_mpn="MOSFET",
            search_count=0,
        )
        db_session.add(mc)
        db_session.flush()

        mt = MaterialTag(
            material_card_id=mc.id,
            tag_id=child.id,
            confidence=0.9,
            source="ai_classified",
        )
        db_session.add(mt)
        db_session.commit()

        resp = client.get(f"/api/tags/material-cards/{mc.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data[0]["tag"]["parent_id"] == parent.id
