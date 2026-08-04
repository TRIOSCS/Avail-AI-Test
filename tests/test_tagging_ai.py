"""Tests for AI fallback classification — mocked Claude responses.

Called by: pytest
Depends on: app.services.tagging_ai, app.models
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from app.models.intelligence import MaterialCard
from app.models.tags import MaterialTag, Tag
from app.services.tagging_ai import classify_parts_with_ai, run_ai_backfill

# ── Helpers ────────────────────────────────────────────────────────────


def _make_card(db, mpn, manufacturer=None, **kw):
    card = MaterialCard(
        normalized_mpn=mpn.lower(),
        display_mpn=mpn,
        manufacturer=manufacturer,
        created_at=datetime.now(UTC),
        **kw,
    )
    db.add(card)
    db.commit()
    db.refresh(card)
    return card


def _claude_configured():
    """run_ai_backfill early-exits when no Anthropic key is configured — most tests
    simulate a configured deployment."""
    return patch("app.services.credential_service.get_credential_cached", return_value="test-key")


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

    with (
        _claude_configured(),
        patch("app.utils.claude_client.claude_json", new_callable=AsyncMock, return_value=mock_response),
    ):
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

    with (
        _claude_configured(),
        patch("app.utils.claude_client.claude_json", new_callable=AsyncMock, return_value=mock_response),
    ):
        result = await run_ai_backfill(db_session, batch_size=10)

    assert result["total_processed"] == 2
    assert result["total_matched"] == 2
    assert db_session.query(MaterialTag).count() >= 2


@pytest.mark.asyncio
async def test_ai_backfill_skips_already_tagged(db_session):
    card = _make_card(db_session, "AITAGGED")
    tag = Tag(name="Existing", tag_type="brand", created_at=datetime.now(UTC))
    db_session.add(tag)
    db_session.flush()
    db_session.add(MaterialTag(material_card_id=card.id, tag_id=tag.id, confidence=0.9, source="existing_data"))
    db_session.commit()

    with (
        _claude_configured(),
        patch("app.utils.claude_client.claude_json", new_callable=AsyncMock) as mock_claude,
    ):
        result = await run_ai_backfill(db_session, batch_size=10)

    assert result["total_processed"] == 0
    mock_claude.assert_not_called()


@pytest.mark.asyncio
async def test_ai_backfill_updates_manufacturer(db_session):
    card = _make_card(db_session, "DISCOVER1")

    mock_response = [{"mpn": "discover1", "manufacturer": "Renesas", "category": "Microcontrollers (MCU)"}]

    with (
        _claude_configured(),
        patch("app.utils.claude_client.claude_json", new_callable=AsyncMock, return_value=mock_response),
    ):
        await run_ai_backfill(db_session, batch_size=10)

    db_session.refresh(card)
    assert card.manufacturer == "Renesas"


@pytest.mark.asyncio
async def test_ai_backfill_unconfigured_exits_before_walking_cards(db_session):
    """No Anthropic key → one INFO early exit; no Claude calls, no Unknown tags."""
    _make_card(db_session, "NOKEY1")

    with (
        patch("app.services.credential_service.get_credential_cached", return_value=None),
        patch("app.utils.claude_client.claude_json", new_callable=AsyncMock) as mock_claude,
    ):
        result = await run_ai_backfill(db_session, batch_size=10)

    assert result == {"total_processed": 0, "total_matched": 0, "total_unknown": 0}
    mock_claude.assert_not_called()
    assert db_session.query(MaterialTag).count() == 0


@pytest.mark.asyncio
async def test_ai_backfill_limit_and_exclude_internal(db_session):
    """Limit caps the cards per run; exclude_internal drops internal parts."""
    _make_card(db_session, "INTERNALPN", is_internal_part=True)
    _make_card(db_session, "REALPART1")
    _make_card(db_session, "REALPART2")

    mock_response = [{"mpn": "realpart1", "manufacturer": "Infineon", "category": "Power Management ICs"}]

    with (
        _claude_configured(),
        patch("app.utils.claude_client.claude_json", new_callable=AsyncMock, return_value=mock_response) as mc,
    ):
        result = await run_ai_backfill(db_session, batch_size=10, limit=1, exclude_internal=True)

    assert result["total_processed"] == 1
    assert result["total_matched"] == 1
    mc.assert_called_once()
    # The internal part was never sent to Claude.
    assert "internalpn" not in str(mc.call_args)


# ── triage_internal_parts (heuristics) ─────────────────────────────────


def test_triage_internal_parts_heuristics():
    """Heuristic triage catches obvious internal part numbers."""
    from app.services.tagging_ai import triage_internal_parts

    results = triage_internal_parts(
        [
            "123456789",  # pure numeric
            "AB",  # too short
            "INT-CUST-001",  # internal marker
            "STM32F407VGT6",  # real MPN
            "TEST-SAMPLE-XYZ",  # internal marker
            "LM317T",  # real MPN
            "[BRACKET]",  # unusual chars
        ]
    )

    # Map results by mpn
    by_mpn = {r["mpn"]: r for r in results}

    assert by_mpn["123456789"]["is_internal"] is True
    assert by_mpn["AB"]["is_internal"] is True
    assert by_mpn["INT-CUST-001"]["is_internal"] is True
    assert by_mpn["STM32F407VGT6"]["is_internal"] is False
    assert by_mpn["TEST-SAMPLE-XYZ"]["is_internal"] is True
    assert by_mpn["LM317T"]["is_internal"] is False
    assert by_mpn["[BRACKET]"]["is_internal"] is True
