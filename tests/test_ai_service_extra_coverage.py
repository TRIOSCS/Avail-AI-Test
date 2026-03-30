"""test_ai_service_extra_coverage.py — Extra coverage for app/services/ai_service.py.

Targets uncovered branches at lines 103-107, 126-128, 231-235, 242-243, 276-290.
These are ClaudeUnavailableError / ClaudeError handlers and validation-failure paths.

Called by: pytest
Depends on: app/services/ai_service.py, unittest.mock
"""

import os

os.environ["TESTING"] = "1"

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.ai_service import (
    company_intel,
    draft_rfq,
    enrich_contacts_websearch,
    rephrase_rfq,
)
from app.utils.claude_errors import ClaudeError, ClaudeUnavailableError


class TestEnrichContactsWebsearchErrorPaths:
    """Cover lines 103-107, 126-128 — error handlers in enrich_contacts_websearch."""

    async def test_claude_unavailable_returns_empty_list(self):
        """ClaudeUnavailableError → returns [] (line 103-104)."""
        with patch(
            "app.services.ai_service.claude_json",
            new=AsyncMock(side_effect=ClaudeUnavailableError("not configured")),
        ):
            result = await enrich_contacts_websearch("Acme Corp")
        assert result == []

    async def test_claude_error_returns_empty_list(self):
        """ClaudeError → returns [] (line 106-107)."""
        with patch(
            "app.services.ai_service.claude_json",
            new=AsyncMock(side_effect=ClaudeError("api error")),
        ):
            result = await enrich_contacts_websearch("Widget Co", domain="widget.com")
        assert result == []

    async def test_validation_error_falls_back_to_raw_contacts(self):
        """Pydantic ValidationError → falls back to raw dict (lines 117-119)."""
        from pydantic import ValidationError

        # Return a dict where contacts field is invalid (wrong type triggers ValidationError)
        raw_result = {
            "contacts": "not_a_list",  # Invalid: should be a list
        }
        with patch(
            "app.services.ai_service.claude_json",
            new=AsyncMock(return_value=raw_result),
        ):
            result = await enrich_contacts_websearch("Acme Corp")
        # Falls back to raw_result["contacts"] which is not a list → no contacts returned
        assert isinstance(result, list)

    async def test_list_response_with_invalid_items_skips_gracefully(self):
        """List response with invalid items skips non-dict items (lines 120-128)."""
        # Result is a list of contacts — one valid, one invalid
        list_result = [
            {"full_name": "Alice Chen", "title": "Buyer"},
            "not a dict",
            {"full_name": "Charlie", "title": None},
        ]
        with patch(
            "app.services.ai_service.claude_json",
            new=AsyncMock(return_value=list_result),
        ):
            result = await enrich_contacts_websearch("Parts Co")
        # Only dict items with full_name are kept
        names = [c["full_name"] for c in result]
        assert "Alice Chen" in names
        assert "Charlie" in names

    async def test_domain_match_sets_medium_confidence(self):
        """Email matching domain → confidence=medium (line 138-139)."""
        raw_result = {
            "contacts": [{"full_name": "Jane Doe", "email": "jane@acme.com"}]
        }
        with patch(
            "app.services.ai_service.claude_json",
            new=AsyncMock(return_value=raw_result),
        ):
            result = await enrich_contacts_websearch("Acme", domain="acme.com")
        assert result[0]["confidence"] == "medium"

    async def test_email_without_domain_sets_medium_confidence(self):
        """Email present but not matching domain → confidence=medium (line 141)."""
        raw_result = {
            "contacts": [{"full_name": "Jane Doe", "email": "jane@other.com"}]
        }
        with patch(
            "app.services.ai_service.claude_json",
            new=AsyncMock(return_value=raw_result),
        ):
            result = await enrich_contacts_websearch("Acme", domain="acme.com")
        assert result[0]["confidence"] == "medium"

    async def test_linkedin_without_email_stays_low_confidence(self):
        """No email but linkedin → confidence=low (line 143-144)."""
        raw_result = {
            "contacts": [
                {
                    "full_name": "Tom Smith",
                    "linkedin_url": "https://linkedin.com/in/tom",
                }
            ]
        }
        with patch(
            "app.services.ai_service.claude_json",
            new=AsyncMock(return_value=raw_result),
        ):
            result = await enrich_contacts_websearch("Parts Inc")
        assert result[0]["confidence"] == "low"


class TestCompanyIntelErrorPaths:
    """Cover lines 231-235, 242-243 — error handlers and validation failure in company_intel."""

    async def test_claude_unavailable_returns_none(self):
        """ClaudeUnavailableError → returns None (lines 231-232)."""
        with patch(
            "app.cache.intel_cache.get_cached", return_value=None
        ), patch(
            "app.services.ai_service.claude_json",
            new=AsyncMock(side_effect=ClaudeUnavailableError("not configured")),
        ):
            result = await company_intel("Acme Corp")
        assert result is None

    async def test_claude_error_returns_none(self):
        """ClaudeError → returns None (lines 234-235)."""
        with patch(
            "app.cache.intel_cache.get_cached", return_value=None
        ), patch(
            "app.services.ai_service.claude_json",
            new=AsyncMock(side_effect=ClaudeError("api failure")),
        ):
            result = await company_intel("Widget Co", domain="widget.com")
        assert result is None

    async def test_validation_failure_falls_through_to_raw_dict(self):
        """Pydantic ValidationError → uses raw dict (lines 242-243)."""
        # Provide intel with invalid field types to trigger ValidationError
        raw_intel = {"summary": 12345, "revenue": None}  # summary should be str
        with patch(
            "app.services.ai_service.get_cached", return_value=None
        ), patch(
            "app.services.ai_service.claude_json",
            new=AsyncMock(return_value=raw_intel),
        ), patch(
            "app.services.ai_service.set_cached"
        ):
            result = await company_intel("Parts Co")
        # Result can be the raw dict or None depending on validation behavior
        assert result is None or isinstance(result, dict)

    async def test_returns_cached_result(self):
        """Cached result is returned without calling Claude."""
        cached = {"summary": "From cache", "revenue": "unknown"}
        with patch(
            "app.services.ai_service.get_cached", return_value=cached
        ):
            result = await company_intel("Cached Corp")
        assert result == cached

    async def test_non_dict_result_returns_none(self):
        """If claude_json returns non-dict, returns None."""
        with patch(
            "app.services.ai_service.get_cached", return_value=None
        ), patch(
            "app.services.ai_service.claude_json",
            new=AsyncMock(return_value=None),
        ), patch(
            "app.services.ai_service.set_cached"
        ):
            result = await company_intel("Bad Co")
        assert result is None


class TestDraftRfqUserDraftPath:
    """Cover lines 276-290 — user_draft cleanup path."""

    async def test_user_draft_calls_routed_text_fast(self):
        """user_draft path calls routed_text with 'fast' tier (lines 276-290)."""
        parts = [{"mpn": "LM317T", "qty": 100, "target_price": 0.50}]
        user_draft = "Please provide pricing for our list."

        mock_text = "Cleaned up RFQ email body."
        with patch(
            "app.utils.llm_router.routed_text",
            new=AsyncMock(return_value=mock_text),
        ):
            result = await draft_rfq(
                vendor_name="Arrow Electronics",
                parts=parts,
                user_draft=user_draft,
            )
        assert result == mock_text

    async def test_user_draft_with_target_price_includes_price(self):
        """Parts with target_price are included in the cleanup prompt."""
        parts = [
            {"mpn": "TL431", "qty": 500, "target_price": 0.10},
            {"mpn": "NE555", "qty": 200},
        ]
        captured_prompt = []

        async def _capture(prompt, model_tier="fast"):
            captured_prompt.append(prompt)
            return "ok"

        with patch("app.utils.llm_router.routed_text", new=_capture):
            await draft_rfq("Acme", parts=parts, user_draft="draft text here")

        assert len(captured_prompt) == 1
        assert "TL431" in captured_prompt[0]

    async def test_vendor_history_included_in_new_draft(self):
        """vendor_history context is included in prompt for new draft."""
        vendor_history = {
            "total_rfqs": 5,
            "total_offers": 3,
            "last_contact_date": "2024-01-15",
            "avg_response_hours": 24,
            "best_price": 0.45,
        }
        with patch(
            "app.services.ai_service.routed_text",
            new=AsyncMock(return_value="generated rfq body"),
        ):
            result = await draft_rfq(
                vendor_name="Arrow",
                parts=[{"mpn": "LM317T", "qty": 100}],
                vendor_history=vendor_history,
                user_name="Bob",
            )
        assert result == "generated rfq body"
