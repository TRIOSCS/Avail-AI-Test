"""Tests for app.services.tagging — brand tag TOCTOU race fix and classification.

Covers: get_or_create_brand_tag happy path, race-condition retry via IntegrityError,
classify_material_card waterfall, and commodity tag lookup.

Called by: pytest
Depends on: app.services.tagging, app.models.tags, tests.conftest (engine, db_session)
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from sqlalchemy.exc import IntegrityError

from app.models.tags import Tag
from app.services.tagging import (
    classify_material_card,
    get_or_create_brand_tag,
    get_or_create_commodity_tag,
)

# ── get_or_create_brand_tag ──────────────────────────────────────────


class TestGetOrCreateBrandTag:
    """Tests for get_or_create_brand_tag with race-safe savepoint retry."""

    def test_creates_new_brand_tag(self, db_session):
        """Happy path: creates a new brand tag when none exists."""
        tag = get_or_create_brand_tag("Texas Instruments", db_session)

        assert tag is not None
        assert tag.name == "Texas Instruments"
        assert tag.tag_type == "brand"
        assert tag.created_at is not None

    def test_returns_existing_tag_case_insensitive(self, db_session):
        """Returns existing tag via case-insensitive match."""
        existing = Tag(name="Microchip", tag_type="brand", created_at=datetime.now(timezone.utc))
        db_session.add(existing)
        db_session.flush()

        tag = get_or_create_brand_tag("microchip", db_session)
        assert tag.id == existing.id

    def test_strips_whitespace(self, db_session):
        """Strips leading/trailing whitespace from manufacturer name."""
        tag = get_or_create_brand_tag("  Analog Devices  ", db_session)
        assert tag.name == "Analog Devices"

    def test_returns_existing_tag_exact_match(self, db_session):
        """Returns existing tag on exact match without creating duplicate."""
        existing = Tag(name="NXP", tag_type="brand", created_at=datetime.now(timezone.utc))
        db_session.add(existing)
        db_session.flush()

        tag = get_or_create_brand_tag("NXP", db_session)
        assert tag.id == existing.id

    def test_race_condition_integrity_error_retries(self, db_session):
        """Simulates TOCTOU race: first SELECT returns None, INSERT hits
        IntegrityError, re-fetch finds the tag created by concurrent session."""
        # Pre-insert a tag to be found on re-fetch
        existing = Tag(name="STMicroelectronics", tag_type="brand", created_at=datetime.now(timezone.utc))
        db_session.add(existing)
        db_session.flush()
        existing_id = existing.id

        # Patch begin_nested to raise IntegrityError (simulating concurrent insert)
        original_execute = db_session.execute

        call_count = 0

        def mock_execute(stmt, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            # First call is the initial SELECT — return empty to simulate race
            if call_count == 1:
                mock_result = MagicMock()
                mock_result.scalar_one_or_none.return_value = None
                return mock_result
            # Third call (after IntegrityError) is the re-fetch — use real execute
            return original_execute(stmt, *args, **kwargs)

        def mock_begin_nested():
            raise IntegrityError("duplicate key", params=None, orig=Exception())

        with patch.object(db_session, "execute", side_effect=mock_execute):
            with patch.object(db_session, "begin_nested", side_effect=mock_begin_nested):
                tag = get_or_create_brand_tag("STMicroelectronics", db_session)

        assert tag.id == existing_id
        assert tag.name == "STMicroelectronics"

    def test_does_not_match_commodity_tag_with_same_name(self, db_session):
        """A commodity tag with the same name should not be returned as a brand tag."""
        commodity = Tag(name="Resistors", tag_type="commodity", created_at=datetime.now(timezone.utc))
        db_session.add(commodity)
        db_session.flush()

        tag = get_or_create_brand_tag("Resistors", db_session)
        assert tag.id != commodity.id
        assert tag.tag_type == "brand"


# ── get_or_create_commodity_tag ──────────────────────────────────────


class TestGetOrCreateCommodityTag:
    """Tests for commodity tag lookup (pre-seeded, no creation)."""

    def test_finds_existing_commodity(self, db_session):
        existing = Tag(name="Capacitors", tag_type="commodity", created_at=datetime.now(timezone.utc))
        db_session.add(existing)
        db_session.flush()

        result = get_or_create_commodity_tag("Capacitors", db_session)
        assert result is not None
        assert result.id == existing.id

    def test_returns_none_when_not_found(self, db_session):
        result = get_or_create_commodity_tag("Nonexistent", db_session)
        assert result is None


# ── classify_material_card ───────────────────────────────────────────


class TestClassifyMaterialCard:
    """Tests for the classification waterfall."""

    def test_existing_manufacturer_used_as_brand(self):
        result = classify_material_card("STM32F103", "STMicro", None)
        assert result["brand"]["name"] == "STMicro"
        assert result["brand"]["source"] == "existing_data"
        assert result["brand"]["confidence"] == 0.95

    def test_prefix_lookup_fallback(self):
        with patch("app.services.tagging.lookup_manufacturer_by_prefix", return_value=("Texas Instruments", 0.85)):
            result = classify_material_card("TPS54360", None, None)
        assert result["brand"]["name"] == "Texas Instruments"
        assert result["brand"]["source"] == "prefix_lookup"

    def test_no_brand_when_no_data(self):
        with patch("app.services.tagging.lookup_manufacturer_by_prefix", return_value=(None, 0.0)):
            result = classify_material_card("UNKNOWN123", None, None)
        assert result["brand"] is None

    def test_category_maps_to_commodity(self):
        result = classify_material_card("ABC123", None, "MLCC Capacitor")
        assert result["commodity"]["name"] == "Capacitors"
        assert result["commodity"]["source"] == "existing_data"

    def test_no_commodity_for_unknown_category(self):
        result = classify_material_card("ABC123", None, "zzzz_no_match")
        assert result["commodity"] is None

    def test_no_commodity_when_no_category(self):
        result = classify_material_card("ABC123", "Acme", None)
        assert result["commodity"] is None
