"""Tests for AI fallback classification — mocked Claude responses.

Called by: pytest
Depends on: app.services.tagging_ai, app.models
"""

from unittest.mock import AsyncMock, patch

import pytest

from app.services.tagging_ai import classify_parts_with_ai

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
