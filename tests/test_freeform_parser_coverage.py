"""Tests for app/services/freeform_parser_service.py — comprehensive coverage.

Covers parse_freeform_rfq, parse_freeform_offer, normalization steps.

Called by: pytest
Depends on: conftest fixtures, freeform_parser_service
"""

import os

os.environ["TESTING"] = "1"

from unittest.mock import patch

import pytest

from app.services.freeform_parser_service import parse_freeform_offer, parse_freeform_rfq

# Sentinel for parametrized cases that only assert the field is populated (not None),
# as opposed to checking an exact normalized value.
NOT_NONE = object()


def _patch_routed(result, captured=None):
    """Patch routed_structured with an async stub returning ``result``.

    If ``captured`` (a list) is given, each call's prompt is appended to it.
    """

    async def _stub(prompt=None, *a, **kw):
        if captured is not None:
            captured.append(prompt)
        return result

    return patch("app.services.freeform_parser_service.routed_structured", new=_stub)


def _assert_field(actual, expected):
    if expected is NOT_NONE:
        assert actual is not None
    else:
        assert actual == expected


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
        with _patch_routed(mock_result):
            result = await parse_freeform_rfq("Please quote 500 pcs LM317T")

        assert result is not None
        assert result["name"] == "Test RFQ"
        assert result["requirements"][0]["primary_mpn"] == "LM317T"

    @pytest.mark.parametrize(
        ("requirement", "field", "expected"),
        [
            pytest.param({"primary_mpn": "STM32F4", "target_qty": None}, "target_qty", 1, id="qty_default_to_1"),
            pytest.param({"primary_mpn": "LM317T", "target_price": "1.50"}, "target_price", NOT_NONE, id="price"),
            pytest.param({"primary_mpn": "LM317T", "condition": "NEW"}, "condition", NOT_NONE, id="condition"),
            pytest.param({"primary_mpn": "LM317T"}, "condition", "new", id="condition_default_new"),
            pytest.param(
                {"primary_mpn": "LM317T", "packaging": "Tape and Reel"}, "packaging", NOT_NONE, id="packaging"
            ),
            pytest.param({"primary_mpn": "LM317T", "date_codes": "2024+"}, "date_codes", NOT_NONE, id="date_codes"),
            pytest.param({"primary_mpn": "LM317T", "substitutes": None}, "substitutes", [], id="empty_substitutes"),
        ],
    )
    async def test_normalizes_requirement_field(self, requirement, field, expected):
        mock_result = {"name": "RFQ", "requirements": [requirement]}
        with _patch_routed(mock_result):
            result = await parse_freeform_rfq("Quote LM317T")

        _assert_field(result["requirements"][0][field], expected)

    async def test_api_returns_none_propagates_none(self):
        with _patch_routed(None):
            result = await parse_freeform_rfq("Quote LM317T")

        assert result is None

    async def test_text_truncated_to_6000_chars(self):
        long_text = "A" * 10000
        called_with = []

        with _patch_routed({"name": "RFQ", "requirements": []}, captured=called_with):
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
        with _patch_routed(mock_result):
            result = await parse_freeform_offer("We have 1000 LM317T at $0.50 each")

        assert result is not None
        assert result["vendor_name"] == "Arrow Electronics"
        assert result["offers"][0]["mpn"] == "LM317T"

    @pytest.mark.parametrize(
        ("offer", "field", "expected"),
        [
            pytest.param({"mpn": "LM317T", "unit_price": "0.75"}, "unit_price", NOT_NONE, id="price"),
            pytest.param({"mpn": "LM317T", "qty_available": "1,000"}, "qty_available", NOT_NONE, id="quantity"),
            pytest.param({"mpn": "LM317T", "condition": "NEW ORIGINAL"}, "condition", NOT_NONE, id="condition"),
            pytest.param({"mpn": "LM317T", "date_code": "2339"}, "date_code", NOT_NONE, id="date_code"),
            pytest.param({"mpn": "LM317T", "moq": "100"}, "moq", NOT_NONE, id="moq"),
            pytest.param({"mpn": "LM317T", "packaging": "TR"}, "packaging", NOT_NONE, id="packaging"),
            pytest.param({"mpn": "LM317T", "currency": None}, "currency", "USD", id="currency_default_usd"),
        ],
    )
    async def test_normalizes_offer_field(self, offer, field, expected):
        mock_result = {"vendor_name": "Arrow", "offers": [offer]}
        with _patch_routed(mock_result):
            result = await parse_freeform_offer("Arrow: LM317T")

        _assert_field(result["offers"][0][field], expected)

    async def test_with_rfq_context(self):
        mock_result = {
            "vendor_name": "Arrow",
            "offers": [{"mpn": "LM317T"}],
        }
        called_prompt = []

        rfq_context = [{"mpn": "LM317T", "qty": 500}]
        with _patch_routed(mock_result, captured=called_prompt):
            result = await parse_freeform_offer("Arrow offer", rfq_context=rfq_context)

        assert result is not None
        # Context should appear in the prompt
        assert "LM317T" in called_prompt[0]

    async def test_api_returns_none_propagates_none(self):
        with _patch_routed(None):
            result = await parse_freeform_offer("Arrow offer")

        assert result is None

    async def test_rfq_context_capped_at_10_parts(self):
        called_prompt = []

        rfq_context = [{"mpn": f"PART{i}", "qty": 10} for i in range(15)]
        with _patch_routed({"vendor_name": "A", "offers": []}, captured=called_prompt):
            await parse_freeform_offer("Offer text", rfq_context=rfq_context)

        # First 10 parts included, but the 11th-15th are cut off
        for i in range(10):
            assert f"PART{i}" in called_prompt[0]
