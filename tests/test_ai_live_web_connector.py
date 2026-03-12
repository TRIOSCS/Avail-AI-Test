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
                    "in_stock_explicit": True,
                    "listing_age_days": 1,
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
        assert row["raw_data"]["in_stock_explicit"] is True
        assert row["raw_data"]["listing_age_days"] == 1

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
                    "qty_available": 50,
                    "unit_price": "not-a-price",
                    "condition": "factory sealed",
                    "currency": "USDX",
                    "vendor_url": "https://partsnow.example.com/lm317t",
                    "in_stock_explicit": True,
                    "evidence_note": "Qty 50 in stock",
                },
            ]
        }

        with patch("app.connectors.ai_live_web.claude_json", new=AsyncMock(return_value=payload)):
            out = await connector._do_search("LM317T")

        assert len(out) == 1
        row = out[0]
        # Falls back to queried part number when mpn is blank
        assert row["mpn_matched"] == "LM317T"
        assert row["qty_available"] == 50
        assert row["unit_price"] is None
        assert row["condition"] is None
        # Currency is uppercased and clipped to first 3 chars
        assert row["currency"] == "USD"

    @pytest.mark.asyncio
    async def test_quality_gate_rejects_no_stock_signal_or_url(self):
        connector = AIWebSearchConnector(api_key="test-key")
        payload = {
            "offers": [
                {
                    "vendor_name": "NoSignal Vendor",
                    "mpn": "LM324",
                    "qty_available": 300,
                    "vendor_url": "",
                    "evidence_note": "Please contact sales for details",
                },
                {
                    "vendor_name": "NoSignal2 Vendor",
                    "mpn": "LM324",
                    "qty_available": 100,
                    "vendor_url": "https://example.com/lm324",
                    "evidence_note": "Call us for quote",
                    "in_stock_explicit": False,
                },
            ]
        }
        with patch("app.connectors.ai_live_web.claude_json", new=AsyncMock(return_value=payload)):
            out = await connector._do_search("LM324")
        assert out == []

    @pytest.mark.asyncio
    async def test_quality_gate_rejects_stale_listing(self):
        connector = AIWebSearchConnector(api_key="test-key")
        payload = {
            "offers": [
                {
                    "vendor_name": "Old Listing Vendor",
                    "mpn": "NE555",
                    "qty_available": 42,
                    "vendor_url": "https://example.com/ne555",
                    "in_stock_explicit": True,
                    "listing_age_days": 120,
                    "evidence_note": "In stock qty 42",
                }
            ]
        }
        with patch("app.connectors.ai_live_web.claude_json", new=AsyncMock(return_value=payload)):
            out = await connector._do_search("NE555")
        assert out == []
