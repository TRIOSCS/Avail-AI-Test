"""Tests for batch material enrichment — submit + poll + apply cycle.

Called by: pytest
Depends on: app.services.material_enrichment_service, conftest fixtures
"""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models import MaterialCard
from app.services.material_enrichment_service import (
    batch_enrich_materials,
    process_material_batch_results,
)


@pytest.fixture()
def cards_needing_enrichment(db_session):
    """Create 3 unenriched material cards."""
    cards = []
    for i, mpn in enumerate(["LM358N", "STM32F103", "TPS65217B"], start=1):
        card = MaterialCard(
            id=i,
            normalized_mpn=mpn.lower(),
            display_mpn=mpn,
            manufacturer="TestMfr",
            enriched_at=None,
            deleted_at=None,
        )
        db_session.add(card)
        cards.append(card)
    db_session.commit()
    return cards


@pytest.fixture()
def enriched_card(db_session):
    """Create an already-enriched card (should be skipped)."""
    card = MaterialCard(
        id=100,
        normalized_mpn="already_enriched",
        display_mpn="ALREADY_ENRICHED",
        enriched_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        deleted_at=None,
    )
    db_session.add(card)
    db_session.commit()
    return card


class TestBatchEnrichMaterials:
    """Tests for batch_enrich_materials() — submit phase."""

    def test_submits_batch_and_returns_id(self, db_session, cards_needing_enrichment):
        """Should collect unenriched cards, build batch, submit, store in Redis."""
        mock_redis = MagicMock()
        mock_submit = AsyncMock(return_value="batch_abc123")

        with (
            patch(
                "app.services.material_enrichment_service._get_redis",
                return_value=mock_redis,
            ),
            patch(
                "app.services.material_enrichment_service.claude_batch_submit",
                mock_submit,
            ),
        ):
            result = asyncio.get_event_loop().run_until_complete(batch_enrich_materials(db_session))

        assert result == "batch_abc123"
        mock_submit.assert_called_once()
        # Verify the requests list has correct number of items (one per card)
        requests = mock_submit.call_args[0][0]
        assert len(requests) == 3
        # Each request should have the card's MPN in the prompt
        prompts = [r["prompt"] for r in requests]
        assert any("LM358N" in p for p in prompts)
        assert any("STM32F103" in p for p in prompts)
        assert any("TPS65217B" in p for p in prompts)

        # Verify Redis key was set
        mock_redis.set.assert_called_once()
        call_args = mock_redis.set.call_args
        assert call_args[0][0] == "batch:material_enrich:current"
        assert call_args[0][1] == "batch_abc123"

    def test_returns_none_when_no_unenriched(self, db_session, enriched_card):
        """Should return None when all cards are already enriched."""
        mock_redis = MagicMock()
        mock_submit = AsyncMock()

        with (
            patch(
                "app.services.material_enrichment_service._get_redis",
                return_value=mock_redis,
            ),
            patch(
                "app.services.material_enrichment_service.claude_batch_submit",
                mock_submit,
            ),
        ):
            result = asyncio.get_event_loop().run_until_complete(batch_enrich_materials(db_session))

        assert result is None
        mock_submit.assert_not_called()

    def test_skips_deleted_cards(self, db_session):
        """Soft-deleted cards should not be included in the batch."""
        card = MaterialCard(
            id=200,
            normalized_mpn="deleted_part",
            display_mpn="DELETED_PART",
            enriched_at=None,
            deleted_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        db_session.add(card)
        db_session.commit()

        mock_redis = MagicMock()
        mock_submit = AsyncMock()

        with (
            patch(
                "app.services.material_enrichment_service._get_redis",
                return_value=mock_redis,
            ),
            patch(
                "app.services.material_enrichment_service.claude_batch_submit",
                mock_submit,
            ),
        ):
            result = asyncio.get_event_loop().run_until_complete(batch_enrich_materials(db_session))

        assert result is None
        mock_submit.assert_not_called()

    def test_returns_none_on_submit_failure(self, db_session, cards_needing_enrichment):
        """Should return None if claude_batch_submit returns None."""
        mock_redis = MagicMock()
        mock_submit = AsyncMock(return_value=None)

        with (
            patch(
                "app.services.material_enrichment_service._get_redis",
                return_value=mock_redis,
            ),
            patch(
                "app.services.material_enrichment_service.claude_batch_submit",
                mock_submit,
            ),
        ):
            result = asyncio.get_event_loop().run_until_complete(batch_enrich_materials(db_session))

        assert result is None
        mock_redis.set.assert_not_called()


class TestProcessMaterialBatchResults:
    """Tests for process_material_batch_results() — poll + apply phase."""

    def test_applies_results_when_batch_complete(self, db_session, cards_needing_enrichment):
        """Should update cards with AI results when batch is ended."""
        mock_redis = MagicMock()
        mock_redis.get.return_value = b"batch_abc123"

        batch_results = {
            "mat_enrich:1": {
                "parts": [
                    {
                        "mpn": "LM358N",
                        "description": "Dual operational amplifier",
                        "category": "analog_ic",
                    }
                ]
            },
            "mat_enrich:2": {
                "parts": [
                    {
                        "mpn": "STM32F103",
                        "description": "ARM Cortex-M3 microcontroller",
                        "category": "microcontrollers",
                    }
                ]
            },
            "mat_enrich:3": {
                "parts": [
                    {
                        "mpn": "TPS65217B",
                        "description": "Power management IC",
                        "category": "power_management",
                    }
                ]
            },
        }
        mock_batch_results_fn = AsyncMock(return_value=batch_results)

        with (
            patch(
                "app.services.material_enrichment_service._get_redis",
                return_value=mock_redis,
            ),
            patch(
                "app.services.material_enrichment_service.claude_batch_results",
                mock_batch_results_fn,
            ),
        ):
            result = asyncio.get_event_loop().run_until_complete(process_material_batch_results(db_session))

        assert result["applied"] == 3
        assert result["errors"] == 0

        # Verify cards were updated
        card1 = db_session.get(MaterialCard, 1)
        assert card1.description == "Dual operational amplifier"
        assert card1.category == "analog_ic"
        assert card1.enriched_at is not None
        assert card1.enrichment_source == "batch_api"

        card2 = db_session.get(MaterialCard, 2)
        assert card2.description == "ARM Cortex-M3 microcontroller"
        assert card2.category == "microcontrollers"

        # Redis key should be cleared
        mock_redis.delete.assert_called_once_with("batch:material_enrich:current")

    def test_returns_none_when_no_batch_pending(self, db_session):
        """Should return None when no batch_id in Redis."""
        mock_redis = MagicMock()
        mock_redis.get.return_value = None

        with patch(
            "app.services.material_enrichment_service._get_redis",
            return_value=mock_redis,
        ):
            result = asyncio.get_event_loop().run_until_complete(process_material_batch_results(db_session))

        assert result is None

    def test_returns_none_when_still_processing(self, db_session):
        """Should return None when batch is not yet complete."""
        mock_redis = MagicMock()
        mock_redis.get.return_value = b"batch_abc123"
        mock_batch_results_fn = AsyncMock(return_value=None)

        with (
            patch(
                "app.services.material_enrichment_service._get_redis",
                return_value=mock_redis,
            ),
            patch(
                "app.services.material_enrichment_service.claude_batch_results",
                mock_batch_results_fn,
            ),
        ):
            result = asyncio.get_event_loop().run_until_complete(process_material_batch_results(db_session))

        assert result is None
        # Should NOT clear the Redis key
        mock_redis.delete.assert_not_called()

    def test_handles_error_results_gracefully(self, db_session, cards_needing_enrichment):
        """Cards with None results (errors) should be counted as errors."""
        mock_redis = MagicMock()
        mock_redis.get.return_value = b"batch_abc123"

        batch_results = {
            "mat_enrich:1": {
                "parts": [
                    {
                        "mpn": "LM358N",
                        "description": "Dual op-amp",
                        "category": "analog_ic",
                    }
                ]
            },
            "mat_enrich:2": None,  # Error result
            "mat_enrich:3": {
                "parts": [
                    {
                        "mpn": "TPS65217B",
                        "description": "PMIC",
                        "category": "power_management",
                    }
                ]
            },
        }
        mock_batch_results_fn = AsyncMock(return_value=batch_results)

        with (
            patch(
                "app.services.material_enrichment_service._get_redis",
                return_value=mock_redis,
            ),
            patch(
                "app.services.material_enrichment_service.claude_batch_results",
                mock_batch_results_fn,
            ),
        ):
            result = asyncio.get_event_loop().run_until_complete(process_material_batch_results(db_session))

        assert result["applied"] == 2
        assert result["errors"] == 1

        # Card 2 should remain unenriched
        card2 = db_session.get(MaterialCard, 2)
        assert card2.enriched_at is None

    def test_handles_no_redis(self, db_session):
        """Should return None gracefully if Redis is unavailable."""
        with patch(
            "app.services.material_enrichment_service._get_redis",
            return_value=None,
        ):
            result = asyncio.get_event_loop().run_until_complete(process_material_batch_results(db_session))

        assert result is None
