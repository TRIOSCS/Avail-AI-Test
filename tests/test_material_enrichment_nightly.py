"""test_material_enrichment_nightly.py — Coverage for uncovered branches in
material_enrichment_service.py.

Targets:
- enrich_material_cards (lines 91-107): query + batch loop
- _enrich_batch manufacturer path (line 118), AI call (140-143),
  empty response (146-148), mismatched count (153-155), per-card loop (161-163)
- enrich_pending_cards full path (lines 182-220)
- batch_enrich_materials empty-requests guard (line 295)
- process_material_batch_results non-int card_id (360-362),
  apply exception (379-381), commit failure (385-388)

Called by: pytest
Depends on: app/services/material_enrichment_service.py
"""

import os

os.environ["TESTING"] = "1"

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models import MaterialCard, Requirement, Requisition, User
from tests.conftest import engine  # noqa: F401


@pytest.fixture
def db(db_session: Session):
    """Alias for db_session."""
    return db_session


def _make_card(db: Session, mpn: str, *, manufacturer: str | None = None, enriched_at=None) -> MaterialCard:
    card = MaterialCard(
        normalized_mpn=mpn.lower().replace("-", "").replace(" ", ""),
        display_mpn=mpn,
        manufacturer=manufacturer,
        enriched_at=enriched_at,
        search_count=1,
    )
    db.add(card)
    db.commit()
    db.refresh(card)
    return card


def _make_card_with_requirement(db: Session, mpn: str) -> MaterialCard:
    """Create a material card linked to an active requisition requirement."""
    user = User(
        email=f"user_{mpn}@test.com",
        name="Tester",
        role="buyer",
        azure_id=f"azure-{mpn}",
    )
    db.add(user)
    db.flush()

    req = Requisition(
        name=f"REQ-{mpn}",
        customer_name="Test Corp",
        status="active",
        created_by=user.id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(req)
    db.flush()

    card = MaterialCard(
        normalized_mpn=mpn.lower(),
        display_mpn=mpn,
        manufacturer="TI",
        search_count=5,
    )
    db.add(card)
    db.flush()

    item = Requirement(
        requisition_id=req.id,
        primary_mpn=mpn,
        target_qty=100,
        material_card_id=card.id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(item)
    db.commit()
    db.refresh(card)
    return card


# ── enrich_material_cards (lines 91-107) ──────────────────────────────


@pytest.mark.asyncio
async def test_enrich_material_cards_queries_and_batches(db: Session):
    """enrich_material_cards fetches cards by ID and processes them in batches."""
    from app.services.material_enrichment_service import enrich_material_cards

    card1 = _make_card(db, "LM317T", manufacturer="TI")
    card2 = _make_card(db, "NE555")

    mock_result = {
        "parts": [
            {"mpn": "LM317T", "description": "Voltage regulator", "category": "power_ic", "lifecycle_status": "active"},
            {"mpn": "NE555", "description": "Timer IC", "category": "standard_logic", "lifecycle_status": "active"},
        ]
    }

    with patch("app.services.material_enrichment_service._enrich_batch", new_callable=AsyncMock) as mock_batch:
        stats = await enrich_material_cards([card1.id, card2.id], db, batch_size=30)

    mock_batch.assert_called_once()
    assert stats == {"enriched": 0, "skipped": 0, "errors": 0}


@pytest.mark.asyncio
async def test_enrich_material_cards_uses_batch_size(db: Session):
    """enrich_material_cards splits cards into batches of batch_size."""
    from app.services.material_enrichment_service import enrich_material_cards

    cards = [_make_card(db, f"PART{i}") for i in range(5)]
    card_ids = [c.id for c in cards]

    call_args = []

    async def _capture_batch(chunk, db_, stats_):
        call_args.append(len(chunk))

    with patch("app.services.material_enrichment_service._enrich_batch", side_effect=_capture_batch):
        await enrich_material_cards(card_ids, db, batch_size=3)

    assert len(call_args) == 2
    assert call_args[0] == 3
    assert call_args[1] == 2


@pytest.mark.asyncio
async def test_enrich_material_cards_empty_list_returns_zero_stats(db: Session):
    """enrich_material_cards with no matching IDs returns zero stats."""
    from app.services.material_enrichment_service import enrich_material_cards

    stats = await enrich_material_cards([999999], db)
    assert stats == {"enriched": 0, "skipped": 0, "errors": 0}


@pytest.mark.asyncio
async def test_enrich_material_cards_skips_deleted_cards(db: Session):
    """Soft-deleted cards (deleted_at set) are excluded from enrichment."""
    from app.services.material_enrichment_service import enrich_material_cards

    card = _make_card(db, "DELETED-PART")
    card.deleted_at = datetime.now(timezone.utc)
    db.commit()

    with patch("app.services.material_enrichment_service._enrich_batch", new_callable=AsyncMock) as mock_batch:
        await enrich_material_cards([card.id], db)

    mock_batch.assert_not_called()


# ── _enrich_batch branches ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_enrich_batch_with_manufacturer_includes_it_in_prompt(db: Session):
    """Line 118: manufacturer present → included in prompt text."""
    from app.services.material_enrichment_service import _enrich_batch

    card = _make_card(db, "LM7805", manufacturer="Fairchild")
    stats = {"enriched": 0, "skipped": 0, "errors": 0}

    mock_result = {
        "parts": [
            {"mpn": "LM7805", "description": "5V regulator", "category": "power_ic", "lifecycle_status": "active"}
        ]
    }

    captured_prompt = []

    async def _fake_claude(prompt, schema, *, system, model_tier, max_tokens, timeout):
        captured_prompt.append(prompt)
        return mock_result

    with patch("app.utils.claude_client.claude_structured", side_effect=_fake_claude):
        await _enrich_batch([card], db, stats)

    assert stats["enriched"] == 1
    assert len(captured_prompt) == 1
    assert "Fairchild" in captured_prompt[0]


@pytest.mark.asyncio
async def test_enrich_batch_ai_exception_increments_errors(db: Session):
    """Lines 140-143: exception from claude_structured → errors += len(cards)."""
    from app.services.material_enrichment_service import _enrich_batch

    card = _make_card(db, "FAILPART")
    stats = {"enriched": 0, "skipped": 0, "errors": 0}

    with patch("app.utils.claude_client.claude_structured", side_effect=RuntimeError("timeout")):
        await _enrich_batch([card], db, stats)

    assert stats["errors"] == 1
    assert stats["enriched"] == 0


@pytest.mark.asyncio
async def test_enrich_batch_empty_response_increments_errors(db: Session):
    """Lines 146-148: empty result dict → errors += len(cards)."""
    from app.services.material_enrichment_service import _enrich_batch

    card = _make_card(db, "EMPTYRESP")
    stats = {"enriched": 0, "skipped": 0, "errors": 0}

    with patch("app.utils.claude_client.claude_structured", new_callable=AsyncMock, return_value={}):
        await _enrich_batch([card], db, stats)

    assert stats["errors"] == 1


@pytest.mark.asyncio
async def test_enrich_batch_none_response_increments_errors(db: Session):
    """Lines 146-148: None result → errors += len(cards)."""
    from app.services.material_enrichment_service import _enrich_batch

    card = _make_card(db, "NONERESULT")
    stats = {"enriched": 0, "skipped": 0, "errors": 0}

    with patch("app.utils.claude_client.claude_structured", new_callable=AsyncMock, return_value=None):
        await _enrich_batch([card], db, stats)

    assert stats["errors"] == 1


@pytest.mark.asyncio
async def test_enrich_batch_mismatched_count_skips_batch(db: Session):
    """Lines 153-155: AI returns wrong number of parts → errors += len(cards)."""
    from app.services.material_enrichment_service import _enrich_batch

    card1 = _make_card(db, "PARTA")
    card2 = _make_card(db, "PARTB")
    stats = {"enriched": 0, "skipped": 0, "errors": 0}

    mock_result = {
        "parts": [
            {"mpn": "PARTA", "description": "Part A", "category": "other", "lifecycle_status": "active"},
        ]
    }

    with patch("app.utils.claude_client.claude_structured", new_callable=AsyncMock, return_value=mock_result):
        await _enrich_batch([card1, card2], db, stats)

    assert stats["errors"] == 2
    assert stats["enriched"] == 0


@pytest.mark.asyncio
async def test_enrich_batch_per_card_apply_exception(db: Session):
    """Lines 161-163: _apply_enrichment_result raises → that card counted as error."""
    from app.services.material_enrichment_service import _enrich_batch

    card1 = _make_card(db, "GOODPART")
    card2 = _make_card(db, "BADPART2")
    stats = {"enriched": 0, "skipped": 0, "errors": 0}

    mock_result = {
        "parts": [
            {"mpn": "GOODPART", "description": "Good", "category": "other", "lifecycle_status": "active"},
            {"mpn": "BADPART2", "description": "Bad", "category": "other", "lifecycle_status": "active"},
        ]
    }

    # Capture the real function before patching so we can delegate to it from the mock
    from app.services.material_enrichment_service import _apply_enrichment_result as real_apply

    call_count = [0]

    def _sometimes_fail(card, ai):
        call_count[0] += 1
        if call_count[0] == 2:
            raise ValueError("apply failed")
        real_apply(card, ai)

    with (
        patch("app.utils.claude_client.claude_structured", new_callable=AsyncMock, return_value=mock_result),
        patch(
            "app.services.material_enrichment_service._apply_enrichment_result",
            side_effect=_sometimes_fail,
        ),
    ):
        await _enrich_batch([card1, card2], db, stats)

    assert stats["enriched"] == 1
    assert stats["errors"] == 1


@pytest.mark.asyncio
async def test_enrich_batch_successful_enrichment(db: Session):
    """Lines 157-163: successful per-card enrichment increments enriched."""
    from app.services.material_enrichment_service import _enrich_batch

    card = _make_card(db, "LM741")
    stats = {"enriched": 0, "skipped": 0, "errors": 0}

    mock_result = {
        "parts": [{"mpn": "LM741", "description": "Op-amp", "category": "amplifiers", "lifecycle_status": "active"}]
    }

    with patch("app.utils.claude_client.claude_structured", new_callable=AsyncMock, return_value=mock_result):
        await _enrich_batch([card], db, stats)

    assert stats["enriched"] == 1
    assert stats["errors"] == 0
    db.refresh(card)
    assert card.description == "Op-amp"
    assert card.enrichment_source == "claude_haiku"


# ── enrich_pending_cards (lines 182-220) ──────────────────────────────


@pytest.mark.asyncio
async def test_enrich_pending_cards_returns_early_when_no_pending(db: Session):
    """Line 215-216: no unenriched cards → returns zero stats with pending key."""
    from app.services.material_enrichment_service import enrich_pending_cards

    _make_card(db, "ALREADY-DONE", enriched_at=datetime.now(timezone.utc))

    result = await enrich_pending_cards(db)
    assert result == {"enriched": 0, "skipped": 0, "errors": 0, "pending": 0}


@pytest.mark.asyncio
async def test_enrich_pending_cards_prioritises_requirement_linked_cards(db: Session):
    """Lines 182-220: cards linked to requirements come first."""
    from app.services.material_enrichment_service import enrich_pending_cards

    card_with_req = _make_card_with_requirement(db, "REQ-LINKED")
    card_standalone = _make_card(db, "STANDALONE")

    called_ids = []

    async def _capture(card_ids, db_, *, batch_size):
        called_ids.extend(card_ids)
        return {"enriched": 2, "skipped": 0, "errors": 0}

    with patch("app.services.material_enrichment_service.enrich_material_cards", side_effect=_capture):
        result = await enrich_pending_cards(db, limit=300, batch_size=30)

    assert card_with_req.id in called_ids
    assert card_standalone.id in called_ids
    assert result["pending"] == 2


@pytest.mark.asyncio
async def test_enrich_pending_cards_fills_remaining_from_any_unenriched(db: Session):
    """Lines 198-213: when requirement-linked cards < limit, fills remaining slots."""
    from app.services.material_enrichment_service import enrich_pending_cards

    standalone1 = _make_card(db, "STANDALONE-1")
    standalone2 = _make_card(db, "STANDALONE-2")

    called_ids = []

    async def _capture(card_ids, db_, *, batch_size):
        called_ids.extend(card_ids)
        return {"enriched": len(card_ids), "skipped": 0, "errors": 0}

    with patch("app.services.material_enrichment_service.enrich_material_cards", side_effect=_capture):
        result = await enrich_pending_cards(db, limit=300)

    assert standalone1.id in called_ids
    assert standalone2.id in called_ids
    assert result["pending"] == 2


@pytest.mark.asyncio
async def test_enrich_pending_cards_respects_limit(db: Session):
    """enrich_pending_cards respects the limit parameter."""
    from app.services.material_enrichment_service import enrich_pending_cards

    for i in range(10):
        _make_card(db, f"BULK-PART-{i}")

    called_ids = []

    async def _capture(card_ids, db_, *, batch_size):
        called_ids.extend(card_ids)
        return {"enriched": len(card_ids), "skipped": 0, "errors": 0}

    with patch("app.services.material_enrichment_service.enrich_material_cards", side_effect=_capture):
        result = await enrich_pending_cards(db, limit=5)

    assert len(called_ids) <= 5
    assert result["pending"] == len(called_ids)


@pytest.mark.asyncio
async def test_enrich_pending_cards_returns_pending_count_in_result(db: Session):
    """Line 219: result dict includes 'pending' key equal to number of card_ids."""
    from app.services.material_enrichment_service import enrich_pending_cards

    _make_card(db, "PENDING-A")
    _make_card(db, "PENDING-B")

    async def _fake_enrich(card_ids, db_, *, batch_size):
        return {"enriched": 2, "skipped": 0, "errors": 0}

    with patch("app.services.material_enrichment_service.enrich_material_cards", side_effect=_fake_enrich):
        result = await enrich_pending_cards(db)

    assert "pending" in result
    assert result["pending"] == 2


# ── batch_enrich_materials empty requests guard (line 295) ────────────


@pytest.mark.asyncio
async def test_batch_enrich_returns_none_when_batch_queue_empty(db: Session):
    """Line 295: build_batch() returns [] → returns None before submitting."""
    from app.services.material_enrichment_service import batch_enrich_materials

    _make_card(db, "PART-FOR-EMPTY-BATCH")

    mock_redis = MagicMock()
    mock_redis.get.return_value = None

    with (
        patch("app.services.material_enrichment_service._get_redis", return_value=mock_redis),
        patch("app.services.material_enrichment_service.BatchQueue") as mock_bq_cls,
    ):
        mock_bq = MagicMock()
        mock_bq.build_batch.return_value = []
        mock_bq_cls.return_value = mock_bq

        result = await batch_enrich_materials(db)

    assert result is None


# ── process_material_batch_results: non-int card_id (360-362) ─────────


@pytest.mark.asyncio
async def test_process_batch_handles_non_integer_card_id(db: Session):
    """Lines 360-362: card_id portion is not a valid int → error counted."""
    from app.services.material_enrichment_service import process_material_batch_results

    batch_results = {
        "mat_enrich-notanumber": {
            "parts": [{"mpn": "X", "description": "X", "category": "other", "lifecycle_status": "active"}]
        }
    }
    mock_redis = MagicMock()
    mock_redis.get.return_value = b"batch-nonint"
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
    assert stats["errors"] >= 1


# ── process_material_batch_results: apply exception (379-381) ─────────


@pytest.mark.asyncio
async def test_process_batch_apply_exception_increments_error(db: Session):
    """Lines 379-381: _apply_enrichment_result raises during batch processing → error."""
    from app.services.material_enrichment_service import process_material_batch_results

    card = _make_card(db, "APPLY-FAIL-CARD")

    batch_results = {
        f"mat_enrich-{card.id}": {
            "parts": [
                {"mpn": "APPLY-FAIL-CARD", "description": "desc", "category": "other", "lifecycle_status": "active"}
            ]
        }
    }
    mock_redis = MagicMock()
    mock_redis.get.return_value = b"batch-apply-fail"

    with (
        patch("app.services.material_enrichment_service._get_redis", return_value=mock_redis),
        patch(
            "app.services.material_enrichment_service.claude_batch_results",
            new_callable=AsyncMock,
            return_value=batch_results,
        ),
        patch(
            "app.services.material_enrichment_service._apply_enrichment_result",
            side_effect=ValueError("apply broke"),
        ),
    ):
        stats = await process_material_batch_results(db)

    assert stats is not None
    assert stats["errors"] >= 1
    assert stats["applied"] == 0


# ── process_material_batch_results: commit failure (385-388) ──────────


@pytest.mark.asyncio
async def test_process_batch_commit_failure_returns_stats(db: Session):
    """Lines 385-388: commit failure → rollback, still return stats (not None)."""
    from app.services.material_enrichment_service import process_material_batch_results

    card = _make_card(db, "COMMIT-FAIL-CARD")

    batch_results = {
        f"mat_enrich-{card.id}": {
            "parts": [
                {
                    "mpn": "COMMIT-FAIL-CARD",
                    "description": "desc",
                    "category": "other",
                    "lifecycle_status": "active",
                }
            ]
        }
    }
    mock_redis = MagicMock()
    mock_redis.get.return_value = b"batch-commit-fail"

    original_commit = db.commit
    commit_calls = [0]

    def _fail_commit():
        commit_calls[0] += 1
        if commit_calls[0] == 1:
            raise Exception("DB commit failed")
        return original_commit()

    db.commit = _fail_commit
    try:
        with (
            patch("app.services.material_enrichment_service._get_redis", return_value=mock_redis),
            patch(
                "app.services.material_enrichment_service.claude_batch_results",
                new_callable=AsyncMock,
                return_value=batch_results,
            ),
        ):
            stats = await process_material_batch_results(db)
    finally:
        db.commit = original_commit

    # Commit failed → returns stats dict (not None), Redis.delete NOT called
    assert stats is not None
    assert "applied" in stats
    mock_redis.delete.assert_not_called()
