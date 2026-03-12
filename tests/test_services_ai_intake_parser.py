"""tests/test_services_ai_intake_parser.py — Coverage for AI intake parsing helpers.

Called by: pytest
Depends on: app/services/ai_intake_parser.py
"""

from unittest.mock import AsyncMock, patch

import pytest

from app.services.ai_intake_parser import parse_freeform_intake


@pytest.mark.asyncio
async def test_parse_freeform_intake_normalizes_rfq_requirements():
    mock_result = {
        "document_type": "rfq",
        "confidence": 0.91,
        "summary": "Parsed request",
        "customer_name": "Acme Medical",
        "requirements": [
            {
                "mpn": " lm317t ",
                "quantity": "1,000",
                "target_price": "$0.45",
                "condition": "Factory New",
                "packaging": "Tape & Reel",
            }
        ],
        "offers": [],
    }

    with patch("app.services.ai_intake_parser.routed_structured", AsyncMock(return_value=mock_result)):
        result = await parse_freeform_intake("Need LM317T qty 1,000 at $0.45")

    assert result is not None
    assert result["document_type"] == "rfq"
    assert result["requirements"][0]["mpn"] == "LM317T"
    assert result["requirements"][0]["quantity"] == 1000
    assert result["requirements"][0]["target_price"] == 0.45
    assert result["requirements"][0]["condition"] == "new"
    assert result["requirements"][0]["packaging"] == "reel"
    assert result["requisition_name"]


@pytest.mark.asyncio
async def test_parse_freeform_intake_normalizes_offer_rows():
    mock_result = {
        "document_type": "offer",
        "confidence": "0.84",
        "vendor_name": "Acme Vendor",
        "requirements": [],
        "offers": [
            {
                "mpn": " lm7805 ",
                "qty_available": "2,500",
                "unit_price": "$0.12",
                "currency": "$",
                "condition": "Factory New",
                "packaging": "Tape & Reel",
                "moq": "100",
            }
        ],
    }

    with patch("app.services.ai_intake_parser.routed_structured", AsyncMock(return_value=mock_result)):
        result = await parse_freeform_intake("We can offer LM7805 qty 2,500 at $0.12")

    assert result is not None
    assert result["document_type"] == "offer"
    assert result["confidence"] == 0.84
    assert result["offers"][0]["vendor_name"] == "Acme Vendor"
    assert result["offers"][0]["mpn"] == "LM7805"
    assert result["offers"][0]["qty_available"] == 2500
    assert result["offers"][0]["unit_price"] == 0.12
    assert result["offers"][0]["currency"] == "USD"
    assert result["offers"][0]["condition"] == "new"
    assert result["offers"][0]["packaging"] == "reel"
    assert result["offers"][0]["moq"] == 100


@pytest.mark.asyncio
async def test_parse_freeform_intake_returns_none_on_empty_llm_result():
    with patch("app.services.ai_intake_parser.routed_structured", AsyncMock(return_value=None)):
        result = await parse_freeform_intake("Need LM317T")

    assert result is None
