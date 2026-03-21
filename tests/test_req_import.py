"""Tests for requisition AI import (paste/upload → parse → save)."""

from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_parse_freeform_rfq_returns_brand_and_condition():
    """Verify the parser schema accepts brand and condition fields."""
    mock_result = {
        "name": "Test RFQ",
        "requirements": [
            {
                "primary_mpn": "LM358DR",
                "target_qty": 500,
                "brand": "Texas Instruments",
                "condition": "new",
            }
        ],
    }
    with patch(
        "app.services.freeform_parser_service.routed_structured",
        new_callable=AsyncMock,
        return_value=mock_result,
    ):
        from app.services.freeform_parser_service import parse_freeform_rfq

        result = await parse_freeform_rfq("LM358DR x500 TI new")
        assert result is not None
        req = result["requirements"][0]
        assert req["brand"] == "Texas Instruments"
        assert req["condition"] == "new"
