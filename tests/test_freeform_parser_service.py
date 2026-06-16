"""Tests for app/services/freeform_parser_service.py.

Covers: parse_freeform_rfq, parse_freeform_offer (empty input, normalization,
mock AI response, None from AI).

Called by: pytest
Depends on: conftest.py
"""

import os

os.environ["TESTING"] = "1"

from unittest.mock import AsyncMock, patch

import pytest

from app.services.freeform_parser_service import parse_freeform_offer, parse_freeform_rfq


def _patch_ai(return_value):
    """Patch routed_structured at its source module with the given return value."""
    return patch(
        "app.services.freeform_parser_service.routed_structured",
        new_callable=AsyncMock,
        return_value=return_value,
    )


class TestParseFreeformRFQ:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("text", ["", "   ", None], ids=["empty", "whitespace", "none"])
    async def test_blank_input_returns_none(self, text):
        result = await parse_freeform_rfq(text)
        assert result is None

    @pytest.mark.asyncio
    async def test_ai_returns_none_propagates(self):
        with _patch_ai(None):
            result = await parse_freeform_rfq("Need 100x LM358")
            assert result is None

    @pytest.mark.asyncio
    async def test_normalizes_requirements(self):
        raw_ai_result = {
            "name": "Test RFQ",
            "requirements": [
                {
                    "primary_mpn": "LM358",
                    "target_qty": None,
                    "target_price": 1.5,
                    "condition": "NEW",
                    "packaging": "tape and reel",
                    "date_codes": "2024+",
                    "substitutes": None,
                }
            ],
        }
        with _patch_ai(raw_ai_result):
            result = await parse_freeform_rfq("Need 100x LM358")
        assert result is not None
        req = result["requirements"][0]
        # target_qty defaults to 1
        assert req["target_qty"] == 1
        # substitutes defaults to []
        assert req["substitutes"] == []
        # condition normalized to lowercase default
        assert req["condition"] == "new"

    @pytest.mark.asyncio
    async def test_no_condition_defaults_to_new(self):
        raw_ai_result = {
            "name": "Test",
            "requirements": [{"primary_mpn": "LM741", "target_qty": 50}],
        }
        with _patch_ai(raw_ai_result):
            result = await parse_freeform_rfq("Need LM741")
        assert result["requirements"][0]["condition"] == "new"

    @pytest.mark.asyncio
    async def test_passes_prompt_to_ai(self):
        with _patch_ai(None) as mock_call:
            await parse_freeform_rfq("Need 50x BC547")
            mock_call.assert_called_once()
            call_kwargs = mock_call.call_args
            assert "BC547" in call_kwargs.kwargs.get("prompt", "") or "BC547" in str(call_kwargs)

    @pytest.mark.asyncio
    async def test_text_truncated_at_6000(self):
        long_text = "A" * 7000
        with _patch_ai(None) as mock_call:
            await parse_freeform_rfq(long_text)
            call_kwargs = mock_call.call_args
            prompt = call_kwargs.kwargs.get("prompt", "")
            assert len(prompt) <= 6100  # truncated to 6000 + header text


class TestParseFreeformOffer:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("text", ["", None], ids=["empty", "none"])
    async def test_blank_input_returns_none(self, text):
        result = await parse_freeform_offer(text)
        assert result is None

    @pytest.mark.asyncio
    async def test_ai_returns_none_propagates(self):
        with _patch_ai(None):
            result = await parse_freeform_offer("We have 100x LM358 @ $0.50")
            assert result is None

    @pytest.mark.asyncio
    async def test_normalizes_offers(self):
        raw_ai_result = {
            "vendor_name": "Acme Parts",
            "offers": [
                {
                    "mpn": "LM358",
                    "unit_price": 0.50,
                    "qty_available": 100,
                    "condition": "NEW",
                    "date_code": "2339",
                    "moq": 10,
                    "packaging": "tape and reel",
                    "currency": "USD",
                }
            ],
        }
        with _patch_ai(raw_ai_result):
            result = await parse_freeform_offer("We have 100x LM358 @ $0.50")
        assert result is not None
        offer = result["offers"][0]
        assert offer["unit_price"] is not None
        assert offer["qty_available"] is not None
        assert offer["currency"] == "USD"

    @pytest.mark.asyncio
    async def test_rfq_context_included_in_prompt(self):
        rfq_context = [{"mpn": "LM358", "qty": 100}]
        with _patch_ai(None) as mock_call:
            await parse_freeform_offer("We have stock", rfq_context=rfq_context)
            prompt = mock_call.call_args.kwargs.get("prompt", "")
            assert "LM358" in prompt

    @pytest.mark.asyncio
    async def test_no_rfq_context(self):
        with _patch_ai(None) as mock_call:
            await parse_freeform_offer("We have 50x BC547")
            mock_call.assert_called_once()

    @pytest.mark.asyncio
    async def test_currency_defaults_to_usd(self):
        raw_ai_result = {
            "vendor_name": "Vendor",
            "offers": [{"mpn": "BC547", "currency": None}],
        }
        with _patch_ai(raw_ai_result):
            result = await parse_freeform_offer("We have BC547")
        assert result["offers"][0]["currency"] == "USD"
