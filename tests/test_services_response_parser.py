"""test_services_response_parser.py — Tests for vendor response parser.

Tests confidence thresholds, offer extraction, email cleaning,
cross-validation, and normalization pipeline.

Called by: pytest
Depends on: app/services/response_parser.py
"""

from unittest.mock import AsyncMock, patch

import pytest

from app.services.response_parser import (
    CONFIDENCE_AUTO,
    CONFIDENCE_REVIEW,
    _clean_email_body,
    _cross_validate,
    _normalize_parsed_parts,
    extract_draft_offers,
    should_auto_apply,
    should_flag_review,
)

# ── Confidence threshold tests ──────────────────────────────────────


class TestConfidenceThresholds:
    def test_auto_apply_high_confidence(self):
        assert should_auto_apply({"confidence": 0.9}) is True

    def test_auto_apply_exact_threshold(self):
        assert should_auto_apply({"confidence": CONFIDENCE_AUTO}) is True

    def test_auto_apply_below_threshold(self):
        assert should_auto_apply({"confidence": 0.79}) is False

    def test_flag_review_in_range(self):
        assert should_flag_review({"confidence": 0.6}) is True

    def test_flag_review_at_lower_bound(self):
        assert should_flag_review({"confidence": CONFIDENCE_REVIEW}) is True

    def test_flag_review_below_range(self):
        assert should_flag_review({"confidence": 0.4}) is False

    def test_flag_review_above_range(self):
        """≥0.8 should auto-apply, not flag for review."""
        assert should_flag_review({"confidence": 0.85}) is False

    def test_missing_confidence(self):
        assert should_auto_apply({}) is False
        assert should_flag_review({}) is False


# ── Draft offer extraction ──────────────────────────────────────────


class TestExtractDraftOffers:
    def test_extract_quoted_parts(self):
        result = {
            "parts": [
                {"mpn": "LM317T", "status": "quoted", "unit_price": 0.75, "qty_available": 5000},
                {"mpn": "LM7805", "status": "no_stock"},
                {"mpn": "LM317K", "status": "quoted", "unit_price": 1.25, "currency": "EUR"},
            ]
        }
        offers = extract_draft_offers(result, "Arrow Electronics")
        assert len(offers) == 2
        assert offers[0]["mpn"] == "LM317T"
        assert offers[0]["vendor_name"] == "Arrow Electronics"
        assert offers[0]["unit_price"] == 0.75
        assert offers[0]["source"] == "ai_parsed"
        assert offers[0]["status"] == "pending_review"
        assert offers[1]["currency"] == "EUR"

    def test_skip_no_stock(self):
        result = {"parts": [{"mpn": "XYZ", "status": "no_stock"}]}
        offers = extract_draft_offers(result, "Test Vendor")
        assert len(offers) == 0

    def test_skip_quoted_without_price(self):
        result = {"parts": [{"mpn": "ABC", "status": "quoted", "unit_price": None}]}
        offers = extract_draft_offers(result, "Test Vendor")
        assert len(offers) == 0

    def test_empty_parts(self):
        assert extract_draft_offers({"parts": []}, "V") == []
        assert extract_draft_offers({}, "V") == []

    def test_offer_fields_complete(self):
        result = {
            "parts": [
                {
                    "mpn": "LM317T",
                    "status": "quoted",
                    "unit_price": 0.75,
                    "qty_available": 5000,
                    "currency": "USD",
                    "lead_time": "2-3 weeks",
                    "date_code": "2525",
                    "condition_normalized": "new",
                    "packaging_normalized": "tube",
                    "moq": 100,
                    "valid_days": 30,
                    "notes": "Ships from Austin",
                }
            ]
        }
        offers = extract_draft_offers(result, "Arrow")
        o = offers[0]
        assert o["condition"] == "new"
        assert o["packaging"] == "tube"
        assert o["moq"] == 100
        assert o["valid_days"] == 30


# ── Email body cleaning ─────────────────────────────────────────────


class TestCleanEmailBody:
    def test_strip_html_tags(self):
        html = "<p>Hi,</p><br/><b>We have stock</b>"
        cleaned = _clean_email_body(html)
        assert "<" not in cleaned
        assert "We have stock" in cleaned

    def test_collapse_whitespace(self):
        text = "Hello     there\n\n\n   how   are  you"
        cleaned = _clean_email_body(text)
        assert "  " not in cleaned

    def test_empty_body(self):
        assert _clean_email_body("") == ""
        assert _clean_email_body(None) == ""

    def test_disclaimer_removal(self):
        text = "We can quote LM317T at $0.75.\n\nDISCLAIMER: This email and any attachments are confidential."
        cleaned = _clean_email_body(text)
        assert "We can quote" in cleaned


# ── Normalization pipeline ──────────────────────────────────────────


class TestNormalizeParsedParts:
    def test_normalizes_price(self):
        result = {"parts": [{"mpn": "X", "status": "quoted", "unit_price": "1.234"}]}
        _normalize_parsed_parts(result)
        # Price normalization should handle string → float
        assert isinstance(result["parts"][0]["unit_price"], (int, float, type(None)))

    def test_default_currency(self):
        result = {"parts": [{"mpn": "X", "status": "quoted"}]}
        _normalize_parsed_parts(result)
        assert result["parts"][0]["currency"] == "USD"

    def test_empty_parts_list(self):
        result = {"parts": []}
        _normalize_parsed_parts(result)  # should not raise


# ── Cross-validation ────────────────────────────────────────────────


class TestCrossValidate:
    def test_matching_mpn(self):
        result = {"parts": [{"mpn": "LM317T", "status": "quoted"}]}
        rfq = {"mpn": "LM317T", "qty": 1000}
        _cross_validate(result, rfq)
        assert result["parts"][0]["mpn_matches_rfq"] is True

    def test_non_matching_mpn(self):
        result = {"parts": [{"mpn": "TOTALLY-DIFFERENT", "status": "quoted"}]}
        rfq = {"mpn": "LM317T", "qty": 1000}
        _cross_validate(result, rfq)
        assert result["parts"][0]["mpn_matches_rfq"] is False

    def test_multi_rfq_context(self):
        result = {"parts": [{"mpn": "LM7805", "status": "quoted"}]}
        rfq_list = [{"mpn": "LM317T"}, {"mpn": "LM7805"}, {"mpn": "LM358"}]
        _cross_validate(result, rfq_list)
        assert result["parts"][0]["mpn_matches_rfq"] is True

    def test_invalid_context_type(self):
        result = {"parts": [{"mpn": "X"}]}
        _cross_validate(result, "not a dict or list")  # should not raise


# ── Full parse flow (mocked Claude) ────────────────────────────────


class TestParseVendorResponse:
    @pytest.mark.asyncio
    async def test_parse_with_mock_claude(self):
        from app.services.response_parser import parse_vendor_response

        mock_claude_result = {
            "overall_sentiment": "positive",
            "overall_classification": "quote_provided",
            "confidence": 0.9,
            "parts": [
                {
                    "mpn": "LM317T",
                    "status": "quoted",
                    "unit_price": 0.75,
                    "qty_available": 5000,
                    "currency": "USD",
                    "lead_time": "2-3 weeks",
                }
            ],
            "vendor_notes": "Valid for 30 days",
        }

        with patch(
            "app.services.response_parser.routed_structured",
            new_callable=AsyncMock,
            return_value=mock_claude_result,
        ):
            result = await parse_vendor_response(
                email_body="Hi, we can offer LM317T at $0.75 for 5000 pcs.",
                email_subject="RE: RFQ LM317T",
                vendor_name="Arrow Electronics",
            )

        assert result is not None
        assert result["confidence"] == 0.9
        assert result["overall_classification"] == "quote_provided"
        assert len(result["parts"]) == 1

    @pytest.mark.asyncio
    async def test_parse_returns_none_on_failure(self):
        from app.services.response_parser import parse_vendor_response

        with patch(
            "app.services.response_parser.routed_structured",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await parse_vendor_response(
                email_body="Some garbled text",
                email_subject="?",
                vendor_name="Unknown",
            )
        assert result is None

    @pytest.mark.asyncio
    async def test_cross_validates_against_context(self):
        from app.services.response_parser import parse_vendor_response

        mock_result = {
            "overall_sentiment": "positive",
            "overall_classification": "quote_provided",
            "confidence": 0.85,
            "parts": [{"mpn": "LM317T", "status": "quoted", "unit_price": 0.8}],
        }

        with patch(
            "app.services.response_parser.routed_structured",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            result = await parse_vendor_response(
                email_body="Quote: LM317T $0.80",
                email_subject="RE: RFQ",
                vendor_name="Arrow",
                rfq_context={"mpn": "LM317T", "qty": 1000},
            )

        assert result["parts"][0]["mpn_matches_rfq"] is True

    @pytest.mark.asyncio
    async def test_rfq_context_as_list(self):
        """rfq_context as a list builds multi-part context string."""
        from app.services.response_parser import parse_vendor_response

        mock_result = {
            "overall_sentiment": "positive",
            "overall_classification": "quote_provided",
            "confidence": 0.9,
            "parts": [{"mpn": "LM317T", "status": "quoted", "unit_price": 0.5}],
        }

        with patch(
            "app.services.response_parser.routed_structured",
            new_callable=AsyncMock,
            return_value=mock_result,
        ) as mock_claude:
            result = await parse_vendor_response(
                email_body="Quote attached",
                email_subject="RE: RFQ",
                vendor_name="Arrow",
                rfq_context=[
                    {"mpn": "LM317T", "qty": 500},
                    {"mpn": "LM7805", "qty": 1000},
                ],
            )

        # Verify the prompt contains parts list from rfq_context
        prompt_arg = mock_claude.call_args[1]["prompt"]
        assert "LM317T" in prompt_arg
        assert "LM7805" in prompt_arg
        assert result is not None

    @pytest.mark.asyncio
    async def test_extended_thinking_retry_upgrades_confidence(self):
        """Ambiguous confidence triggers retry with smart model; higher confidence
        wins."""
        from app.services.response_parser import parse_vendor_response

        first_result = {
            "overall_sentiment": "neutral",
            "overall_classification": "clarification_needed",
            "confidence": 0.65,  # In review band [0.5, 0.8)
            "parts": [{"mpn": "LM317T", "status": "follow_up"}],
        }
        retry_result = {
            "overall_sentiment": "positive",
            "overall_classification": "quote_provided",
            "confidence": 0.88,  # Higher confidence
            "parts": [{"mpn": "LM317T", "status": "quoted", "unit_price": 0.75}],
        }

        with (
            patch(
                "app.services.response_parser.routed_structured",
                new_callable=AsyncMock,
                return_value=first_result,
            ),
            patch(
                "app.services.response_parser.claude_structured",
                new_callable=AsyncMock,
                return_value=retry_result,
            ) as mock_retry,
        ):
            result = await parse_vendor_response(
                email_body="We can offer LM317T",
                email_subject="RE: RFQ",
                vendor_name="Arrow",
            )

        mock_retry.assert_called_once()  # Extended thinking retry called
        assert result["confidence"] == 0.88  # Retry result used
        assert result["overall_classification"] == "quote_provided"

    @pytest.mark.asyncio
    async def test_extended_thinking_retry_keeps_original_if_no_improvement(self):
        """Retry that doesn't improve confidence keeps original result."""
        from app.services.response_parser import parse_vendor_response

        first_result = {
            "overall_sentiment": "neutral",
            "overall_classification": "clarification_needed",
            "confidence": 0.65,
            "parts": [{"mpn": "X", "status": "follow_up"}],
        }
        retry_result = {
            "overall_sentiment": "neutral",
            "overall_classification": "clarification_needed",
            "confidence": 0.55,  # Lower confidence than first
            "parts": [{"mpn": "X", "status": "follow_up"}],
        }

        with (
            patch(
                "app.services.response_parser.routed_structured",
                new_callable=AsyncMock,
                return_value=first_result,
            ),
            patch(
                "app.services.response_parser.claude_structured",
                new_callable=AsyncMock,
                return_value=retry_result,
            ) as mock_retry,
        ):
            result = await parse_vendor_response(
                email_body="Unclear response",
                email_subject="RE: RFQ",
                vendor_name="Vendor",
            )

        mock_retry.assert_called_once()
        assert result["confidence"] == 0.65  # Original kept

    @pytest.mark.asyncio
    async def test_extended_thinking_retry_returns_none(self):
        """Retry returning None keeps original result."""
        from app.services.response_parser import parse_vendor_response

        first_result = {
            "overall_sentiment": "neutral",
            "overall_classification": "clarification_needed",
            "confidence": 0.6,
            "parts": [],
        }

        with (
            patch(
                "app.services.response_parser.routed_structured",
                new_callable=AsyncMock,
                return_value=first_result,
            ),
            patch(
                "app.services.response_parser.claude_structured",
                new_callable=AsyncMock,
                return_value=None,
            ) as mock_retry,
        ):
            result = await parse_vendor_response(
                email_body="Hmm",
                email_subject="RE: RFQ",
                vendor_name="V",
            )

        mock_retry.assert_called_once()
        assert result["confidence"] == 0.6


# ── Normalization edge cases ──────────────────────────────────────


class TestNormalizeParsedPartsEdgeCases:
    def test_parts_as_json_string(self):
        """Claude batch API sometimes returns parts as a JSON string."""
        result = {"parts": '[{"mpn": "LM317T", "status": "quoted", "unit_price": 0.5}]'}
        _normalize_parsed_parts(result)
        assert isinstance(result["parts"], list)
        assert len(result["parts"]) == 1
        assert result["parts"][0]["mpn"] == "LM317T"

    def test_parts_as_invalid_json_string(self):
        """Invalid JSON string in parts -> parts set to empty list."""
        result = {"parts": "not valid json at all"}
        _normalize_parsed_parts(result)
        assert result["parts"] == []

    def test_non_dict_items_skipped(self):
        """Non-dict items in parts list are skipped without error."""
        result = {
            "parts": [
                {"mpn": "LM317T", "status": "quoted", "unit_price": 0.5},
                "not a dict",
                42,
                None,
                {"mpn": "LM7805", "status": "quoted"},
            ]
        }
        _normalize_parsed_parts(result)
        # Should process the 2 dicts and skip the 3 non-dicts
        assert result["parts"][0]["currency"] == "USD"
        assert result["parts"][-1]["currency"] == "USD"

    def test_all_normalization_fields(self):
        """Exercise all normalization branches: lead_time, condition, date_code, moq, packaging, currency."""
        result = {
            "parts": [
                {
                    "mpn": "LM317T",
                    "status": "quoted",
                    "unit_price": 1.0,
                    "qty_available": 5000,
                    "lead_time": "2 weeks",
                    "condition": "New",
                    "date_code": "2525",
                    "moq": 100,
                    "packaging": "Reel",
                    "currency": "EUR",
                }
            ]
        }
        _normalize_parsed_parts(result)
        part = result["parts"][0]
        assert "lead_time_days" in part
        assert "condition_normalized" in part
        assert "packaging_normalized" in part
        assert part["currency"] == "EUR"
