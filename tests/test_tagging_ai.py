"""Tests for AI fallback classification — mocked Claude responses.

Called by: pytest
Depends on: app.services.tagging_ai, app.models
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.models.intelligence import MaterialCard
from app.models.tags import MaterialTag, Tag
from app.services.tagging_ai import classify_parts_with_ai, run_ai_backfill


# ── Helpers ────────────────────────────────────────────────────────────


def _make_card(db, mpn, manufacturer=None):
    card = MaterialCard(
        normalized_mpn=mpn.lower(),
        display_mpn=mpn,
        manufacturer=manufacturer,
        created_at=datetime.now(timezone.utc),
    )
    db.add(card)
    db.commit()
    db.refresh(card)
    return card


# ── classify_parts_with_ai ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ai_classify_parses_response():
    mock_response = [
        {"mpn": "ABC123", "manufacturer": "Texas Instruments", "category": "Analog ICs"},
        {"mpn": "DEF456", "manufacturer": "Microchip Technology", "category": "Microcontrollers (MCU)"},
    ]

    with patch("app.utils.claude_client.claude_json", new_callable=AsyncMock, return_value=mock_response):
        result = await classify_parts_with_ai(["ABC123", "DEF456"])

    assert len(result) == 2
    assert result[0]["manufacturer"] == "Texas Instruments"
    assert result[1]["category"] == "Microcontrollers (MCU)"


@pytest.mark.asyncio
async def test_ai_classify_handles_malformed_response():
    with patch("app.utils.claude_client.claude_json", new_callable=AsyncMock, return_value=None):
        result = await classify_parts_with_ai(["ABC123"])

    assert len(result) == 1
    assert result[0]["manufacturer"] == "Unknown"
    assert result[0]["category"] == "Miscellaneous"


@pytest.mark.asyncio
async def test_ai_classify_handles_string_response():
    """claude_json returns something that's not a list."""
    with patch("app.utils.claude_client.claude_json", new_callable=AsyncMock, return_value={"error": "bad"}):
        result = await classify_parts_with_ai(["ABC123"])

    assert len(result) == 1
    assert result[0]["manufacturer"] == "Unknown"


# ── run_ai_backfill ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ai_tags_unknown_as_miscellaneous(db_session):
    _make_card(db_session, "ZZZUNKNOWN")

    mock_response = [{"mpn": "zzzunknown", "manufacturer": "Unknown", "category": "Miscellaneous"}]

    with patch("app.utils.claude_client.claude_json", new_callable=AsyncMock, return_value=mock_response):
        result = await run_ai_backfill(db_session, batch_size=10)

    assert result["total_unknown"] == 1
    # Should still create a tag, but with low confidence
    mt = db_session.query(MaterialTag).first()
    assert mt is not None
    assert mt.confidence == 0.3
    assert mt.source == "ai_classified"


@pytest.mark.asyncio
async def test_ai_backfill_processes_remaining(db_session):
    _make_card(db_session, "AIPART1")
    _make_card(db_session, "AIPART2")

    mock_response = [
        {"mpn": "aipart1", "manufacturer": "Infineon", "category": "Power Management ICs"},
        {"mpn": "aipart2", "manufacturer": "NXP", "category": "Interface ICs"},
    ]

    with patch("app.utils.claude_client.claude_json", new_callable=AsyncMock, return_value=mock_response):
        result = await run_ai_backfill(db_session, batch_size=10)

    assert result["total_processed"] == 2
    assert result["total_matched"] == 2
    assert db_session.query(MaterialTag).count() >= 2


@pytest.mark.asyncio
async def test_ai_backfill_skips_already_tagged(db_session):
    card = _make_card(db_session, "AITAGGED")
    tag = Tag(name="Existing", tag_type="brand", created_at=datetime.now(timezone.utc))
    db_session.add(tag)
    db_session.flush()
    db_session.add(MaterialTag(material_card_id=card.id, tag_id=tag.id, confidence=0.9, source="existing_data"))
    db_session.commit()

    with patch("app.utils.claude_client.claude_json", new_callable=AsyncMock) as mock_claude:
        result = await run_ai_backfill(db_session, batch_size=10)

    assert result["total_processed"] == 0
    mock_claude.assert_not_called()


@pytest.mark.asyncio
async def test_ai_backfill_updates_manufacturer(db_session):
    card = _make_card(db_session, "DISCOVER1")

    mock_response = [{"mpn": "discover1", "manufacturer": "Renesas", "category": "Microcontrollers (MCU)"}]

    with patch("app.utils.claude_client.claude_json", new_callable=AsyncMock, return_value=mock_response):
        await run_ai_backfill(db_session, batch_size=10)

    db_session.refresh(card)
    assert card.manufacturer == "Renesas"
