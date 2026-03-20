"""Tests for batch material enrichment — submit and process results.

Verifies batch_enrich_materials() and process_material_batch_results()
use the BatchQueue + claude_batch_submit/results flow correctly.

Called by: pytest
Depends on: app.services.material_enrichment_service, conftest fixtures
"""

import asyncio
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.models import MaterialCard
from app.services.material_enrichment_service import (
    batch_enrich_materials,
    process_material_batch_results,
)
from tests.conftest import engine  # noqa: F401 — ensures SQLite engine is used


@pytest.fixture()
def unenriched_cards(db_session):
    """Create 5 material cards with enriched_at = NULL."""
    cards = []
    for i in range(5):
        card = MaterialCard(
            display_mpn=f"TEST-MPN-{i:03d}",
            normalized_mpn=f"test-mpn-{i:03d}",
            manufacturer=f"Vendor{i}" if i % 2 == 0 else None,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(card)
        cards.append(card)
    db_session.commit()
    for c in cards:
        db_session.refresh(c)
    return cards


@pytest.fixture()
def enriched_cards(db_session):
    """Create 3 material cards that are already enriched."""
    cards = []
    for i in range(3):
        card = MaterialCard(
            display_mpn=f"DONE-MPN-{i:03d}",
            normalized_mpn=f"done-mpn-{i:03d}",
            enriched_at=datetime.now(timezone.utc),
            enrichment_source="claude_haiku",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(card)
        cards.append(card)
    db_session.commit()
    return cards


# ── batch_enrich_materials tests ──────────────────────────────────────


def test_batch_enrich_no_cards(db_session):
    """Returns None when no unenriched cards exist."""
    result = asyncio.get_event_loop().run_until_complete(batch_enrich_materials(db_session))
    assert result is None


@patch("app.services.material_enrichment_service._get_redis")
@patch("app.services.material_enrichment_service.claude_batch_submit")
def test_batch_enrich_submits_batch(mock_submit, mock_redis, db_session, unenriched_cards):
    """Submits a batch and stores batch_id in Redis."""
    mock_submit.return_value = "batch_abc123"
    mock_r = MagicMock()
    mock_r.get.return_value = None  # No in-flight batch
    mock_redis.return_value = mock_r

    result = asyncio.get_event_loop().run_until_complete(batch_enrich_materials(db_session))

    assert result == "batch_abc123"
    mock_submit.assert_called_once()

    # Check the requests passed to claude_batch_submit — one per card
    requests = mock_submit.call_args[0][0]
    assert len(requests) == 5
    for req in requests:
        assert "custom_id" in req
        assert "prompt" in req
        assert "schema" in req
        assert "system" in req

    # Verify Redis set was called
    mock_r.set.assert_called_once_with("batch:material_enrich:current", "batch_abc123")


@patch("app.services.material_enrichment_service._get_redis")
@patch("app.services.material_enrichment_service.claude_batch_submit")
def test_batch_enrich_skips_enriched(mock_submit, mock_redis, db_session, unenriched_cards, enriched_cards):
    """Only picks up cards with enriched_at IS NULL."""
    mock_submit.return_value = "batch_xyz"
    mock_r = MagicMock()
    mock_r.get.return_value = None  # No in-flight batch
    mock_redis.return_value = mock_r

    result = asyncio.get_event_loop().run_until_complete(batch_enrich_materials(db_session))

    assert result == "batch_xyz"
    requests = mock_submit.call_args[0][0]
    # Should have 5 requests (one per unenriched card), not 8
    assert len(requests) == 5
    # Collect all prompts to check contents
    all_prompts = " ".join(r["prompt"] for r in requests)
    for i in range(5):
        assert f"TEST-MPN-{i:03d}" in all_prompts
    for i in range(3):
        assert f"DONE-MPN-{i:03d}" not in all_prompts


@patch("app.services.material_enrichment_service._get_redis")
@patch("app.services.material_enrichment_service.claude_batch_submit")
def test_batch_enrich_submit_fails(mock_submit, mock_redis, db_session, unenriched_cards):
    """Returns None when claude_batch_submit fails."""
    mock_submit.return_value = None
    mock_r = MagicMock()
    mock_r.get.return_value = None  # No in-flight batch
    mock_redis.return_value = mock_r

    result = asyncio.get_event_loop().run_until_complete(batch_enrich_materials(db_session))

    assert result is None
    mock_r.set.assert_not_called()


@patch("app.services.material_enrichment_service._get_redis")
@patch("app.services.material_enrichment_service.claude_batch_submit")
def test_batch_enrich_skips_if_inflight(mock_submit, mock_redis, db_session, unenriched_cards):
    """Returns None immediately when a batch is already in-flight in Redis."""
    mock_r = MagicMock()
    mock_r.get.return_value = b"batch_already_running"  # Simulate in-flight batch
    mock_redis.return_value = mock_r

    result = asyncio.get_event_loop().run_until_complete(batch_enrich_materials(db_session))

    assert result is None
    mock_submit.assert_not_called()


# ── process_material_batch_results tests ──────────────────────────────


@patch("app.services.material_enrichment_service._get_redis")
def test_process_results_no_batch_id(mock_redis, db_session):
    """Returns 0 when no batch_id is stored in Redis."""
    mock_r = MagicMock()
    mock_r.get.return_value = None
    mock_redis.return_value = mock_r

    result = asyncio.get_event_loop().run_until_complete(process_material_batch_results(db_session))
    assert result is None


@patch("app.services.material_enrichment_service._get_redis")
@patch("app.services.material_enrichment_service.claude_batch_results")
def test_process_results_still_processing(mock_results, mock_redis, db_session):
    """Returns None when batch is still processing."""
    mock_r = MagicMock()
    mock_r.get.return_value = b"batch_abc123"
    mock_redis.return_value = mock_r
    mock_results.return_value = None  # Still processing

    result = asyncio.get_event_loop().run_until_complete(process_material_batch_results(db_session))
    assert result is None
    # Should NOT clear the Redis key
    mock_r.delete.assert_not_called()


@patch("app.services.material_enrichment_service._get_redis")
@patch("app.services.material_enrichment_service.claude_batch_results")
def test_process_results_applies_enrichment(mock_results, mock_redis, db_session, unenriched_cards):
    """Applies AI results to material cards and clears Redis key."""
    mock_r = MagicMock()
    mock_r.get.return_value = b"batch_abc123"
    mock_redis.return_value = mock_r

    # Build results keyed by the custom_id pattern used in batch_enrich_materials
    results_dict = {}
    for i in range(5):
        card = unenriched_cards[i]
        custom_id = f"mat_enrich-{card.id}"
        results_dict[custom_id] = {
            "parts": [
                {
                    "mpn": card.display_mpn,
                    "description": f"Test capacitor {i}",
                    "category": "capacitors",
                }
            ]
        }

    mock_results.return_value = results_dict

    result = asyncio.get_event_loop().run_until_complete(process_material_batch_results(db_session))

    assert result["applied"] == 5
    assert result["errors"] == 0

    # Verify cards were enriched
    for card in unenriched_cards:
        db_session.refresh(card)
        assert card.enriched_at is not None
        assert card.enrichment_source == "batch_api"
        assert card.description is not None
        assert card.category == "capacitors"

    # Redis key should be cleared
    mock_r.delete.assert_called_once_with("batch:material_enrich:current")


@patch("app.services.material_enrichment_service._get_redis")
@patch("app.services.material_enrichment_service.claude_batch_results")
def test_process_results_handles_none_entry(mock_results, mock_redis, db_session, unenriched_cards):
    """Skips cards with None results (errors) without crashing."""
    mock_r = MagicMock()
    mock_r.get.return_value = b"batch_abc123"
    mock_redis.return_value = mock_r

    card = unenriched_cards[0]
    results_dict = {
        f"mat_enrich-{card.id}": None,  # Error entry
    }
    mock_results.return_value = results_dict

    result = asyncio.get_event_loop().run_until_complete(process_material_batch_results(db_session))

    # Only the error entry, so 0 applied
    assert result["applied"] == 0
    assert result["errors"] == 1
    db_session.refresh(card)
    assert card.enriched_at is None

    # Redis key should still be cleared since results were returned
    mock_r.delete.assert_called_once()


@patch("app.services.material_enrichment_service._get_redis")
@patch("app.services.material_enrichment_service.claude_batch_results")
def test_process_results_invalid_category_falls_back(mock_results, mock_redis, db_session, unenriched_cards):
    """Invalid category gets replaced with 'other'."""
    mock_r = MagicMock()
    mock_r.get.return_value = b"batch_abc123"
    mock_redis.return_value = mock_r

    card = unenriched_cards[0]
    results_dict = {
        f"mat_enrich-{card.id}": {
            "parts": [
                {
                    "mpn": card.display_mpn,
                    "description": "A widget",
                    "category": "INVALID_NONSENSE",
                }
            ]
        },
    }
    mock_results.return_value = results_dict

    result = asyncio.get_event_loop().run_until_complete(process_material_batch_results(db_session))

    assert result["applied"] == 1
    db_session.refresh(card)
    assert card.category == "other"
