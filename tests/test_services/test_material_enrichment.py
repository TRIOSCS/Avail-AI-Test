"""Tests for material enrichment service — AI description + commodity classification."""

import os

os.environ["TESTING"] = "1"
os.environ["RATE_LIMIT_ENABLED"] = "false"

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.models import Base, MaterialCard
from tests.conftest import TestSessionLocal, engine


@pytest.fixture(autouse=True)
def _tables():
    Base.metadata.create_all(bind=engine)
    yield
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


# ── enrich_material_cards tests ──────────────────────────────────────


@pytest.mark.asyncio
async def test_enrich_material_cards_success(db):
    """Enrichment populates description + category from AI response."""
    from app.services.material_enrichment_service import enrich_material_cards

    card1 = _make_card(db, "LM317T", manufacturer="Texas Instruments")
    card2 = _make_card(db, "STM32F407VG", manufacturer="STMicroelectronics")

    mock_result = {
        "parts": [
            {
                "mpn": "LM317T",
                "description": "Adjustable positive voltage regulator, 1.2V to 37V output",
                "category": "power_ic",
            },
            {
                "mpn": "STM32F407VG",
                "description": "ARM Cortex-M4 microcontroller with 1MB Flash",
                "category": "microcontrollers",
            },
        ]
    }

    with (
        patch(
            "app.utils.claude_client.claude_structured",
            new_callable=AsyncMock,
            return_value=mock_result,
        ),
    ):
        stats = await enrich_material_cards([card1.id, card2.id], db)

    assert stats["enriched"] == 2
    assert stats["errors"] == 0

    db.refresh(card1)
    db.refresh(card2)
    assert card1.description == "Adjustable positive voltage regulator, 1.2V to 37V output"
    assert card1.category == "power_ic"
    assert card1.enrichment_source == "claude_haiku"
    assert card1.enriched_at is not None
    assert card2.description == "ARM Cortex-M4 microcontroller with 1MB Flash"


@pytest.mark.asyncio
async def test_enrich_material_cards_invalid_category(db):
    """Invalid category falls back to 'other'."""
    from app.services.material_enrichment_service import enrich_material_cards

    card = _make_card(db, "MYSTERY-PART")

    mock_result = {
        "parts": [
            {
                "mpn": "MYSTERY-PART",
                "description": "Unknown component",
                "category": "bogus_category",
            }
        ]
    }

    with patch(
        "app.utils.claude_client.claude_structured",
        new_callable=AsyncMock,
        return_value=mock_result,
    ):
        stats = await enrich_material_cards([card.id], db)

    assert stats["enriched"] == 1
    db.refresh(card)
    assert card.category == "other"


@pytest.mark.asyncio
async def test_enrich_material_cards_null_description(db):
    """Null description from AI doesn't overwrite existing."""
    from app.services.material_enrichment_service import enrich_material_cards

    card = _make_card(db, "XYZ-PART", description="Existing description")

    mock_result = {
        "parts": [
            {
                "mpn": "XYZ-PART",
                "description": None,
                "category": "other",
            }
        ]
    }

    with patch(
        "app.utils.claude_client.claude_structured",
        new_callable=AsyncMock,
        return_value=mock_result,
    ):
        stats = await enrich_material_cards([card.id], db)

    assert stats["enriched"] == 1
    db.refresh(card)
    assert card.description == "Existing description"
    assert card.category == "other"


@pytest.mark.asyncio
async def test_enrich_material_cards_api_failure(db):
    """Claude API failure counts all cards as errors."""
    from app.services.material_enrichment_service import enrich_material_cards

    card = _make_card(db, "FAIL-PART")

    with patch(
        "app.utils.claude_client.claude_structured",
        new_callable=AsyncMock,
        side_effect=Exception("API timeout"),
    ):
        stats = await enrich_material_cards([card.id], db)

    assert stats["errors"] == 1
    assert stats["enriched"] == 0


@pytest.mark.asyncio
async def test_enrich_material_cards_empty_response(db):
    """Empty/invalid API response counts as errors."""
    from app.services.material_enrichment_service import enrich_material_cards

    card = _make_card(db, "EMPTY-PART")

    with (
        patch(
            "app.utils.claude_client.claude_structured",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "app.utils.claude_client.claude_structured",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        stats = await enrich_material_cards([card.id], db)

    assert stats["errors"] == 1
    assert stats["enriched"] == 0


@pytest.mark.asyncio
async def test_enrich_material_cards_batch_processing(db):
    """Cards are processed in batches of the specified size."""
    from app.services.material_enrichment_service import enrich_material_cards

    cards = [_make_card(db, f"BATCH-{i}") for i in range(5)]

    call_count = 0

    async def mock_claude(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        # Return parts matching the batch size
        return {
            "parts": [
                {"mpn": f"BATCH-{i}", "description": f"Part {i}", "category": "other"}
                for i in range(min(2, 5 - (call_count - 1) * 2))
            ]
        }

    with patch(
        "app.utils.claude_client.claude_structured",
        new_callable=AsyncMock,
        side_effect=mock_claude,
    ):
        stats = await enrich_material_cards([c.id for c in cards], db, batch_size=2)

    # 5 cards / batch_size 2 = 3 Claude calls
    assert call_count == 3
    assert stats["enriched"] == 5


# ── enrich_pending_cards tests ───────────────────────────────────────


@pytest.mark.asyncio
async def test_enrich_pending_cards_no_pending(db):
    """No un-enriched cards returns zeros."""
    from app.services.material_enrichment_service import enrich_pending_cards

    # Card already enriched
    _make_card(db, "ENRICHED-1", enriched_at=datetime.now(timezone.utc))

    result = await enrich_pending_cards(db, limit=10)
    assert result["enriched"] == 0
    assert result["pending"] == 0


@pytest.mark.asyncio
async def test_enrich_pending_cards_picks_unenriched(db):
    """Picks up un-enriched cards."""
    from app.services.material_enrichment_service import enrich_pending_cards

    card = _make_card(db, "NEED-ENRICH")

    mock_result = {
        "parts": [
            {
                "mpn": "NEED-ENRICH",
                "description": "Test part",
                "category": "other",
            }
        ]
    }

    with patch(
        "app.utils.claude_client.claude_structured",
        new_callable=AsyncMock,
        return_value=mock_result,
    ):
        result = await enrich_pending_cards(db, limit=10)

    assert result["enriched"] == 1
    assert result["pending"] == 1
    db.refresh(card)
    assert card.description == "Test part"


# ── VALID_CATEGORIES tests ──────────────────────────────────────────


def test_valid_categories_includes_commodity_map():
    """All COMMODITY_MAP keys are in VALID_CATEGORIES."""
    from app.services.material_enrichment_service import VALID_CATEGORIES
    from app.services.specialty_detector import COMMODITY_MAP

    for key in COMMODITY_MAP:
        assert key in VALID_CATEGORIES


def test_valid_categories_includes_other():
    """'other' is a fallback category."""
    from app.services.material_enrichment_service import VALID_CATEGORIES

    assert "other" in VALID_CATEGORIES


@pytest.mark.asyncio
async def test_enrich_apply_exception_counts_error(db):
    """Exception during card attribute apply is caught and counted as error."""
    from app.services.material_enrichment_service import enrich_material_cards

    card = _make_card(db, "ERR-APPLY")

    # Return a result where ai part is not a dict (will raise on .get())
    mock_result = {"parts": ["not-a-dict"]}

    with patch(
        "app.utils.claude_client.claude_structured",
        new_callable=AsyncMock,
        return_value=mock_result,
    ):
        stats = await enrich_material_cards([card.id], db)

    assert stats["errors"] >= 1
