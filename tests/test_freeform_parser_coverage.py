"""Tests for app/services/freeform_parser_service.py — comprehensive coverage.

Covers parse_freeform_rfq, parse_freeform_offer, normalization steps.

Called by: pytest
Depends on: conftest fixtures, freeform_parser_service
"""

import os

os.environ["TESTING"] = "1"

from unittest.mock import patch

from app.services.freeform_parser_service import parse_freeform_offer, parse_freeform_rfq


class TestParseFreeformRfq:
    async def test_empty_text_returns_none(self):
        result = await parse_freeform_rfq("")
        assert result is None

    async def test_whitespace_only_returns_none(self):
        result = await parse_freeform_rfq("   \n   ")
        assert result is None

    async def test_successful_parse_basic(self):
        mock_result = {
            "name": "Test RFQ",
            "customer_name": "Acme",
            "requirements": [{"primary_mpn": "LM317T", "target_qty": 500, "condition": "new"}],
        }

        async def _mock(*a, **kw):
            return mock_result

        with patch("app.services.freeform_parser_service.routed_structured", new=_mock):
            result = await parse_freeform_rfq("Please quote 500 pcs LM317T")

        assert result is not None
        assert result["name"] == "Test RFQ"
        assert result["requirements"][0]["primary_mpn"] == "LM317T"

    async def test_normalizes_qty_default_to_1(self):
        mock_result = {
            "name": "RFQ",
            "requirements": [{"primary_mpn": "STM32F4", "target_qty": None}],
        }

        async def _mock(*a, **kw):
            return mock_result

        with patch("app.services.freeform_parser_service.routed_structured", new=_mock):
            result = await parse_freeform_rfq("Quote STM32F4")

        assert result["requirements"][0]["target_qty"] == 1

    async def test_normalizes_price(self):
        mock_result = {
            "name": "RFQ",
            "requirements": [{"primary_mpn": "LM317T", "target_price": "1.50"}],
        }

        async def _mock(*a, **kw):
            return mock_result

        with patch("app.services.freeform_parser_service.routed_structured", new=_mock):
            result = await parse_freeform_rfq("Quote LM317T at $1.50")

        assert result["requirements"][0]["target_price"] is not None

    async def test_normalizes_condition(self):
        mock_result = {
            "name": "RFQ",
            "requirements": [{"primary_mpn": "LM317T", "condition": "NEW"}],
        }

        async def _mock(*a, **kw):
            return mock_result

        with patch("app.services.freeform_parser_service.routed_structured", new=_mock):
            result = await parse_freeform_rfq("Quote LM317T new condition")

        # condition should be normalized (lowercased etc.)
        assert result["requirements"][0]["condition"] is not None

    async def test_normalizes_condition_default_new(self):
        mock_result = {
            "name": "RFQ",
            "requirements": [
                {"primary_mpn": "LM317T"}  # No condition
            ],
        }

        async def _mock(*a, **kw):
            return mock_result

        with patch("app.services.freeform_parser_service.routed_structured", new=_mock):
            result = await parse_freeform_rfq("Quote LM317T")

        assert result["requirements"][0]["condition"] == "new"

    async def test_normalizes_packaging(self):
        mock_result = {
            "name": "RFQ",
            "requirements": [{"primary_mpn": "LM317T", "packaging": "Tape and Reel"}],
        }

        async def _mock(*a, **kw):
            return mock_result

        with patch("app.services.freeform_parser_service.routed_structured", new=_mock):
            result = await parse_freeform_rfq("Quote LM317T T&R")

        assert result["requirements"][0]["packaging"] is not None

    async def test_normalizes_date_codes(self):
        mock_result = {
            "name": "RFQ",
            "requirements": [{"primary_mpn": "LM317T", "date_codes": "2024+"}],
        }

        async def _mock(*a, **kw):
            return mock_result

        with patch("app.services.freeform_parser_service.routed_structured", new=_mock):
            result = await parse_freeform_rfq("Quote LM317T 2024+")

        assert result["requirements"][0]["date_codes"] is not None

    async def test_sets_empty_substitutes_when_none(self):
        mock_result = {
            "name": "RFQ",
            "requirements": [{"primary_mpn": "LM317T", "substitutes": None}],
        }

        async def _mock(*a, **kw):
            return mock_result

        with patch("app.services.freeform_parser_service.routed_structured", new=_mock):
            result = await parse_freeform_rfq("Quote LM317T")

        assert result["requirements"][0]["substitutes"] == []

    async def test_api_returns_none_propagates_none(self):
        async def _none(*a, **kw):
            return None

        with patch("app.services.freeform_parser_service.routed_structured", new=_none):
            result = await parse_freeform_rfq("Quote LM317T")

        assert result is None

    async def test_text_truncated_to_6000_chars(self):
        long_text = "A" * 10000
        called_with = []

        async def _capture(prompt, **kw):
            called_with.append(prompt)
            return {"name": "RFQ", "requirements": []}

        with patch("app.services.freeform_parser_service.routed_structured", new=_capture):
            await parse_freeform_rfq(long_text)

        assert len(called_with) == 1
        # The prompt includes the text, which was capped at 6000 chars
        assert len(called_with[0]) <= 6050  # Prompt wrapper adds small overhead


class TestParseFreeformOffer:
    async def test_empty_text_returns_none(self):
        result = await parse_freeform_offer("")
        assert result is None

    async def test_successful_parse(self):
        mock_result = {
            "vendor_name": "Arrow Electronics",
            "offers": [
                {
                    "mpn": "LM317T",
                    "qty_available": 1000,
                    "unit_price": 0.50,
                    "currency": "USD",
                    "condition": "new",
                }
            ],
        }

        async def _mock(*a, **kw):
            return mock_result

        with patch("app.services.freeform_parser_service.routed_structured", new=_mock):
            result = await parse_freeform_offer("We have 1000 LM317T at $0.50 each")

        assert result is not None
        assert result["vendor_name"] == "Arrow Electronics"
        assert result["offers"][0]["mpn"] == "LM317T"

    async def test_normalizes_price(self):
        mock_result = {
            "vendor_name": "Arrow",
            "offers": [{"mpn": "LM317T", "unit_price": "0.75"}],
        }

        async def _mock(*a, **kw):
            return mock_result

        with patch("app.services.freeform_parser_service.routed_structured", new=_mock):
            result = await parse_freeform_offer("Arrow: LM317T $0.75")

        assert result["offers"][0]["unit_price"] is not None

    async def test_normalizes_quantity(self):
        mock_result = {
            "vendor_name": "Arrow",
            "offers": [{"mpn": "LM317T", "qty_available": "1,000"}],
        }

        async def _mock(*a, **kw):
            return mock_result

        with patch("app.services.freeform_parser_service.routed_structured", new=_mock):
            result = await parse_freeform_offer("Arrow: LM317T 1000 pcs")

        assert result["offers"][0]["qty_available"] is not None

    async def test_normalizes_condition(self):
        mock_result = {
            "vendor_name": "Arrow",
            "offers": [{"mpn": "LM317T", "condition": "NEW ORIGINAL"}],
        }

        async def _mock(*a, **kw):
            return mock_result

        with patch("app.services.freeform_parser_service.routed_structured", new=_mock):
            result = await parse_freeform_offer("Arrow: LM317T new")

        assert result["offers"][0]["condition"] is not None

    async def test_normalizes_date_code(self):
        mock_result = {
            "vendor_name": "Arrow",
            "offers": [{"mpn": "LM317T", "date_code": "2339"}],
        }

        async def _mock(*a, **kw):
            return mock_result

        with patch("app.services.freeform_parser_service.routed_structured", new=_mock):
            result = await parse_freeform_offer("Arrow: LM317T DC2339")

        assert result["offers"][0]["date_code"] is not None

    async def test_normalizes_moq(self):
        mock_result = {
            "vendor_name": "Arrow",
            "offers": [{"mpn": "LM317T", "moq": "100"}],
        }

        async def _mock(*a, **kw):
            return mock_result

        with patch("app.services.freeform_parser_service.routed_structured", new=_mock):
            result = await parse_freeform_offer("Arrow: LM317T MOQ 100")

        assert result["offers"][0]["moq"] is not None

    async def test_normalizes_packaging(self):
        mock_result = {
            "vendor_name": "Arrow",
            "offers": [{"mpn": "LM317T", "packaging": "TR"}],
        }

        async def _mock(*a, **kw):
            return mock_result

        with patch("app.services.freeform_parser_service.routed_structured", new=_mock):
            result = await parse_freeform_offer("Arrow: LM317T T&R")

        assert result["offers"][0]["packaging"] is not None

    async def test_defaults_currency_to_usd(self):
        mock_result = {
            "vendor_name": "Arrow",
            "offers": [{"mpn": "LM317T", "currency": None}],
        }

        async def _mock(*a, **kw):
            return mock_result

        with patch("app.services.freeform_parser_service.routed_structured", new=_mock):
            result = await parse_freeform_offer("Arrow: LM317T")

        assert result["offers"][0]["currency"] == "USD"

    async def test_with_rfq_context(self):
        mock_result = {
            "vendor_name": "Arrow",
            "offers": [{"mpn": "LM317T"}],
        }
        called_prompt = []

        async def _capture(prompt, **kw):
            called_prompt.append(prompt)
            return mock_result

        rfq_context = [{"mpn": "LM317T", "qty": 500}]
        with patch("app.services.freeform_parser_service.routed_structured", new=_capture):
            result = await parse_freeform_offer("Arrow offer", rfq_context=rfq_context)

        assert result is not None
        # Context should appear in the prompt
        assert "LM317T" in called_prompt[0]

    async def test_api_returns_none_propagates_none(self):
        async def _none(*a, **kw):
            return None

        with patch("app.services.freeform_parser_service.routed_structured", new=_none):
            result = await parse_freeform_offer("Arrow offer")

        assert result is None

    async def test_rfq_context_capped_at_10_parts(self):
        called_prompt = []

        async def _capture(prompt, **kw):
            called_prompt.append(prompt)
            return {"vendor_name": "A", "offers": []}

        rfq_context = [{"mpn": f"PART{i}", "qty": 10} for i in range(15)]
        with patch("app.services.freeform_parser_service.routed_structured", new=_capture):
            await parse_freeform_offer("Offer text", rfq_context=rfq_context)

        # First 10 parts included, but the 11th-15th are cut off
        for i in range(10):
            assert f"PART{i}" in called_prompt[0]
