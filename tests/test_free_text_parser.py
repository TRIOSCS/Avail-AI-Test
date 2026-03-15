"""test_free_text_parser.py — Tests for AI free-text RFQ/Offer parsing.

Tests the parsing service, schemas, and router endpoints for the
free-text paste → AI parse → review → save flow.

Covers: parse_free_text service, schema validation, parse/save-rfq/save-offers endpoints
"""

from unittest.mock import AsyncMock, patch

import pytest  # noqa: I001

# ---------------------------------------------------------------------------
# Service unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parse_free_text_empty():
    """Empty text returns None."""
    from app.services.free_text_parser import parse_free_text

    result = await parse_free_text("")
    assert result is None

    result = await parse_free_text("   ")
    assert result is None


@pytest.mark.asyncio
async def test_parse_free_text_success():
    """Successful parse returns structured data with line_items."""
    mock_result = {
        "document_type": "rfq",
        "confidence": 0.9,
        "company_name": "Acme Corp",
        "contact_name": "John Doe",
        "contact_email": "john@acme.com",
        "notes": "Need ASAP",
        "line_items": [
            {"mpn": "LM358N", "quantity": 100, "target_price": 0.50, "condition": "new"},
            {"mpn": "NE555P", "quantity": 500},
        ],
    }

    with patch(
        "app.services.free_text_parser.claude_structured",
        new_callable=AsyncMock,
        return_value=mock_result,
    ):
        from app.services.free_text_parser import parse_free_text

        result = await parse_free_text("Looking for LM358N x100 and NE555P x500")

    assert result is not None
    assert result["document_type"] == "rfq"
    assert result["confidence"] == 0.9
    assert len(result["line_items"]) == 2
    assert result["line_items"][0]["mpn"] == "LM358N"


@pytest.mark.asyncio
async def test_parse_free_text_offer():
    """Offer-type text is correctly classified."""
    mock_result = {
        "document_type": "offer",
        "confidence": 0.85,
        "company_name": "Parts Direct",
        "line_items": [
            {"mpn": "STM32F103", "quantity": 1000, "target_price": 2.50, "condition": "new"},
        ],
    }

    with patch(
        "app.services.free_text_parser.claude_structured",
        new_callable=AsyncMock,
        return_value=mock_result,
    ):
        from app.services.free_text_parser import parse_free_text

        result = await parse_free_text("We have STM32F103 x1000 at $2.50 each")

    assert result["document_type"] == "offer"
    assert len(result["line_items"]) == 1


@pytest.mark.asyncio
async def test_parse_free_text_ai_failure():
    """AI failure returns None."""
    with patch(
        "app.services.free_text_parser.claude_structured",
        new_callable=AsyncMock,
        return_value=None,
    ):
        from app.services.free_text_parser import parse_free_text

        result = await parse_free_text("Some random text")

    assert result is None


@pytest.mark.asyncio
async def test_normalize_line_items():
    """Line item normalization applies condition/packaging cleanup."""
    from app.services.free_text_parser import _normalize_line_items

    result = {
        "line_items": [
            {"mpn": "  LM358N  ", "condition": "New", "packaging": "Tape & Reel", "quantity": 0},
            {"mpn": "NE555P", "currency": "EUR"},
        ]
    }
    _normalize_line_items(result)

    assert result["line_items"][0]["mpn"] == "LM358N"
    assert result["line_items"][0]["quantity"] == 1
    assert result["line_items"][1]["currency"] == "EUR"
