"""test_services_response_parser_coverage_new.py — Coverage tests for missing error paths.

Covers lines 156-161, 170-171, 189-190, 197-198 in app/services/response_parser.py.

Called by: pytest
Depends on: app/services/response_parser.py, app/utils/claude_errors.py
"""

import os

os.environ["TESTING"] = "1"

from unittest.mock import AsyncMock, patch

import pytest

from app.utils.claude_errors import ClaudeError, ClaudeUnavailableError


class TestParseVendorResponseErrorPaths:
    """Tests for exception handling in parse_vendor_response."""

    @pytest.mark.asyncio
    async def test_claude_unavailable_returns_none(self):
        """Lines 156-157: ClaudeUnavailableError → returns None."""
        from app.services.response_parser import parse_vendor_response

        with patch(
            "app.services.response_parser.routed_structured",
            new_callable=AsyncMock,
            side_effect=ClaudeUnavailableError("Claude not configured"),
        ):
            result = await parse_vendor_response(
                email_body="We can quote LM317T at $0.75",
                email_subject="RE: RFQ",
                vendor_name="Arrow Electronics",
            )

        assert result is None

    @pytest.mark.asyncio
    async def test_claude_error_returns_none(self):
        """Lines 158-161: ClaudeError → returns None."""
        from app.services.response_parser import parse_vendor_response

        with patch(
            "app.services.response_parser.routed_structured",
            new_callable=AsyncMock,
            side_effect=ClaudeError("API call failed"),
        ):
            result = await parse_vendor_response(
                email_body="Some vendor email",
                email_subject="RE: RFQ",
                vendor_name="Vendor Inc",
            )

        assert result is None

    @pytest.mark.asyncio
    async def test_validation_error_falls_through_with_raw_dict(self):
        """Lines 170-171: ValidationError in first validation → falls through with raw dict."""
        from app.services.response_parser import parse_vendor_response

        # Return a result that passes None check but fails VendorResponseParsed validation
        # A result missing required fields will fail Pydantic validation
        invalid_result = {
            "confidence": 0.9,
            "parts": [],
            # Missing required: overall_sentiment, overall_classification
        }

        with patch(
            "app.services.response_parser.routed_structured",
            new_callable=AsyncMock,
            return_value=invalid_result,
        ):
            result = await parse_vendor_response(
                email_body="Some vendor email",
                email_subject="RE: RFQ",
                vendor_name="Vendor Inc",
            )

        # Falls through — result is the raw dict (not None)
        assert result is not None
        assert result.get("confidence") == 0.9

    @pytest.mark.asyncio
    async def test_claude_error_in_retry_block_keeps_original(self):
        """Lines 189-190: ClaudeError in the extended-thinking retry → keeps original result."""
        from app.services.response_parser import parse_vendor_response

        first_result = {
            "overall_sentiment": "neutral",
            "overall_classification": "clarification_needed",
            "confidence": 0.65,
            "parts": [{"mpn": "LM317T", "status": "follow_up"}],
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
                side_effect=ClaudeError("Retry failed"),
            ) as mock_retry,
        ):
            result = await parse_vendor_response(
                email_body="Ambiguous vendor email",
                email_subject="RE: RFQ",
                vendor_name="Vendor Inc",
            )

        mock_retry.assert_called_once()
        # Original result kept since retry raised ClaudeError (retry=None)
        assert result is not None
        assert result.get("confidence") == 0.65

    @pytest.mark.asyncio
    async def test_validation_error_in_retry_uses_raw_retry_dict(self):
        """Lines 197-198: ValidationError in retry validation → uses raw retry dict."""
        from app.services.response_parser import parse_vendor_response

        first_result = {
            "overall_sentiment": "neutral",
            "overall_classification": "clarification_needed",
            "confidence": 0.65,
            "parts": [{"mpn": "LM317T", "status": "follow_up"}],
        }

        # Retry result has higher confidence but is missing required fields for Pydantic
        retry_result = {
            "confidence": 0.85,  # Higher — will trigger use
            "parts": [{"mpn": "LM317T", "status": "quoted", "unit_price": 0.75}],
            # Missing required: overall_sentiment, overall_classification
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
                email_body="Ambiguous vendor email",
                email_subject="RE: RFQ",
                vendor_name="Vendor Inc",
            )

        mock_retry.assert_called_once()
        # ValidationError on retry validation → result = retry (raw dict)
        assert result is not None
        assert result.get("confidence") == 0.85
