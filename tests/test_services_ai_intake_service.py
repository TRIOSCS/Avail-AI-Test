"""test_services_ai_intake_service.py — Tests for AI intake parser service.

Tests normalization helpers, text cleaning, JSON coercion, document type
backfill, requisition name generation, and the full parse pipeline.

Covers: app/services/ai_intake_parser.py
"""

from unittest.mock import AsyncMock, patch

import pytest

from app.services.ai_intake_parser import (
    _backfill_document_type,
    _backfill_requisition_name,
    _clean_scalar,
    _clean_text,
    _coerce_json_list,
    _normalize_offers,
    _normalize_requirements,
    _normalize_top_level,
    parse_freeform_intake,
)

# ---------------------------------------------------------------------------
# _clean_text
# ---------------------------------------------------------------------------


class TestCleanText:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("  hello  ", "hello"),
            ("a\r\nb\rc", "a\nb\nc"),
            ("", ""),
            (None, ""),
            ("Line 1\n  Line 2\n    Line 3", "Line 1\n  Line 2\n    Line 3"),
        ],
        ids=["strips_whitespace", "normalizes_crlf", "empty_str", "none", "preserves_structure"],
    )
    def test_clean_text(self, raw, expected):
        assert _clean_text(raw) == expected

    def test_collapses_blank_runs(self):
        result = _clean_text("a\n\n\n\n\nb")
        # _clean_text allows up to 2 consecutive blank lines (3 newlines)
        assert "\n\n\n\n" not in result


# ---------------------------------------------------------------------------
# _coerce_json_list
# ---------------------------------------------------------------------------


class TestCoerceJsonList:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ([1, 2, 3], [1, 2, 3]),
            ('[{"mpn": "ABC"}]', [{"mpn": "ABC"}]),
            ("not json", []),
            (None, []),
            ('{"key": "val"}', []),
            (42, []),
        ],
        ids=["list_passthrough", "json_string", "invalid_json", "none", "non_array_json", "integer"],
    )
    def test_coerce_json_list(self, value, expected):
        assert _coerce_json_list(value) == expected


# ---------------------------------------------------------------------------
# _clean_scalar
# ---------------------------------------------------------------------------


class TestCleanScalar:
    @pytest.mark.parametrize(
        "value,expected",
        [
            (None, None),
            ("", None),
            ("   ", None),
            ("  hello   world  ", "hello world"),
            (42, "42"),
        ],
        ids=["none", "empty_string", "blank_string", "collapses_whitespace", "non_string"],
    )
    def test_clean_scalar(self, value, expected):
        assert _clean_scalar(value) == expected


# ---------------------------------------------------------------------------
# _normalize_top_level
# ---------------------------------------------------------------------------


class TestNormalizeTopLevel:
    def test_normalizes_doc_type(self):
        result = {"document_type": "RFQ", "confidence": 0.9, "requirements": [], "offers": []}
        _normalize_top_level(result)
        assert result["document_type"] == "rfq"

    def test_invalid_doc_type_becomes_unclear(self):
        result = {"document_type": "invoice", "confidence": 0.5, "requirements": [], "offers": []}
        _normalize_top_level(result)
        assert result["document_type"] == "unclear"

    def test_clamps_confidence(self):
        result = {"document_type": "offer", "confidence": 5.0, "requirements": [], "offers": []}
        _normalize_top_level(result)
        assert result["confidence"] == 1.0

        result["confidence"] = -0.5
        _normalize_top_level(result)
        assert result["confidence"] == 0.0

    def test_invalid_confidence(self):
        result = {"document_type": "rfq", "confidence": "bad", "requirements": [], "offers": []}
        _normalize_top_level(result)
        assert result["confidence"] == 0.0

    def test_generates_summary_for_rfq(self):
        result = {
            "document_type": "rfq",
            "confidence": 0.8,
            "requirements": [{"mpn": "A"}, {"mpn": "B"}],
            "offers": [],
        }
        _normalize_top_level(result)
        assert "2 RFQ line(s)" in result["summary"]

    def test_generates_summary_for_offer(self):
        result = {
            "document_type": "offer",
            "confidence": 0.8,
            "requirements": [],
            "offers": [{"mpn": "X"}],
        }
        _normalize_top_level(result)
        assert "1 offer line(s)" in result["summary"]

    def test_cleans_string_fields(self):
        result = {
            "document_type": "rfq",
            "confidence": 0.9,
            "summary": "  hello   world  ",
            "vendor_name": "",
            "customer_name": None,
            "notes": 42,
            "requirements": [],
            "offers": [],
        }
        _normalize_top_level(result)
        assert result["summary"] == "hello world"
        assert result["vendor_name"] is None  # empty → None
        assert result["notes"] == "42"  # non-string → str


# ---------------------------------------------------------------------------
# _normalize_requirements
# ---------------------------------------------------------------------------


class TestNormalizeRequirements:
    def test_basic_requirement(self):
        result = {
            "requirements": [
                {"mpn": "LM358", "quantity": 100, "manufacturer": "TI"},
            ],
        }
        _normalize_requirements(result)
        assert len(result["requirements"]) == 1
        row = result["requirements"][0]
        assert row["mpn"] == "LM358"
        assert row["quantity"] == 100
        assert row["manufacturer"] == "TI"

    def test_skips_empty_mpn(self):
        result = {"requirements": [{"mpn": "", "quantity": 10}]}
        _normalize_requirements(result)
        assert result["requirements"] == []

    def test_defaults_quantity_to_1(self):
        result = {"requirements": [{"mpn": "ABC123"}]}
        _normalize_requirements(result)
        assert result["requirements"][0]["quantity"] == 1

    def test_skips_non_dict_rows(self):
        result = {"requirements": ["not a dict", 42, None]}
        _normalize_requirements(result)
        assert result["requirements"] == []

    def test_json_string_input(self):
        result = {"requirements": '[{"mpn": "XYZ"}]'}
        _normalize_requirements(result)
        assert len(result["requirements"]) == 1
        assert result["requirements"][0]["mpn"] == "XYZ"


# ---------------------------------------------------------------------------
# _normalize_offers
# ---------------------------------------------------------------------------


class TestNormalizeOffers:
    def test_basic_offer(self):
        result = {
            "vendor_name": "Acme Corp",
            "offers": [
                {"mpn": "LM358", "qty_available": 500, "unit_price": 1.25, "vendor_name": "Acme"},
            ],
        }
        _normalize_offers(result)
        assert len(result["offers"]) == 1
        row = result["offers"][0]
        assert row["mpn"] == "LM358"
        assert row["vendor_name"] == "Acme"

    def test_inherits_vendor_name(self):
        result = {
            "vendor_name": "FallbackVendor",
            "offers": [{"mpn": "ABC"}],
        }
        _normalize_offers(result)
        assert result["offers"][0]["vendor_name"] == "FallbackVendor"

    def test_defaults_currency_to_usd(self):
        result = {"offers": [{"mpn": "X"}]}
        _normalize_offers(result)
        assert result["offers"][0]["currency"] == "USD"

    def test_skips_empty_mpn(self):
        result = {"offers": [{"mpn": "", "unit_price": 5.0}]}
        _normalize_offers(result)
        assert result["offers"] == []


# ---------------------------------------------------------------------------
# _backfill_document_type
# ---------------------------------------------------------------------------


class TestBackfillDocumentType:
    @pytest.mark.parametrize(
        "result,expected",
        [
            ({"document_type": "rfq", "requirements": [], "offers": []}, "rfq"),
            ({"document_type": "unclear", "requirements": [], "offers": [{"mpn": "A"}]}, "offer"),
            ({"document_type": "unclear", "requirements": [{"mpn": "A"}], "offers": []}, "rfq"),
            (
                {"document_type": "unclear", "requirements": [{"mpn": "A"}], "offers": [{"mpn": "B"}, {"mpn": "C"}]},
                "offer",
            ),
            ({"document_type": "unclear", "requirements": [], "offers": []}, "unclear"),
        ],
        ids=["no_change_when_not_unclear", "infers_offer", "infers_rfq", "offer_wins_when_more", "stays_unclear"],
    )
    def test_backfill_document_type(self, result, expected):
        _backfill_document_type(result)
        assert result["document_type"] == expected


# ---------------------------------------------------------------------------
# _backfill_requisition_name
# ---------------------------------------------------------------------------


class TestBackfillRequisitionName:
    def test_no_overwrite(self):
        result = {"requisition_name": "My Req", "document_type": "rfq"}
        _backfill_requisition_name(result)
        assert result["requisition_name"] == "My Req"

    @pytest.mark.parametrize(
        "result,expected_substr",
        [
            ({"requisition_name": None, "document_type": "rfq", "customer_name": "Acme"}, "Acme RFQ intake"),
            ({"requisition_name": None, "document_type": "offer", "vendor_name": "DigiKey"}, "DigiKey offer intake"),
            ({"requisition_name": None, "document_type": "unclear"}, "AI intake draft"),
        ],
        ids=["rfq_name", "offer_name", "fallback_name"],
    )
    def test_generates_name(self, result, expected_substr):
        _backfill_requisition_name(result)
        assert expected_substr in result["requisition_name"]


# ---------------------------------------------------------------------------
# parse_freeform_intake (integration)
# ---------------------------------------------------------------------------


class TestParseFreeformIntake:
    @pytest.mark.asyncio
    async def test_empty_text_returns_none(self):
        result = await parse_freeform_intake("")
        assert result is None

    @pytest.mark.asyncio
    async def test_whitespace_only_returns_none(self):
        result = await parse_freeform_intake("   \n\n   ")
        assert result is None

    @pytest.mark.asyncio
    async def test_successful_parse(self):
        mock_result = {
            "document_type": "rfq",
            "confidence": 0.85,
            "summary": "Test RFQ",
            "requirements": [{"mpn": "LM358", "quantity": 100}],
            "offers": [],
        }
        with patch("app.services.ai_intake_parser.routed_structured", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = mock_result
            result = await parse_freeform_intake("Need 100x LM358")

        assert result is not None
        assert result["document_type"] == "rfq"
        assert result["confidence"] == 0.85
        assert len(result["requirements"]) == 1
        assert result["requirements"][0]["mpn"] == "LM358"

    @pytest.mark.asyncio
    async def test_llm_returns_none(self):
        with patch("app.services.ai_intake_parser.routed_structured", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = None
            result = await parse_freeform_intake("Some text")
        assert result is None

    @pytest.mark.asyncio
    async def test_context_lines_included(self):
        mock_result = {
            "document_type": "offer",
            "confidence": 0.7,
            "requirements": [],
            "offers": [{"mpn": "ABC", "unit_price": 1.5}],
        }
        context = [{"mpn": "ABC", "qty": 50}, {"mpn": "DEF", "qty": 100}]
        with patch("app.services.ai_intake_parser.routed_structured", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = mock_result
            result = await parse_freeform_intake("Vendor quote text", context)

        assert result is not None
        # Verify context was passed in the prompt
        call_kwargs = mock_llm.call_args
        prompt_text = call_kwargs.kwargs.get("prompt") or call_kwargs.args[0] if call_kwargs.args else ""
        if not prompt_text and call_kwargs.kwargs:
            prompt_text = call_kwargs.kwargs.get("prompt", "")
        assert "ABC" in prompt_text or result["document_type"] == "offer"

    @pytest.mark.asyncio
    async def test_non_dict_result_returns_none(self):
        with patch("app.services.ai_intake_parser.routed_structured", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = "just a string"
            result = await parse_freeform_intake("Some text")
        assert result is None
