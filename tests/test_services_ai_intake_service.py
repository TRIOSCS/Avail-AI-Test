"""
test_services_ai_intake_service.py — Tests for free-form intake parsing service.

Validates AI result normalization and heuristic fallback behavior for the
free-form intake parser used by /api/ai/intake-parse.

Called by: pytest
Depends on: app.services.ai_intake_service
"""

from unittest.mock import AsyncMock, patch

import pytest

from app.services.ai_intake_service import parse_freeform_intake


@pytest.mark.asyncio
async def test_parse_freeform_intake_normalizes_ai_rows():
    """AI rows are normalized into clean typed fields."""
    ai_result = {
        "detected_type": "mixed",
        "context": {"vendor_name": "Acme Components"},
        "rows": [
            {
                "row_type": "requirement",
                "mpn": " lm317t ",
                "qty": "1,000",
                "unit_price": "0.44",
                "confidence": 0.91,
            },
            {
                "row_type": "offer",
                "mpn": "LM7805",
                "qty": "500",
                "unit_price": "$0.35",
                "vendor_name": "",
                "condition": "NIB",
                "confidence": "0.8",
            },
        ],
    }

    with patch("app.services.ai_intake_service.routed_structured", new_callable=AsyncMock, return_value=ai_result):
        result = await parse_freeform_intake("messy pasted text", mode="auto")

    assert result["detected_type"] == "mixed"
    assert result["summary"]["rows"] == 2
    assert result["rows"][0]["mpn"] == "LM317T"
    assert result["rows"][0]["qty"] == 1000
    assert result["rows"][0]["unit_price"] == 0.44
    assert result["rows"][1]["row_type"] == "offer"
    assert result["rows"][1]["vendor_name"] == "Acme Components"
    assert result["rows"][1]["unit_price"] == 0.35


@pytest.mark.asyncio
async def test_parse_freeform_intake_falls_back_to_heuristics():
    """When AI parse fails, TSV-like lines still produce rows."""
    with patch("app.services.ai_intake_service.routed_structured", new_callable=AsyncMock, return_value=None):
        result = await parse_freeform_intake("LM317T\t250\t0.42\nLM7805\t500\t0.31", mode="rfq")

    assert result["summary"]["rows"] == 2
    assert result["detected_type"] == "rfq"
    assert all(r["row_type"] == "requirement" for r in result["rows"])
    assert result["rows"][0]["mpn"] == "LM317T"
    assert result["rows"][0]["qty"] == 250
