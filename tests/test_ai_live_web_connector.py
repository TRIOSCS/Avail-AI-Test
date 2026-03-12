"""Tests for AI live web connector parsing and safety.

What it does:
- Verifies app.connectors.ai_live_web.AIWebSearchConnector output shaping.
- Ensures malformed AI output is handled safely without crashing.

What calls it:
- pytest suite in CI and local test runs.

What it depends on:
- app.connectors.ai_live_web
- unittest.mock AsyncMock/patch
"""

from unittest.mock import AsyncMock, patch

import pytest

from app.connectors.ai_live_web import AIWebSearchConnector


class TestAIWebSearchConnector:
    @pytest.mark.asyncio
    async def test_returns_empty_when_no_api_key(self):
        connector = AIWebSearchConnector(api_key="")
        out = await connector._do_search("LM358")
        assert out == []

    @pytest.mark.asyncio
    async def test_parses_valid_offer_rows(self):
        connector = AIWebSearchConnector(api_key="test-key")
        payload = {
            "offers": [
                {
                    "vendor_name": "Acme Electronics",
                    "mpn": "LM358DR",
                    "manufacturer": "Texas Instruments",
                    "qty_available": 1200,
                    "unit_price": 0.1845,
                    "currency": "usd",
                    "condition": "new",
                    "lead_time": "2-3 weeks",
                    "vendor_url": "https://example.com/lm358dr",
                    "vendor_email": "sales@example.com",
                    "vendor_phone": "+1 555 0100",
                    "evidence_note": "In stock listing",
                }
            ]
        }

        with patch("app.connectors.ai_live_web.claude_json", new=AsyncMock(return_value=payload)):
            out = await connector._do_search("LM358DR")

        assert len(out) == 1
        row = out[0]
        assert row["vendor_name"] == "Acme Electronics"
        assert row["mpn_matched"] == "LM358DR"
        assert row["source_type"] == "ai_live_web"
        assert row["currency"] == "USD"
        assert row["qty_available"] == 1200
        assert row["unit_price"] == 0.1845
        assert row["condition"] == "new"
        assert row["raw_data"]["evidence_note"] == "In stock listing"

    @pytest.mark.asyncio
    async def test_handles_non_dict_response(self):
        connector = AIWebSearchConnector(api_key="test-key")
        with patch("app.connectors.ai_live_web.claude_json", new=AsyncMock(return_value=["bad", "shape"])):
            out = await connector._do_search("LM317")
        assert out == []

    @pytest.mark.asyncio
    async def test_filters_missing_vendor_and_bad_numbers(self):
        connector = AIWebSearchConnector(api_key="test-key")
        payload = {
            "offers": [
                {"vendor_name": "", "mpn": "LM317T", "qty_available": 10},
                {
                    "vendor_name": "PartsNow",
                    "mpn": "",
                    "qty_available": "n/a",
                    "unit_price": "not-a-price",
                    "condition": "factory sealed",
                    "currency": "USDX",
                },
            ]
        }

        with patch("app.connectors.ai_live_web.claude_json", new=AsyncMock(return_value=payload)):
            out = await connector._do_search("LM317T")

        assert len(out) == 1
        row = out[0]
        # Falls back to queried part number when mpn is blank
        assert row["mpn_matched"] == "LM317T"
        assert row["qty_available"] is None
        assert row["unit_price"] is None
        assert row["condition"] is None
        # Currency is uppercased and clipped to first 3 chars
        assert row["currency"] == "USD"
