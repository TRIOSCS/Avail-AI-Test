"""test_material_enrichment_service_coverage.py — Extra coverage for
material_enrichment_service.py.

Targets uncovered branches at lines 75, 162-166, 226-235, 253-308, 321-392.
These cover:
- _apply_enrichment_result with invalid category/lifecycle fallbacks
- Commit failure in _enrich_batch
- _build_enrich_prompt helper
- batch_enrich_materials full path (Redis pending check, no cards, submit failures)
- process_material_batch_results full path (no redis, no batch, results, apply)

Called by: pytest
Depends on: app/services/material_enrichment_service.py
"""

import os

os.environ["TESTING"] = "1"

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models import Base, MaterialCard
from tests.conftest import TestSessionLocal, engine


@pytest.fixture(autouse=True)
def _tables():
    Base.metadata.create_all(bind=engine)
    yield
    # Recreate tables after each test so subsequent tests aren't affected.
    # (Drop + recreate instead of drop-only to preserve the shared test schema.)
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


@pytest.fixture
def db():
    session = TestSessionLocal()
    yield session
    session.close()


def _make_card(db, mpn, *, manufacturer=None, description=None, enriched_at=None):
    card = MaterialCard(
        normalized_mpn=mpn.lower().replace("-", "").replace(" ", ""),
        display_mpn=mpn,
        manufacturer=manufacturer,
        description=description,
        enriched_at=enriched_at,
        search_count=1,
    )
    db.add(card)
    db.commit()
    db.refresh(card)
    return card


# ── _apply_enrichment_result ─────────────────────────────────────────


def test_apply_enrichment_invalid_category_defaults_to_other(db):
    """Category not in VALID_CATEGORIES → 'other' (line 72-73)."""
    from app.services.material_enrichment_service import _apply_enrichment_result

    card = _make_card(db, "LM317T")
    _apply_enrichment_result(card, {"description": "desc", "category": "invalid_cat", "lifecycle_status": "active"})
    assert card.category == "other"


def test_apply_enrichment_invalid_lifecycle_defaults_to_active(db):
    """lifecycle_status not in VALID_LIFECYCLE → 'active' (line 74-75)."""
    from app.services.material_enrichment_service import _apply_enrichment_result

    card = _make_card(db, "NE555")
    _apply_enrichment_result(
        card, {"description": "555 timer", "category": "other", "lifecycle_status": "unknown_status"}
    )
    assert card.lifecycle_status == "active"


def test_apply_enrichment_none_description_skips_update(db):
    """None description → card.description not updated."""
    from app.services.material_enrichment_service import _apply_enrichment_result

    card = _make_card(db, "TL431", description="Original desc")
    _apply_enrichment_result(card, {"description": None, "category": "other", "lifecycle_status": "active"})
    assert card.description == "Original desc"


def test_apply_enrichment_sets_enrichment_source_and_time(db):
    """enrichment_source and enriched_at are set."""
    from app.services.material_enrichment_service import _apply_enrichment_result

    card = _make_card(db, "LM7805")
    before = datetime.now(timezone.utc)
    _apply_enrichment_result(
        card, {"description": "5V regulator", "category": "power_ic", "lifecycle_status": "active"}
    )
    assert card.enrichment_source == "claude_haiku"
    assert card.enriched_at >= before


# ── _enrich_batch commit failure ─────────────────────────────────────


@pytest.mark.asyncio
async def test_enrich_batch_commit_failure_rolls_back(db):
    """Commit failure → rollback, stats reflect errors (lines 162-166)."""
    from app.services.material_enrichment_service import _enrich_batch

    card = _make_card(db, "STM32")
    stats = {"enriched": 0, "skipped": 0, "errors": 0}

    mock_result = {
        "parts": [
            {"mpn": "STM32", "description": "ARM MCU", "category": "microcontrollers", "lifecycle_status": "active"}
        ]
    }

    with patch(
        "app.utils.claude_client.claude_structured",
        new_callable=AsyncMock,
        return_value=mock_result,
    ):
        # Patch db.commit to raise an exception
        original_commit = db.commit
        call_count = [0]

        def _fail_commit():
            call_count[0] += 1
            if call_count[0] == 1:
                raise Exception("DB commit failed")
            return original_commit()

        db.commit = _fail_commit
        try:
            await _enrich_batch([card], db, stats)
        finally:
            db.commit = original_commit

    # After commit failure: enriched decremented back, errors incremented
    assert stats["errors"] > 0


# ── _build_enrich_prompt ─────────────────────────────────────────────


def test_build_enrich_prompt_without_manufacturer(db):
    """Prompt is built with just MPN when no manufacturer (line 226-235)."""
    from app.services.material_enrichment_service import _build_enrich_prompt

    card = _make_card(db, "GENERIC-PART")
    prompt = _build_enrich_prompt([card])
    assert "GENERIC-PART" in prompt
    assert "categories" in prompt.lower() or "valid" in prompt.lower()


def test_build_enrich_prompt_with_manufacturer(db):
    """Prompt includes manufacturer name when provided."""
    from app.services.material_enrichment_service import _build_enrich_prompt

    card = _make_card(db, "LM317T", manufacturer="Texas Instruments")
    prompt = _build_enrich_prompt([card])
    assert "LM317T" in prompt
    assert "Texas Instruments" in prompt


def test_build_enrich_prompt_multiple_cards(db):
    """Multiple cards → all MPNs appear in prompt."""
    from app.services.material_enrichment_service import _build_enrich_prompt

    c1 = _make_card(db, "LM317T", manufacturer="TI")
    c2 = _make_card(db, "NE555")
    prompt = _build_enrich_prompt([c1, c2])
    assert "LM317T" in prompt
    assert "NE555" in prompt


# ── batch_enrich_materials ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_batch_enrich_returns_none_when_batch_pending(db):
    """If Redis shows a batch already pending, returns None (lines 253-256)."""
    from app.services.material_enrichment_service import batch_enrich_materials

    mock_redis = MagicMock()
    mock_redis.get.return_value = b"existing-batch-id"
    with patch("app.services.material_enrichment_service._get_redis", return_value=mock_redis):
        result = await batch_enrich_materials(db)
    assert result is None


@pytest.mark.asyncio
async def test_batch_enrich_returns_none_when_no_cards(db):
    """No unenriched cards → returns None (lines 268-270)."""
    from app.services.material_enrichment_service import batch_enrich_materials

    # All cards are already enriched
    _make_card(db, "ENRICHED", enriched_at=datetime.now(timezone.utc))

    mock_redis = MagicMock()
    mock_redis.get.return_value = None
    with patch("app.services.material_enrichment_service._get_redis", return_value=mock_redis):
        result = await batch_enrich_materials(db)
    assert result is None


@pytest.mark.asyncio
async def test_batch_enrich_claude_unavailable_returns_none(db):
    """ClaudeUnavailableError → returns None (lines 294-296)."""
    from app.services.material_enrichment_service import batch_enrich_materials
    from app.utils.claude_errors import ClaudeUnavailableError

    _make_card(db, "PART-A")

    mock_redis = MagicMock()
    mock_redis.get.return_value = None
    with (
        patch("app.services.material_enrichment_service._get_redis", return_value=mock_redis),
        patch(
            "app.services.material_enrichment_service.claude_batch_submit",
            new_callable=AsyncMock,
            side_effect=ClaudeUnavailableError("not configured"),
        ),
    ):
        result = await batch_enrich_materials(db)
    assert result is None


@pytest.mark.asyncio
async def test_batch_enrich_claude_error_returns_none(db):
    """ClaudeError → returns None (lines 297-299)."""
    from app.services.material_enrichment_service import batch_enrich_materials
    from app.utils.claude_errors import ClaudeError

    _make_card(db, "PART-B")

    mock_redis = MagicMock()
    mock_redis.get.return_value = None
    with (
        patch("app.services.material_enrichment_service._get_redis", return_value=mock_redis),
        patch(
            "app.services.material_enrichment_service.claude_batch_submit",
            new_callable=AsyncMock,
            side_effect=ClaudeError("api error"),
        ),
    ):
        result = await batch_enrich_materials(db)
    assert result is None


@pytest.mark.asyncio
async def test_batch_enrich_none_batch_id_returns_none(db):
    """claude_batch_submit returns None → returns None (lines 300-302)."""
    from app.services.material_enrichment_service import batch_enrich_materials

    _make_card(db, "PART-C")

    mock_redis = MagicMock()
    mock_redis.get.return_value = None
    with (
        patch("app.services.material_enrichment_service._get_redis", return_value=mock_redis),
        patch(
            "app.services.material_enrichment_service.claude_batch_submit",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        result = await batch_enrich_materials(db)
    assert result is None


@pytest.mark.asyncio
async def test_batch_enrich_success_stores_batch_id(db):
    """Successful submit → stores batch_id in Redis and returns it (lines 304-308)."""
    from app.services.material_enrichment_service import batch_enrich_materials

    card = _make_card(db, "PART-D")

    mock_redis = MagicMock()
    mock_redis.get.return_value = None
    with (
        patch("app.services.material_enrichment_service._get_redis", return_value=mock_redis),
        patch(
            "app.services.material_enrichment_service.claude_batch_submit",
            new_callable=AsyncMock,
            return_value="batch-xyz-123",
        ),
    ):
        result = await batch_enrich_materials(db)

    assert result == "batch-xyz-123"
    mock_redis.set.assert_called_once_with("batch:material_enrich:current", "batch-xyz-123")


@pytest.mark.asyncio
async def test_batch_enrich_no_redis_still_submits(db):
    """No Redis available → still submits and returns batch_id."""
    from app.services.material_enrichment_service import batch_enrich_materials

    _make_card(db, "PART-E")

    with (
        patch("app.services.material_enrichment_service._get_redis", return_value=None),
        patch(
            "app.services.material_enrichment_service.claude_batch_submit",
            new_callable=AsyncMock,
            return_value="batch-no-redis",
        ),
    ):
        result = await batch_enrich_materials(db)
    assert result == "batch-no-redis"


# ── process_material_batch_results ───────────────────────────────────


@pytest.mark.asyncio
async def test_process_batch_returns_none_when_no_redis(db):
    """No Redis → returns None immediately (lines 321-323)."""
    from app.services.material_enrichment_service import process_material_batch_results

    with patch("app.services.material_enrichment_service._get_redis", return_value=None):
        result = await process_material_batch_results(db)
    assert result is None


@pytest.mark.asyncio
async def test_process_batch_returns_none_when_no_key(db):
    """Redis key not set → returns None (lines 325-327)."""
    from app.services.material_enrichment_service import process_material_batch_results

    mock_redis = MagicMock()
    mock_redis.get.return_value = None
    with patch("app.services.material_enrichment_service._get_redis", return_value=mock_redis):
        result = await process_material_batch_results(db)
    assert result is None


@pytest.mark.asyncio
async def test_process_batch_returns_none_when_still_processing(db):
    """claude_batch_results returns None → still processing (lines 337-338)."""
    from app.services.material_enrichment_service import process_material_batch_results

    mock_redis = MagicMock()
    mock_redis.get.return_value = b"batch-123"
    with (
        patch("app.services.material_enrichment_service._get_redis", return_value=mock_redis),
        patch(
            "app.services.material_enrichment_service.claude_batch_results",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        result = await process_material_batch_results(db)
    assert result is None


@pytest.mark.asyncio
async def test_process_batch_claude_error_returns_none(db):
    """ClaudeError during poll → returns None (lines 332-335)."""
    from app.services.material_enrichment_service import process_material_batch_results
    from app.utils.claude_errors import ClaudeError

    mock_redis = MagicMock()
    mock_redis.get.return_value = "batch-456"
    with (
        patch("app.services.material_enrichment_service._get_redis", return_value=mock_redis),
        patch(
            "app.services.material_enrichment_service.claude_batch_results",
            new_callable=AsyncMock,
            side_effect=ClaudeError("poll failed"),
        ),
    ):
        result = await process_material_batch_results(db)
    assert result is None


@pytest.mark.asyncio
async def test_process_batch_applies_results_to_cards(db):
    """Valid results → cards enriched, Redis key deleted (lines 340-392)."""
    from app.services.material_enrichment_service import process_material_batch_results

    card = _make_card(db, "LM317T", manufacturer="TI")
    batch_results = {
        f"mat_enrich-{card.id}": {
            "parts": [
                {
                    "mpn": "LM317T",
                    "description": "Adjustable voltage regulator",
                    "category": "power_ic",
                    "lifecycle_status": "active",
                }
            ]
        }
    }
    mock_redis = MagicMock()
    mock_redis.get.return_value = b"batch-789"
    with (
        patch("app.services.material_enrichment_service._get_redis", return_value=mock_redis),
        patch(
            "app.services.material_enrichment_service.claude_batch_results",
            new_callable=AsyncMock,
            return_value=batch_results,
        ),
    ):
        stats = await process_material_batch_results(db)

    assert stats is not None
    assert stats["applied"] == 1
    assert stats["errors"] == 0
    mock_redis.delete.assert_called_once()

    db.refresh(card)
    assert card.description == "Adjustable voltage regulator"
    assert card.enrichment_source == "batch_api"


@pytest.mark.asyncio
async def test_process_batch_handles_none_result_for_item(db):
    """None result for a custom_id → error counted (lines 343-346)."""
    from app.services.material_enrichment_service import process_material_batch_results

    card = _make_card(db, "BADPART")
    batch_results = {
        f"mat_enrich-{card.id}": None,
    }
    mock_redis = MagicMock()
    mock_redis.get.return_value = b"batch-bad"
    with (
        patch("app.services.material_enrichment_service._get_redis", return_value=mock_redis),
        patch(
            "app.services.material_enrichment_service.claude_batch_results",
            new_callable=AsyncMock,
            return_value=batch_results,
        ),
    ):
        stats = await process_material_batch_results(db)

    assert stats["errors"] == 1
    assert stats["applied"] == 0


@pytest.mark.asyncio
async def test_process_batch_handles_bad_custom_id_format(db):
    """custom_id without '-' separator → error counted (lines 349-354)."""
    from app.services.material_enrichment_service import process_material_batch_results

    batch_results = {
        "nohyphen": {"parts": [{"mpn": "X", "description": "x", "category": "other", "lifecycle_status": "active"}]},
    }
    mock_redis = MagicMock()
    mock_redis.get.return_value = b"batch-fmt"
    with (
        patch("app.services.material_enrichment_service._get_redis", return_value=mock_redis),
        patch(
            "app.services.material_enrichment_service.claude_batch_results",
            new_callable=AsyncMock,
            return_value=batch_results,
        ),
    ):
        stats = await process_material_batch_results(db)

    assert stats["errors"] >= 1


@pytest.mark.asyncio
async def test_process_batch_handles_missing_card(db):
    """Card not found in DB → error counted (lines 360-362)."""
    from app.services.material_enrichment_service import process_material_batch_results

    batch_results = {
        "mat_enrich-99999": {
            "parts": [{"mpn": "X", "description": "x", "category": "other", "lifecycle_status": "active"}]
        }
    }
    mock_redis = MagicMock()
    mock_redis.get.return_value = b"batch-missing"
    with (
        patch("app.services.material_enrichment_service._get_redis", return_value=mock_redis),
        patch(
            "app.services.material_enrichment_service.claude_batch_results",
            new_callable=AsyncMock,
            return_value=batch_results,
        ),
    ):
        stats = await process_material_batch_results(db)

    assert stats["errors"] >= 1


@pytest.mark.asyncio
async def test_process_batch_handles_empty_parts_list(db):
    """Empty parts list in result → error counted (lines 364-366)."""
    from app.services.material_enrichment_service import process_material_batch_results

    card = _make_card(db, "EMPTY-PARTS")
    batch_results = {
        f"mat_enrich-{card.id}": {"parts": []},
    }
    mock_redis = MagicMock()
    mock_redis.get.return_value = b"batch-empty"
    with (
        patch("app.services.material_enrichment_service._get_redis", return_value=mock_redis),
        patch(
            "app.services.material_enrichment_service.claude_batch_results",
            new_callable=AsyncMock,
            return_value=batch_results,
        ),
    ):
        stats = await process_material_batch_results(db)

    assert stats["errors"] >= 1
