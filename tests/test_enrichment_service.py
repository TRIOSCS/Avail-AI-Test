"""test_enrichment_service.py — Tests for app/services/enrichment.py.

Covers enrich_material_card, _try_connector_config, enrich_batch,
_apply_enrichment_to_card, boost_confidence_internal, nexar_bulk_validate,
nexar_backfill_untagged, and cross_validate_batch.

Called by: pytest
Depends on: conftest.py fixtures, app.services.enrichment
"""

import asyncio
import os

os.environ["TESTING"] = "1"

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.intelligence import MaterialCard
from app.models.tags import MaterialTag, Tag

# ═══════════════════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════════════════


def _make_card(db, mpn="lm317t", display="LM317T", manufacturer=None, category=None):
    """Create and flush a MaterialCard."""
    card = MaterialCard(
        normalized_mpn=mpn,
        display_mpn=display,
        manufacturer=manufacturer,
        category=category,
        created_at=datetime.now(timezone.utc),
    )
    db.add(card)
    db.flush()
    return card


def _make_brand_tag(db, name="Texas Instruments"):
    """Create a brand Tag."""
    tag = Tag(name=name, tag_type="brand", created_at=datetime.now(timezone.utc))
    db.add(tag)
    db.flush()
    return tag


def _make_commodity_tag(db, name="Capacitors"):
    """Create a commodity Tag."""
    tag = Tag(name=name, tag_type="commodity", created_at=datetime.now(timezone.utc))
    db.add(tag)
    db.flush()
    return tag


def _make_material_tag(db, card_id, tag_id, source="ai_classified", confidence=0.7):
    """Create a MaterialTag."""
    mt = MaterialTag(
        material_card_id=card_id,
        tag_id=tag_id,
        source=source,
        confidence=confidence,
        classified_at=datetime.now(timezone.utc),
    )
    db.add(mt)
    db.flush()
    return mt


# ═══════════════════════════════════════════════════════════════════════
#  _try_connector_config
# ═══════════════════════════════════════════════════════════════════════


class TestTryConnectorConfig:
    @pytest.mark.asyncio
    async def test_missing_credentials_returns_none(self):
        """No credentials => returns None."""
        from app.services.enrichment import _try_connector_config

        config = {
            "name": "digikey",
            "module": "app.connectors.digikey",
            "class": "DigiKeyConnector",
            "creds": [("digikey", "DIGIKEY_CLIENT_ID"), ("digikey", "DIGIKEY_CLIENT_SECRET")],
            "confidence": 0.95,
        }
        with patch("app.services.enrichment.get_credential_cached", return_value=None):
            result = await _try_connector_config(config, "LM317T")
        assert result is None

    @pytest.mark.asyncio
    async def test_successful_enrichment(self):
        """Connector returns valid manufacturer data."""
        from app.services.enrichment import _try_connector_config

        config = {
            "name": "digikey",
            "module": "app.connectors.digikey",
            "class": "DigiKeyConnector",
            "creds": [("digikey", "DIGIKEY_CLIENT_ID")],
            "confidence": 0.95,
        }
        mock_connector = MagicMock()
        mock_connector.search = AsyncMock(
            return_value=[{"manufacturer": "Texas Instruments", "category": "Voltage Regulators"}]
        )

        with (
            patch("app.services.enrichment.get_credential_cached", return_value="fake-key"),
            patch("app.services.enrichment.importlib.import_module") as mock_import,
        ):
            mock_module = MagicMock()
            mock_module.DigiKeyConnector.return_value = mock_connector
            mock_import.return_value = mock_module

            result = await _try_connector_config(config, "LM317T")

        assert result is not None
        assert result["manufacturer"] == "Texas Instruments"
        assert result["category"] == "Voltage Regulators"
        assert result["source"] == "digikey"
        assert result["confidence"] == 0.95

    @pytest.mark.asyncio
    async def test_ignored_manufacturer_skipped(self):
        """Manufacturer in _IGNORED_MANUFACTURERS returns None."""
        from app.services.enrichment import _try_connector_config

        config = {
            "name": "mouser",
            "module": "app.connectors.mouser",
            "class": "MouserConnector",
            "creds": [("mouser", "MOUSER_API_KEY")],
            "confidence": 0.95,
        }
        mock_connector = MagicMock()
        mock_connector.search = AsyncMock(return_value=[{"manufacturer": "Unknown", "category": "Test"}])

        with (
            patch("app.services.enrichment.get_credential_cached", return_value="fake-key"),
            patch("app.services.enrichment.importlib.import_module") as mock_import,
        ):
            mock_module = MagicMock()
            mock_module.MouserConnector.return_value = mock_connector
            mock_import.return_value = mock_module

            result = await _try_connector_config(config, "LM317T")

        assert result is None

    @pytest.mark.asyncio
    async def test_empty_results_returns_none(self):
        """Connector returns empty results."""
        from app.services.enrichment import _try_connector_config

        config = {
            "name": "mouser",
            "module": "app.connectors.mouser",
            "class": "MouserConnector",
            "creds": [("mouser", "MOUSER_API_KEY")],
            "confidence": 0.95,
        }
        mock_connector = MagicMock()
        mock_connector.search = AsyncMock(return_value=[])

        with (
            patch("app.services.enrichment.get_credential_cached", return_value="fake-key"),
            patch("app.services.enrichment.importlib.import_module") as mock_import,
        ):
            mock_module = MagicMock()
            mock_module.MouserConnector.return_value = mock_connector
            mock_import.return_value = mock_module

            result = await _try_connector_config(config, "LM317T")

        assert result is None

    @pytest.mark.asyncio
    async def test_timeout_returns_none(self):
        """Connector times out."""
        from app.services.enrichment import _try_connector_config

        config = {
            "name": "digikey",
            "module": "app.connectors.digikey",
            "class": "DigiKeyConnector",
            "creds": [("digikey", "DIGIKEY_CLIENT_ID")],
            "confidence": 0.95,
        }
        mock_connector = MagicMock()
        mock_connector.search = AsyncMock(side_effect=asyncio.TimeoutError())

        with (
            patch("app.services.enrichment.get_credential_cached", return_value="fake-key"),
            patch("app.services.enrichment.importlib.import_module") as mock_import,
        ):
            mock_module = MagicMock()
            mock_module.DigiKeyConnector.return_value = mock_connector
            mock_import.return_value = mock_module

            result = await _try_connector_config(config, "LM317T")

        assert result is None

    @pytest.mark.asyncio
    async def test_auth_error_returns_none(self):
        """Connector returns 401 auth error."""
        from app.services.enrichment import _try_connector_config

        config = {
            "name": "digikey",
            "module": "app.connectors.digikey",
            "class": "DigiKeyConnector",
            "creds": [("digikey", "DIGIKEY_CLIENT_ID")],
            "confidence": 0.95,
        }
        mock_connector = MagicMock()
        mock_connector.search = AsyncMock(side_effect=Exception("401 Unauthorized"))

        with (
            patch("app.services.enrichment.get_credential_cached", return_value="fake-key"),
            patch("app.services.enrichment.importlib.import_module") as mock_import,
        ):
            mock_module = MagicMock()
            mock_module.DigiKeyConnector.return_value = mock_connector
            mock_import.return_value = mock_module

            result = await _try_connector_config(config, "LM317T")

        assert result is None

    @pytest.mark.asyncio
    async def test_rate_limit_error_returns_none(self):
        """Connector returns 429 rate limit error."""
        from app.services.enrichment import _try_connector_config

        config = {
            "name": "digikey",
            "module": "app.connectors.digikey",
            "class": "DigiKeyConnector",
            "creds": [("digikey", "DIGIKEY_CLIENT_ID")],
            "confidence": 0.95,
        }
        mock_connector = MagicMock()
        mock_connector.search = AsyncMock(side_effect=Exception("429 rate limited"))

        with (
            patch("app.services.enrichment.get_credential_cached", return_value="fake-key"),
            patch("app.services.enrichment.importlib.import_module") as mock_import,
        ):
            mock_module = MagicMock()
            mock_module.DigiKeyConnector.return_value = mock_connector
            mock_import.return_value = mock_module

            result = await _try_connector_config(config, "LM317T")

        assert result is None

    @pytest.mark.asyncio
    async def test_generic_error_returns_none(self):
        """Connector raises generic error."""
        from app.services.enrichment import _try_connector_config

        config = {
            "name": "digikey",
            "module": "app.connectors.digikey",
            "class": "DigiKeyConnector",
            "creds": [("digikey", "DIGIKEY_CLIENT_ID")],
            "confidence": 0.95,
        }
        mock_connector = MagicMock()
        mock_connector.search = AsyncMock(side_effect=Exception("Network error"))

        with (
            patch("app.services.enrichment.get_credential_cached", return_value="fake-key"),
            patch("app.services.enrichment.importlib.import_module") as mock_import,
        ):
            mock_module = MagicMock()
            mock_module.DigiKeyConnector.return_value = mock_connector
            mock_import.return_value = mock_module

            result = await _try_connector_config(config, "LM317T")

        assert result is None

    @pytest.mark.asyncio
    async def test_category_from_description_fallback(self):
        """Uses description as category fallback when category is None."""
        from app.services.enrichment import _try_connector_config

        config = {
            "name": "mouser",
            "module": "app.connectors.mouser",
            "class": "MouserConnector",
            "creds": [("mouser", "MOUSER_API_KEY")],
            "confidence": 0.95,
        }
        mock_connector = MagicMock()
        mock_connector.search = AsyncMock(
            return_value=[{"manufacturer": "Microchip", "category": None, "description": "Microcontroller"}]
        )

        with (
            patch("app.services.enrichment.get_credential_cached", return_value="fake-key"),
            patch("app.services.enrichment.importlib.import_module") as mock_import,
        ):
            mock_module = MagicMock()
            mock_module.MouserConnector.return_value = mock_connector
            mock_import.return_value = mock_module

            result = await _try_connector_config(config, "PIC16F877A")

        assert result["category"] == "Microcontroller"

    @pytest.mark.asyncio
    async def test_no_category_or_description(self):
        """Returns None category when neither category nor description present."""
        from app.services.enrichment import _try_connector_config

        config = {
            "name": "mouser",
            "module": "app.connectors.mouser",
            "class": "MouserConnector",
            "creds": [("mouser", "MOUSER_API_KEY")],
            "confidence": 0.95,
        }
        mock_connector = MagicMock()
        mock_connector.search = AsyncMock(return_value=[{"manufacturer": "STMicro"}])

        with (
            patch("app.services.enrichment.get_credential_cached", return_value="fake-key"),
            patch("app.services.enrichment.importlib.import_module") as mock_import,
        ):
            mock_module = MagicMock()
            mock_module.MouserConnector.return_value = mock_connector
            mock_import.return_value = mock_module

            result = await _try_connector_config(config, "STM32F4")

        assert result["manufacturer"] == "STMicro"
        assert result["category"] is None

    @pytest.mark.asyncio
    async def test_multiple_credentials_all_checked(self):
        """Config with 2 credentials checks both; second missing => None."""
        from app.services.enrichment import _try_connector_config

        config = {
            "name": "nexar",
            "module": "app.connectors.sources",
            "class": "NexarConnector",
            "creds": [("nexar", "NEXAR_CLIENT_ID"), ("nexar", "NEXAR_CLIENT_SECRET")],
            "confidence": 0.95,
        }

        def mock_cred(source, env):
            if env == "NEXAR_CLIENT_ID":
                return "id-val"
            return None  # Missing secret

        with patch("app.services.enrichment.get_credential_cached", side_effect=mock_cred):
            result = await _try_connector_config(config, "LM317T")

        assert result is None


# ═══════════════════════════════════════════════════════════════════════
#  enrich_material_card
# ═══════════════════════════════════════════════════════════════════════


class TestEnrichMaterialCard:
    @pytest.mark.asyncio
    async def test_first_connector_succeeds(self, db_session):
        """Returns result from first connector that succeeds."""
        from app.services.enrichment import enrich_material_card

        with patch(
            "app.services.enrichment._try_connector_config",
            new_callable=AsyncMock,
            return_value={"manufacturer": "TI", "source": "digikey", "confidence": 0.95, "category": None},
        ):
            result = await enrich_material_card("LM317T", db_session)

        assert result is not None
        assert result["manufacturer"] == "TI"

    @pytest.mark.asyncio
    async def test_all_connectors_fail(self, db_session):
        """All connectors return None => overall None."""
        from app.services.enrichment import enrich_material_card

        with patch("app.services.enrichment._try_connector_config", new_callable=AsyncMock, return_value=None):
            result = await enrich_material_card("UNKNOWN_PART", db_session)

        assert result is None


# ═══════════════════════════════════════════════════════════════════════
#  _apply_enrichment_to_card
# ═══════════════════════════════════════════════════════════════════════


class TestApplyEnrichmentToCard:
    def test_sets_manufacturer_and_category(self, db_session):
        """Sets manufacturer and category when not already set."""
        from app.services.enrichment import _apply_enrichment_to_card

        card = _make_card(db_session)
        enrichment = {
            "manufacturer": "Texas Instruments",
            "category": "Voltage Regulators",
            "source": "digikey",
            "confidence": 0.95,
        }

        with (
            patch("app.services.enrichment.classify_material_card", return_value={}),
            patch("app.services.enrichment.tag_material_card"),
        ):
            _apply_enrichment_to_card(card, enrichment, db_session)

        assert card.manufacturer == "Texas Instruments"
        assert card.category == "Voltage Regulators"

    def test_does_not_overwrite_existing_manufacturer(self, db_session):
        """Does not overwrite existing manufacturer."""
        from app.services.enrichment import _apply_enrichment_to_card

        card = _make_card(db_session, manufacturer="Existing Mfr")
        enrichment = {
            "manufacturer": "New Mfr",
            "category": "New Cat",
            "source": "mouser",
            "confidence": 0.95,
        }

        with (
            patch("app.services.enrichment.classify_material_card", return_value={}),
            patch("app.services.enrichment.tag_material_card"),
        ):
            _apply_enrichment_to_card(card, enrichment, db_session)

        assert card.manufacturer == "Existing Mfr"

    def test_does_not_overwrite_existing_category(self, db_session):
        """Does not overwrite existing category."""
        from app.services.enrichment import _apply_enrichment_to_card

        card = _make_card(db_session, category="Existing Cat")
        enrichment = {
            "manufacturer": "TI",
            "category": "New Cat",
            "source": "mouser",
            "confidence": 0.95,
        }

        with (
            patch("app.services.enrichment.classify_material_card", return_value={}),
            patch("app.services.enrichment.tag_material_card"),
        ):
            _apply_enrichment_to_card(card, enrichment, db_session)

        assert card.category == "Existing Cat"

    def test_creates_brand_and_commodity_tags(self, db_session):
        """Creates brand and commodity tags from classification."""
        from app.services.enrichment import _apply_enrichment_to_card

        card = _make_card(db_session)
        brand_tag = _make_brand_tag(db_session, "TI")
        commodity_tag = _make_commodity_tag(db_session, "Regulators")
        db_session.commit()

        enrichment = {
            "manufacturer": "TI",
            "category": "Regulators",
            "source": "digikey",
            "confidence": 0.95,
        }

        with (
            patch(
                "app.services.enrichment.classify_material_card",
                return_value={
                    "brand": {"name": "TI"},
                    "commodity": {"name": "Regulators"},
                },
            ),
            patch("app.services.enrichment.get_or_create_brand_tag", return_value=brand_tag),
            patch("app.services.enrichment.get_or_create_commodity_tag", return_value=commodity_tag),
            patch("app.services.enrichment.tag_material_card") as mock_tag,
        ):
            _apply_enrichment_to_card(card, enrichment, db_session)

        mock_tag.assert_called_once()
        tags_applied = mock_tag.call_args[0][1]
        assert len(tags_applied) == 2
        # Brand tag at full confidence
        assert tags_applied[0]["confidence"] == 0.95
        # Commodity tag capped at 0.9
        assert tags_applied[1]["confidence"] == 0.9

    def test_no_category_from_enrichment(self, db_session):
        """When enrichment has no category, card.category stays None."""
        from app.services.enrichment import _apply_enrichment_to_card

        card = _make_card(db_session)
        enrichment = {
            "manufacturer": "TI",
            "category": None,
            "source": "digikey",
            "confidence": 0.95,
        }

        with (
            patch("app.services.enrichment.classify_material_card", return_value={}),
            patch("app.services.enrichment.tag_material_card"),
        ):
            _apply_enrichment_to_card(card, enrichment, db_session)

        assert card.category is None

    def test_no_commodity_tag_returned(self, db_session):
        """When get_or_create_commodity_tag returns None, no commodity tag applied."""
        from app.services.enrichment import _apply_enrichment_to_card

        card = _make_card(db_session)
        brand_tag = _make_brand_tag(db_session, "Microchip")
        db_session.commit()

        enrichment = {
            "manufacturer": "Microchip",
            "category": "MCUs",
            "source": "mouser",
            "confidence": 0.95,
        }

        with (
            patch(
                "app.services.enrichment.classify_material_card",
                return_value={"brand": {"name": "Microchip"}, "commodity": {"name": "MCUs"}},
            ),
            patch("app.services.enrichment.get_or_create_brand_tag", return_value=brand_tag),
            patch("app.services.enrichment.get_or_create_commodity_tag", return_value=None),
            patch("app.services.enrichment.tag_material_card") as mock_tag,
        ):
            _apply_enrichment_to_card(card, enrichment, db_session)

        tags = mock_tag.call_args[0][1]
        assert len(tags) == 1  # Only brand


# ═══════════════════════════════════════════════════════════════════════
#  enrich_batch
# ═══════════════════════════════════════════════════════════════════════


class TestEnrichBatch:
    @pytest.mark.asyncio
    async def test_batch_enrichment(self, db_session):
        """Processes batch and returns stats."""
        from app.services.enrichment import enrich_batch

        card = _make_card(db_session)
        db_session.commit()

        with (
            patch(
                "app.services.enrichment.enrich_material_card",
                new_callable=AsyncMock,
                return_value={"manufacturer": "TI", "source": "digikey", "confidence": 0.95, "category": None},
            ),
            patch("app.services.enrichment._apply_enrichment_to_card"),
        ):
            result = await enrich_batch(["lm317t"], db_session)

        assert result["total"] == 1
        assert result["matched"] == 1
        assert result["skipped"] == 0

    @pytest.mark.asyncio
    async def test_batch_no_result_skips(self, db_session):
        """MPNs with no enrichment result are skipped."""
        from app.services.enrichment import enrich_batch

        with patch("app.services.enrichment.enrich_material_card", new_callable=AsyncMock, return_value=None):
            result = await enrich_batch(["UNKNOWN1", "UNKNOWN2"], db_session)

        assert result["total"] == 2
        assert result["matched"] == 0
        assert result["skipped"] == 2

    @pytest.mark.asyncio
    async def test_batch_card_not_found_skips(self, db_session):
        """MPN with enrichment but no card in DB is skipped."""
        from app.services.enrichment import enrich_batch

        with patch(
            "app.services.enrichment.enrich_material_card",
            new_callable=AsyncMock,
            return_value={"manufacturer": "TI", "source": "digikey", "confidence": 0.95, "category": None},
        ):
            result = await enrich_batch(["nonexistent_mpn"], db_session)

        assert result["total"] == 1
        assert result["skipped"] == 1

    @pytest.mark.asyncio
    async def test_batch_commits_every_100(self, db_session):
        """Batch commits every 100 items for progress tracking."""
        from app.services.enrichment import enrich_batch

        # Create 101 cards
        mpns = []
        for i in range(101):
            mpn = f"part{i:04d}"
            _make_card(db_session, mpn=mpn, display=mpn.upper())
            mpns.append(mpn)
        db_session.commit()

        with (
            patch(
                "app.services.enrichment.enrich_material_card",
                new_callable=AsyncMock,
                return_value={"manufacturer": "TI", "source": "digikey", "confidence": 0.95, "category": None},
            ),
            patch("app.services.enrichment._apply_enrichment_to_card"),
        ):
            result = await enrich_batch(mpns, db_session)

        assert result["total"] == 101
        assert result["matched"] == 101

    @pytest.mark.asyncio
    async def test_batch_sources_tracking(self, db_session):
        """Tracks which sources provided results."""
        from app.services.enrichment import enrich_batch

        card1 = _make_card(db_session, mpn="p1", display="P1")
        card2 = _make_card(db_session, mpn="p2", display="P2")
        db_session.commit()

        call_count = 0

        async def mock_enrich(mpn, db):
            nonlocal call_count
            call_count += 1
            sources = ["digikey", "mouser"]
            return {
                "manufacturer": "TI",
                "source": sources[call_count - 1],
                "confidence": 0.95,
                "category": None,
            }

        with (
            patch("app.services.enrichment.enrich_material_card", side_effect=mock_enrich),
            patch("app.services.enrichment._apply_enrichment_to_card"),
        ):
            result = await enrich_batch(["p1", "p2"], db_session)

        assert result["sources"]["digikey"] == 1
        assert result["sources"]["mouser"] == 1


# ═══════════════════════════════════════════════════════════════════════
#  boost_confidence_internal
# ═══════════════════════════════════════════════════════════════════════


class TestBoostConfidenceInternal:
    def test_boost_when_manufacturer_matches(self, db_session):
        """AI tag confirmed by card.manufacturer => boost to 0.90."""
        from app.services.enrichment import boost_confidence_internal

        card = _make_card(db_session, manufacturer="Texas Instruments")
        tag = _make_brand_tag(db_session, "Texas Instruments")
        mt = _make_material_tag(db_session, card.id, tag.id, source="ai_classified", confidence=0.7)
        db_session.commit()

        result = boost_confidence_internal(db_session, batch_size=100)

        db_session.refresh(mt)
        assert mt.confidence == 0.90
        assert mt.source == "ai_confirmed_internal"
        assert result["total_boosted"] >= 1

    def test_no_boost_when_already_high(self, db_session):
        """Tags already >= 0.9 are not boosted."""
        from app.services.enrichment import boost_confidence_internal

        card = _make_card(db_session, manufacturer="TI")
        tag = _make_brand_tag(db_session, "TI")
        mt = _make_material_tag(db_session, card.id, tag.id, source="ai_classified", confidence=0.92)
        db_session.commit()

        result = boost_confidence_internal(db_session, batch_size=100)

        db_session.refresh(mt)
        assert mt.confidence == 0.92  # Unchanged
        assert result["total_boosted"] == 0

    def test_no_boost_when_manufacturer_mismatch(self, db_session):
        """Tag name != card.manufacturer => no boost."""
        from app.services.enrichment import boost_confidence_internal

        card = _make_card(db_session, manufacturer="Microchip")
        tag = _make_brand_tag(db_session, "Texas Instruments")
        mt = _make_material_tag(db_session, card.id, tag.id, source="ai_classified", confidence=0.7)
        db_session.commit()

        result = boost_confidence_internal(db_session, batch_size=100)

        db_session.refresh(mt)
        assert mt.confidence == 0.7  # Unchanged

    def test_no_boost_when_manufacturer_is_null(self, db_session):
        """Card.manufacturer is NULL => no boost."""
        from app.services.enrichment import boost_confidence_internal

        card = _make_card(db_session, manufacturer=None)
        tag = _make_brand_tag(db_session, "TI")
        mt = _make_material_tag(db_session, card.id, tag.id, source="ai_classified", confidence=0.7)
        db_session.commit()

        result = boost_confidence_internal(db_session, batch_size=100)

        db_session.refresh(mt)
        assert mt.confidence == 0.7

    def test_returns_all_phase_counts(self, db_session):
        """Return dict has all expected keys."""
        from app.services.enrichment import boost_confidence_internal

        result = boost_confidence_internal(db_session, batch_size=100)

        assert "total_boosted" in result
        assert "fuzzy_boosted" in result
        assert "commodity_boosted" in result
        assert "sighting_boosted" in result
        assert "multi_source_boosted" in result

    def test_sighting_confirmed_boost(self, db_session):
        """Phase 4: Sighting manufacturer confirms brand tag => boost to 0.90."""
        from app.models.sourcing import Sighting
        from app.services.enrichment import boost_confidence_internal

        card = _make_card(db_session, mpn="sight1", display="SIGHT1")
        tag = _make_brand_tag(db_session, "Arrow")
        mt = _make_material_tag(db_session, card.id, tag.id, source="ai_classified", confidence=0.7)

        # Need a Requisition+Requirement for Sighting
        from app.models import Requirement, Requisition

        req = Requisition(
            name="SIGHT-REQ",
            customer_name="Test",
            status="active",
            created_by=1,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.flush()
        item = Requirement(
            requisition_id=req.id,
            primary_mpn="SIGHT1",
            target_qty=100,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(item)
        db_session.flush()

        sighting = Sighting(
            requirement_id=item.id,
            material_card_id=card.id,
            vendor_name="Vendor",
            manufacturer="Arrow",
            mpn_matched="SIGHT1",
            source_type="broker",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(sighting)
        db_session.commit()

        result = boost_confidence_internal(db_session, batch_size=100)

        db_session.refresh(mt)
        assert result["sighting_boosted"] >= 1

    def test_multi_source_boost(self, db_session):
        """Phase 5: AI + sighting agree => boost to 0.95."""
        from app.models.sourcing import Sighting
        from app.services.enrichment import boost_confidence_internal

        card = _make_card(db_session, mpn="multi1", display="MULTI1", manufacturer="Murata")
        tag = _make_brand_tag(db_session, "Murata")
        # Start at ai_confirmed_internal with 0.90 (from phase 1)
        mt = _make_material_tag(db_session, card.id, tag.id, source="ai_confirmed_internal", confidence=0.90)

        from app.models import Requirement, Requisition

        req = Requisition(
            name="MULTI-REQ",
            customer_name="T",
            status="active",
            created_by=1,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.flush()
        item = Requirement(
            requisition_id=req.id,
            primary_mpn="MULTI1",
            target_qty=100,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(item)
        db_session.flush()

        sighting = Sighting(
            requirement_id=item.id,
            material_card_id=card.id,
            vendor_name="V",
            manufacturer="Murata",
            mpn_matched="MULTI1",
            source_type="broker",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(sighting)
        db_session.commit()

        result = boost_confidence_internal(db_session, batch_size=100)

        db_session.refresh(mt)
        assert mt.confidence == 0.95
        assert result["multi_source_boosted"] >= 1


# ═══════════════════════════════════════════════════════════════════════
#  nexar_bulk_validate
# ═══════════════════════════════════════════════════════════════════════


class TestNexarBulkValidate:
    @pytest.mark.asyncio
    async def test_no_low_confidence_tags(self, db_session):
        """No low-conf tags => early return with zeros."""
        from app.services.enrichment import nexar_bulk_validate

        result = await nexar_bulk_validate(db_session, limit=100)
        assert result["total_checked"] == 0

    @pytest.mark.asyncio
    async def test_no_nexar_credentials(self, db_session):
        """Missing Nexar credentials => returns with error."""
        from app.services.enrichment import nexar_bulk_validate

        card = _make_card(db_session)
        tag = _make_brand_tag(db_session, "TI")
        _make_material_tag(db_session, card.id, tag.id, source="ai_classified", confidence=0.7)
        db_session.commit()

        with patch("app.services.enrichment.get_credential_cached", return_value=None):
            result = await nexar_bulk_validate(db_session, limit=100)

        assert result.get("error") == "no_nexar_creds"

    @pytest.mark.asyncio
    async def test_nexar_confirms_ai_tag(self, db_session):
        """Nexar agrees with AI => confidence upgraded to 0.95."""
        from app.services.enrichment import nexar_bulk_validate

        card = _make_card(db_session)
        tag = _make_brand_tag(db_session, "Texas Instruments")
        mt = _make_material_tag(db_session, card.id, tag.id, source="ai_classified", confidence=0.7)
        db_session.commit()

        mock_connector = MagicMock()
        mock_connector.AGGREGATE_QUERY = "query"
        mock_connector._run_query = AsyncMock(
            return_value={
                "data": {"supSearchMpn": {"results": [{"part": {"manufacturer": {"name": "Texas Instruments"}}}]}}
            }
        )

        with (
            patch("app.services.enrichment.get_credential_cached", return_value="fake-key"),
            patch("app.services.enrichment.NexarConnector", return_value=mock_connector),
        ):
            result = await nexar_bulk_validate(db_session, limit=100)

        db_session.refresh(mt)
        assert mt.confidence == 0.95
        assert result["confirmed"] == 1

    @pytest.mark.asyncio
    async def test_nexar_disagrees_changes_manufacturer(self, db_session):
        """Nexar has different manufacturer => applies Nexar's."""
        from app.services.enrichment import nexar_bulk_validate

        card = _make_card(db_session)
        tag = _make_brand_tag(db_session, "WrongBrand")
        mt = _make_material_tag(db_session, card.id, tag.id, source="ai_classified", confidence=0.7)
        db_session.commit()

        mock_connector = MagicMock()
        mock_connector.AGGREGATE_QUERY = "query"
        mock_connector._run_query = AsyncMock(
            return_value={
                "data": {"supSearchMpn": {"results": [{"part": {"manufacturer": {"name": "Microchip Technology"}}}]}}
            }
        )

        with (
            patch("app.services.enrichment.get_credential_cached", return_value="fake-key"),
            patch("app.services.enrichment.NexarConnector", return_value=mock_connector),
            patch("app.services.enrichment._apply_enrichment_to_card") as mock_apply,
        ):
            result = await nexar_bulk_validate(db_session, limit=100)

        assert result["changed"] == 1
        mock_apply.assert_called_once()

    @pytest.mark.asyncio
    async def test_nexar_no_results(self, db_session):
        """Nexar returns empty results => no_result count incremented."""
        from app.services.enrichment import nexar_bulk_validate

        card = _make_card(db_session)
        tag = _make_brand_tag(db_session, "SomeBrand")
        _make_material_tag(db_session, card.id, tag.id, source="ai_classified", confidence=0.7)
        db_session.commit()

        mock_connector = MagicMock()
        mock_connector.AGGREGATE_QUERY = "query"
        mock_connector._run_query = AsyncMock(return_value={"data": {"supSearchMpn": {"results": []}}})

        with (
            patch("app.services.enrichment.get_credential_cached", return_value="fake-key"),
            patch("app.services.enrichment.NexarConnector", return_value=mock_connector),
        ):
            result = await nexar_bulk_validate(db_session, limit=100)

        assert result["no_result"] == 1

    @pytest.mark.asyncio
    async def test_nexar_query_error(self, db_session):
        """Nexar query raises exception => no_result."""
        from app.services.enrichment import nexar_bulk_validate

        card = _make_card(db_session)
        tag = _make_brand_tag(db_session, "Brand")
        _make_material_tag(db_session, card.id, tag.id, source="ai_classified", confidence=0.7)
        db_session.commit()

        mock_connector = MagicMock()
        mock_connector.AGGREGATE_QUERY = "query"
        mock_connector._run_query = AsyncMock(side_effect=Exception("API error"))

        with (
            patch("app.services.enrichment.get_credential_cached", return_value="fake-key"),
            patch("app.services.enrichment.NexarConnector", return_value=mock_connector),
        ):
            result = await nexar_bulk_validate(db_session, limit=100)

        assert result["no_result"] == 1

    @pytest.mark.asyncio
    async def test_nexar_ignored_manufacturer(self, db_session):
        """Nexar returns ignored manufacturer ('unknown') => no_result."""
        from app.services.enrichment import nexar_bulk_validate

        card = _make_card(db_session)
        tag = _make_brand_tag(db_session, "Brand")
        _make_material_tag(db_session, card.id, tag.id, source="ai_classified", confidence=0.7)
        db_session.commit()

        mock_connector = MagicMock()
        mock_connector.AGGREGATE_QUERY = "query"
        mock_connector._run_query = AsyncMock(
            return_value={"data": {"supSearchMpn": {"results": [{"part": {"manufacturer": {"name": "Unknown"}}}]}}}
        )

        with (
            patch("app.services.enrichment.get_credential_cached", return_value="fake-key"),
            patch("app.services.enrichment.NexarConnector", return_value=mock_connector),
        ):
            result = await nexar_bulk_validate(db_session, limit=100)

        assert result["no_result"] == 1


# ═══════════════════════════════════════════════════════════════════════
#  nexar_backfill_untagged
# ═══════════════════════════════════════════════════════════════════════


class TestNexarBackfillUntagged:
    @pytest.mark.asyncio
    async def test_no_untagged_cards(self, db_session):
        """No untagged cards => early return."""
        from app.services.enrichment import nexar_backfill_untagged

        result = await nexar_backfill_untagged(db_session, limit=100)
        assert result["total_checked"] == 0

    @pytest.mark.asyncio
    async def test_no_credentials(self, db_session):
        """Missing credentials => returns with error."""
        from app.services.enrichment import nexar_backfill_untagged

        card = _make_card(db_session)
        db_session.commit()

        with patch("app.services.enrichment.get_credential_cached", return_value=None):
            result = await nexar_backfill_untagged(db_session, limit=100)

        assert result.get("error") == "no_nexar_creds"

    @pytest.mark.asyncio
    async def test_backfill_tags_card(self, db_session):
        """Nexar data applied to untagged card."""
        from app.services.enrichment import nexar_backfill_untagged

        card = _make_card(db_session)
        db_session.commit()

        mock_connector = MagicMock()
        mock_connector.AGGREGATE_QUERY = "query"
        mock_connector._run_query = AsyncMock(
            return_value={
                "data": {"supSearchMpn": {"results": [{"part": {"manufacturer": {"name": "Texas Instruments"}}}]}}
            }
        )

        with (
            patch("app.services.enrichment.get_credential_cached", return_value="fake-key"),
            patch("app.services.enrichment.NexarConnector", return_value=mock_connector),
            patch("app.services.enrichment._apply_enrichment_to_card") as mock_apply,
        ):
            result = await nexar_backfill_untagged(db_session, limit=100)

        assert result["tagged"] == 1
        mock_apply.assert_called_once()

    @pytest.mark.asyncio
    async def test_backfill_no_result(self, db_session):
        """Nexar returns empty => no_result."""
        from app.services.enrichment import nexar_backfill_untagged

        card = _make_card(db_session)
        db_session.commit()

        mock_connector = MagicMock()
        mock_connector.AGGREGATE_QUERY = "query"
        mock_connector._run_query = AsyncMock(return_value={"data": {"supSearchMpn": {"results": []}}})

        with (
            patch("app.services.enrichment.get_credential_cached", return_value="fake-key"),
            patch("app.services.enrichment.NexarConnector", return_value=mock_connector),
        ):
            result = await nexar_backfill_untagged(db_session, limit=100)

        assert result["no_result"] == 1

    @pytest.mark.asyncio
    async def test_backfill_error_handled(self, db_session):
        """Nexar query error => no_result, doesn't crash."""
        from app.services.enrichment import nexar_backfill_untagged

        card = _make_card(db_session)
        db_session.commit()

        mock_connector = MagicMock()
        mock_connector.AGGREGATE_QUERY = "query"
        mock_connector._run_query = AsyncMock(side_effect=Exception("API failure"))

        with (
            patch("app.services.enrichment.get_credential_cached", return_value="fake-key"),
            patch("app.services.enrichment.NexarConnector", return_value=mock_connector),
        ):
            result = await nexar_backfill_untagged(db_session, limit=100)

        assert result["no_result"] == 1

    @pytest.mark.asyncio
    async def test_backfill_ignored_manufacturer(self, db_session):
        """Nexar returns 'n/a' manufacturer => no_result."""
        from app.services.enrichment import nexar_backfill_untagged

        card = _make_card(db_session)
        db_session.commit()

        mock_connector = MagicMock()
        mock_connector.AGGREGATE_QUERY = "query"
        mock_connector._run_query = AsyncMock(
            return_value={"data": {"supSearchMpn": {"results": [{"part": {"manufacturer": {"name": "N/A"}}}]}}}
        )

        with (
            patch("app.services.enrichment.get_credential_cached", return_value="fake-key"),
            patch("app.services.enrichment.NexarConnector", return_value=mock_connector),
        ):
            result = await nexar_backfill_untagged(db_session, limit=100)

        assert result["no_result"] == 1


# ═══════════════════════════════════════════════════════════════════════
#  cross_validate_batch
# ═══════════════════════════════════════════════════════════════════════


class TestCrossValidateBatch:
    @pytest.mark.asyncio
    async def test_no_low_conf_tags(self, db_session):
        """No low-confidence tags => early return."""
        from app.services.enrichment import cross_validate_batch

        result = await cross_validate_batch(db_session, limit=100)
        assert result["total"] == 0

    @pytest.mark.asyncio
    async def test_connector_confirms_ai(self, db_session):
        """Connector confirms AI tag => upgrades confidence."""
        from app.services.enrichment import cross_validate_batch

        card = _make_card(db_session)
        tag = _make_brand_tag(db_session, "Texas Instruments")
        mt = _make_material_tag(db_session, card.id, tag.id, source="ai_classified", confidence=0.7)
        db_session.commit()

        with patch(
            "app.services.enrichment.enrich_material_card",
            new_callable=AsyncMock,
            return_value={
                "manufacturer": "Texas Instruments",
                "source": "digikey",
                "confidence": 0.95,
                "category": None,
            },
        ):
            result = await cross_validate_batch(db_session, limit=100)

        db_session.refresh(mt)
        assert mt.confidence == 0.95
        assert result["confirmed"] == 1
        assert result["sources"]["digikey"] == 1

    @pytest.mark.asyncio
    async def test_connector_changes_manufacturer(self, db_session):
        """Connector says different manufacturer => replaces."""
        from app.services.enrichment import cross_validate_batch

        card = _make_card(db_session)
        tag = _make_brand_tag(db_session, "WrongBrand")
        mt = _make_material_tag(db_session, card.id, tag.id, source="ai_classified", confidence=0.7)
        db_session.commit()

        with (
            patch(
                "app.services.enrichment.enrich_material_card",
                new_callable=AsyncMock,
                return_value={
                    "manufacturer": "CorrectBrand",
                    "source": "mouser",
                    "confidence": 0.95,
                    "category": None,
                },
            ),
            patch("app.services.enrichment._apply_enrichment_to_card") as mock_apply,
        ):
            result = await cross_validate_batch(db_session, limit=100)

        assert result["changed_manufacturer"] == 1
        mock_apply.assert_called_once()

    @pytest.mark.asyncio
    async def test_connector_no_result(self, db_session):
        """Connector returns None => no_result."""
        from app.services.enrichment import cross_validate_batch

        card = _make_card(db_session)
        tag = _make_brand_tag(db_session, "SomeBrand")
        _make_material_tag(db_session, card.id, tag.id, source="ai_classified", confidence=0.7)
        db_session.commit()

        with patch("app.services.enrichment.enrich_material_card", new_callable=AsyncMock, return_value=None):
            result = await cross_validate_batch(db_session, limit=100)

        assert result["no_result"] == 1

    @pytest.mark.asyncio
    async def test_fuzzy_match_contains(self, db_session):
        """Fuzzy match: AI 'ti' is contained in connector 'texas instruments'."""
        from app.services.enrichment import cross_validate_batch

        card = _make_card(db_session)
        tag = _make_brand_tag(db_session, "ti")
        mt = _make_material_tag(db_session, card.id, tag.id, source="ai_classified", confidence=0.7)
        db_session.commit()

        with patch(
            "app.services.enrichment.enrich_material_card",
            new_callable=AsyncMock,
            return_value={
                "manufacturer": "Texas Instruments TI",
                "source": "element14",
                "confidence": 0.95,
                "category": None,
            },
        ):
            result = await cross_validate_batch(db_session, limit=100)

        db_session.refresh(mt)
        assert mt.confidence == 0.95
        assert result["confirmed"] == 1
