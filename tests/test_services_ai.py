"""
test_services_ai.py -- Comprehensive tests for app/services/ai_service.py

Tests all public functions:
  - enrich_contacts_websearch: Contact enrichment via Claude + web search
  - company_intel: Company intelligence cards with caching
  - draft_rfq: Smart RFQ email generation
  - rephrase_rfq: RFQ email rephrasing

All Anthropic API calls are mocked via claude_json / claude_text patches.
No real API calls are made.

Called by: pytest
Depends on: app/services/ai_service.py, app/utils/claude_client.py
"""

from unittest.mock import AsyncMock, patch

import pytest

from app.services.ai_service import (
    CONTACT_SEARCH_SCHEMA,
    DEFAULT_TITLE_KEYWORDS,
    FAST,
    INTEL_SCHEMA,
    SMART,
    company_intel,
    draft_rfq,
    enrich_contacts_websearch,
    rephrase_rfq,
)

# ── Constants & schema sanity checks ───────────────────────────────


class TestConstants:
    """Verify module-level constants are correctly defined."""

    def test_model_tier_constants(self):
        assert SMART == "smart"
        assert FAST == "fast"

    def test_default_title_keywords_non_empty(self):
        assert isinstance(DEFAULT_TITLE_KEYWORDS, list)
        assert len(DEFAULT_TITLE_KEYWORDS) > 0
        assert "procurement" in DEFAULT_TITLE_KEYWORDS
        assert "buyer" in DEFAULT_TITLE_KEYWORDS

    def test_contact_search_schema_structure(self):
        assert CONTACT_SEARCH_SCHEMA["type"] == "object"
        assert "contacts" in CONTACT_SEARCH_SCHEMA["properties"]
        items = CONTACT_SEARCH_SCHEMA["properties"]["contacts"]["items"]
        assert "full_name" in items["required"]

    def test_intel_schema_structure(self):
        assert INTEL_SCHEMA["type"] == "object"
        assert "summary" in INTEL_SCHEMA["required"]
        props = INTEL_SCHEMA["properties"]
        assert "revenue" in props
        assert "employees" in props
        assert "components_they_buy" in props
        assert "opportunity_signals" in props


# ── Feature 1: Contact Enrichment via Web Search ───────────────────


class TestEnrichContactsWebsearch:
    """Tests for enrich_contacts_websearch()."""

    async def test_returns_contacts_from_dict_response(self):
        """Standard path: claude_json returns a dict with 'contacts' key."""
        mock_result = {
            "contacts": [
                {
                    "full_name": "Jane Smith",
                    "title": "VP Procurement",
                    "email": "jane@acme.com",
                    "phone": "+1-555-1234",
                    "linkedin_url": "https://linkedin.com/in/janesmith",
                },
                {
                    "full_name": "John Doe",
                    "title": "Buyer",
                    "email": "john@acme.com",
                    "phone": None,
                    "linkedin_url": None,
                },
            ]
        }

        with patch(
            "app.services.ai_service.claude_json",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            contacts = await enrich_contacts_websearch("Acme Corp", domain="acme.com")

        assert len(contacts) == 2
        assert contacts[0]["full_name"] == "Jane Smith"
        assert contacts[0]["title"] == "VP Procurement"
        assert contacts[0]["email"] == "jane@acme.com"
        assert contacts[0]["phone"] == "+1-555-1234"
        assert contacts[0]["source"] == "web_search"

    async def test_returns_contacts_from_list_response(self):
        """Fallback path: claude_json returns a raw list instead of dict."""
        mock_result = [
            {
                "full_name": "Alice Johnson",
                "title": "Supply Chain Manager",
                "email": "alice@example.com",
                "phone": None,
                "linkedin_url": None,
            }
        ]

        with patch(
            "app.services.ai_service.claude_json",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            contacts = await enrich_contacts_websearch("Example Inc")

        assert len(contacts) == 1
        assert contacts[0]["full_name"] == "Alice Johnson"

    async def test_empty_result_returns_empty_list(self):
        """When claude_json returns None, return empty list."""
        with patch(
            "app.services.ai_service.claude_json",
            new_callable=AsyncMock,
            return_value=None,
        ):
            contacts = await enrich_contacts_websearch("Nobody Corp")

        assert contacts == []

    async def test_dict_without_contacts_key(self):
        """claude_json returns a dict missing the 'contacts' key."""
        with patch(
            "app.services.ai_service.claude_json",
            new_callable=AsyncMock,
            return_value={"error": "no results"},
        ):
            contacts = await enrich_contacts_websearch("Ghost Corp")

        assert contacts == []

    async def test_limit_parameter_respected(self):
        """Only return up to 'limit' contacts."""
        mock_result = {
            "contacts": [
                {"full_name": f"Person {i}", "title": "Buyer", "email": f"p{i}@co.com"}
                for i in range(10)
            ]
        }

        with patch(
            "app.services.ai_service.claude_json",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            contacts = await enrich_contacts_websearch("Big Corp", limit=3)

        assert len(contacts) == 3

    async def test_default_limit_is_five(self):
        """Default limit is 5 contacts."""
        mock_result = {
            "contacts": [
                {"full_name": f"Person {i}", "title": "Buyer"}
                for i in range(10)
            ]
        }

        with patch(
            "app.services.ai_service.claude_json",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            contacts = await enrich_contacts_websearch("Big Corp")

        assert len(contacts) == 5

    async def test_contacts_without_full_name_skipped(self):
        """Contacts missing full_name are filtered out."""
        mock_result = {
            "contacts": [
                {"full_name": "Good Contact", "title": "Buyer"},
                {"title": "Missing Name"},
                {"full_name": "", "title": "Empty Name"},
                {"full_name": None, "title": "Null Name"},
            ]
        }

        with patch(
            "app.services.ai_service.claude_json",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            contacts = await enrich_contacts_websearch("Filter Corp")

        assert len(contacts) == 1
        assert contacts[0]["full_name"] == "Good Contact"

    async def test_non_dict_contacts_skipped(self):
        """Non-dict entries in the contacts list are skipped."""
        mock_result = {
            "contacts": [
                {"full_name": "Valid", "title": "Buyer"},
                "not a dict",
                42,
                None,
            ]
        }

        with patch(
            "app.services.ai_service.claude_json",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            contacts = await enrich_contacts_websearch("Mixed Corp")

        assert len(contacts) == 1

    async def test_confidence_medium_when_email_matches_domain(self):
        """Confidence = medium when email domain matches company domain."""
        mock_result = {
            "contacts": [
                {
                    "full_name": "Jane",
                    "email": "jane@acme.com",
                    "title": "Buyer",
                }
            ]
        }

        with patch(
            "app.services.ai_service.claude_json",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            contacts = await enrich_contacts_websearch(
                "Acme Corp", domain="acme.com"
            )

        assert contacts[0]["confidence"] == "medium"

    async def test_confidence_medium_when_email_no_domain_match(self):
        """Confidence = medium when email exists but domain doesn't match."""
        mock_result = {
            "contacts": [
                {
                    "full_name": "Jane",
                    "email": "jane@gmail.com",
                    "title": "Buyer",
                }
            ]
        }

        with patch(
            "app.services.ai_service.claude_json",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            contacts = await enrich_contacts_websearch(
                "Acme Corp", domain="acme.com"
            )

        assert contacts[0]["confidence"] == "medium"

    async def test_confidence_low_when_no_email_but_linkedin(self):
        """Confidence = low when no email but LinkedIn present."""
        mock_result = {
            "contacts": [
                {
                    "full_name": "Jane",
                    "email": None,
                    "linkedin_url": "https://linkedin.com/in/jane",
                }
            ]
        }

        with patch(
            "app.services.ai_service.claude_json",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            contacts = await enrich_contacts_websearch("Acme Corp")

        assert contacts[0]["confidence"] == "low"

    async def test_confidence_low_when_no_email_no_linkedin(self):
        """Confidence = low when only name is present."""
        mock_result = {
            "contacts": [
                {
                    "full_name": "Jane",
                    "email": None,
                    "linkedin_url": None,
                }
            ]
        }

        with patch(
            "app.services.ai_service.claude_json",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            contacts = await enrich_contacts_websearch("Acme Corp")

        assert contacts[0]["confidence"] == "low"

    async def test_email_normalized_lowercase(self):
        """Email addresses are lowercased."""
        mock_result = {
            "contacts": [
                {"full_name": "Jane", "email": "  Jane@ACME.com  "}
            ]
        }

        with patch(
            "app.services.ai_service.claude_json",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            contacts = await enrich_contacts_websearch("Acme Corp")

        assert contacts[0]["email"] == "jane@acme.com"

    async def test_empty_string_fields_become_none(self):
        """Empty string fields are normalized to None."""
        mock_result = {
            "contacts": [
                {
                    "full_name": "Jane Smith",
                    "title": "",
                    "email": "",
                    "phone": "",
                    "linkedin_url": "",
                }
            ]
        }

        with patch(
            "app.services.ai_service.claude_json",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            contacts = await enrich_contacts_websearch("Acme Corp")

        assert contacts[0]["title"] is None
        assert contacts[0]["email"] is None
        assert contacts[0]["phone"] is None
        assert contacts[0]["linkedin_url"] is None

    async def test_whitespace_stripped_from_fields(self):
        """Leading/trailing whitespace is stripped from all fields."""
        mock_result = {
            "contacts": [
                {
                    "full_name": "  Jane Smith  ",
                    "title": "  VP Procurement  ",
                    "phone": "  +1-555-0100  ",
                    "linkedin_url": "  https://linkedin.com/in/jane  ",
                }
            ]
        }

        with patch(
            "app.services.ai_service.claude_json",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            contacts = await enrich_contacts_websearch("Acme Corp")

        c = contacts[0]
        assert c["full_name"] == "Jane Smith"
        assert c["title"] == "VP Procurement"
        assert c["phone"] == "+1-555-0100"
        assert c["linkedin_url"] == "https://linkedin.com/in/jane"

    async def test_custom_title_keywords(self):
        """Custom title_keywords are passed in the prompt."""
        with patch(
            "app.services.ai_service.claude_json",
            new_callable=AsyncMock,
            return_value={"contacts": []},
        ) as mock_claude:
            await enrich_contacts_websearch(
                "Acme Corp",
                title_keywords=["CTO", "engineering manager"],
            )

        # The prompt (first positional arg) should include custom keywords
        call_args = mock_claude.call_args
        prompt = call_args.args[0]
        assert "CTO" in prompt
        assert "engineering manager" in prompt

    async def test_domain_included_in_prompt(self):
        """When domain is provided it appears in the prompt."""
        with patch(
            "app.services.ai_service.claude_json",
            new_callable=AsyncMock,
            return_value={"contacts": []},
        ) as mock_claude:
            await enrich_contacts_websearch("Acme Corp", domain="acme.com")

        prompt = mock_claude.call_args.args[0]
        assert "acme.com" in prompt

    async def test_domain_not_in_prompt_when_none(self):
        """When domain is None, no domain parenthetical in the prompt."""
        with patch(
            "app.services.ai_service.claude_json",
            new_callable=AsyncMock,
            return_value={"contacts": []},
        ) as mock_claude:
            await enrich_contacts_websearch("Acme Corp", domain=None)

        prompt = mock_claude.call_args.args[0]
        # Should not contain the parenthetical for domain
        assert "()" not in prompt

    async def test_uses_smart_model_tier(self):
        """Contact enrichment uses the SMART model tier."""
        with patch(
            "app.services.ai_service.claude_json",
            new_callable=AsyncMock,
            return_value={"contacts": []},
        ) as mock_claude:
            await enrich_contacts_websearch("Acme Corp")

        kwargs = mock_claude.call_args.kwargs
        assert kwargs["model_tier"] == SMART

    async def test_uses_web_search_tool(self):
        """Contact enrichment enables the web_search tool."""
        with patch(
            "app.services.ai_service.claude_json",
            new_callable=AsyncMock,
            return_value={"contacts": []},
        ) as mock_claude:
            await enrich_contacts_websearch("Acme Corp")

        kwargs = mock_claude.call_args.kwargs
        assert kwargs["tools"] is not None
        tool_types = [t["type"] for t in kwargs["tools"]]
        assert "web_search_20250305" in tool_types

    async def test_source_always_web_search(self):
        """All returned contacts have source='web_search'."""
        mock_result = {
            "contacts": [
                {"full_name": "A", "email": "a@co.com"},
                {"full_name": "B", "email": "b@co.com"},
            ]
        }

        with patch(
            "app.services.ai_service.claude_json",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            contacts = await enrich_contacts_websearch("Test Co")

        for c in contacts:
            assert c["source"] == "web_search"

    async def test_unexpected_return_type_returns_empty(self):
        """If claude_json returns an unexpected type (e.g., string), return empty."""
        with patch(
            "app.services.ai_service.claude_json",
            new_callable=AsyncMock,
            return_value="not a dict or list",
        ):
            contacts = await enrich_contacts_websearch("Bad Corp")

        assert contacts == []

    async def test_int_return_type_returns_empty(self):
        """If claude_json returns an int, return empty."""
        with patch(
            "app.services.ai_service.claude_json",
            new_callable=AsyncMock,
            return_value=42,
        ):
            contacts = await enrich_contacts_websearch("Bad Corp")

        assert contacts == []


# ── Feature 3: Company Intelligence Cards ──────────────────────────


class TestCompanyIntel:
    """Tests for company_intel()."""

    async def test_returns_intel_dict(self):
        """Standard path: claude_json returns valid intel dict."""
        mock_intel = {
            "summary": "Acme is a major electronics manufacturer.",
            "revenue": "$500M",
            "employees": "2000",
            "products": "Consumer electronics",
            "components_they_buy": ["capacitors", "resistors", "ICs"],
            "recent_news": ["Acme expands factory"],
            "opportunity_signals": ["New product line launching Q3"],
            "sources": ["acme.com", "reuters.com"],
        }

        with patch(
            "app.services.ai_service.claude_json",
            new_callable=AsyncMock,
            return_value=mock_intel,
        ), patch(
            "app.services.ai_service.get_cached",
            return_value=None,
        ), patch(
            "app.services.ai_service.set_cached",
        ) as mock_set:
            result = await company_intel("Acme Corp")

        assert result is not None
        assert result["summary"] == "Acme is a major electronics manufacturer."
        assert result["revenue"] == "$500M"
        assert "capacitors" in result["components_they_buy"]
        # Verify caching was called
        mock_set.assert_called_once()

    async def test_returns_cached_result(self):
        """When cache hit, return cached data without calling Claude."""
        cached_data = {"summary": "Cached intel", "revenue": "$1B"}

        with patch(
            "app.services.ai_service.get_cached",
            return_value=cached_data,
        ), patch(
            "app.services.ai_service.claude_json",
            new_callable=AsyncMock,
        ) as mock_claude:
            result = await company_intel("Acme Corp")

        assert result == cached_data
        mock_claude.assert_not_called()

    async def test_cache_key_lowered_and_stripped(self):
        """Cache key normalizes company name to lowercase/stripped."""
        with patch(
            "app.services.ai_service.get_cached",
            return_value=None,
        ) as mock_get, patch(
            "app.services.ai_service.claude_json",
            new_callable=AsyncMock,
            return_value={"summary": "test"},
        ), patch(
            "app.services.ai_service.set_cached",
        ):
            await company_intel("  ACME Corp  ")

        # get_cached should be called with lowered/stripped key
        mock_get.assert_called_once_with("intel:acme corp")

    async def test_set_cached_with_7_day_ttl(self):
        """Cache entries use 7-day TTL."""
        with patch(
            "app.services.ai_service.get_cached",
            return_value=None,
        ), patch(
            "app.services.ai_service.claude_json",
            new_callable=AsyncMock,
            return_value={"summary": "test"},
        ), patch(
            "app.services.ai_service.set_cached",
        ) as mock_set:
            await company_intel("Acme Corp")

        mock_set.assert_called_once()
        args = mock_set.call_args
        assert args.kwargs.get("ttl_days") == 7 or args.args[2] == 7

    async def test_returns_none_on_api_failure(self):
        """Returns None when claude_json returns None."""
        with patch(
            "app.services.ai_service.get_cached",
            return_value=None,
        ), patch(
            "app.services.ai_service.claude_json",
            new_callable=AsyncMock,
            return_value=None,
        ), patch(
            "app.services.ai_service.set_cached",
        ) as mock_set:
            result = await company_intel("Ghost Corp")

        assert result is None
        mock_set.assert_not_called()

    async def test_returns_none_on_non_dict_response(self):
        """Returns None when claude_json returns a non-dict (e.g., list, string)."""
        with patch(
            "app.services.ai_service.get_cached",
            return_value=None,
        ), patch(
            "app.services.ai_service.claude_json",
            new_callable=AsyncMock,
            return_value=["not", "a", "dict"],
        ), patch(
            "app.services.ai_service.set_cached",
        ) as mock_set:
            result = await company_intel("Bad Corp")

        assert result is None
        mock_set.assert_not_called()

    async def test_returns_none_on_empty_dict(self):
        """Returns None when claude_json returns an empty dict (falsy check)."""
        with patch(
            "app.services.ai_service.get_cached",
            return_value=None,
        ), patch(
            "app.services.ai_service.claude_json",
            new_callable=AsyncMock,
            return_value={},
        ), patch(
            "app.services.ai_service.set_cached",
        ) as mock_set:
            result = await company_intel("Empty Corp")

        # Empty dict is falsy -> should return None
        assert result is None
        mock_set.assert_not_called()

    async def test_domain_included_in_prompt(self):
        """When domain is provided it appears in the prompt."""
        with patch(
            "app.services.ai_service.get_cached",
            return_value=None,
        ), patch(
            "app.services.ai_service.claude_json",
            new_callable=AsyncMock,
            return_value={"summary": "test"},
        ) as mock_claude, patch(
            "app.services.ai_service.set_cached",
        ):
            await company_intel("Acme Corp", domain="acme.com")

        prompt = mock_claude.call_args.args[0]
        assert "acme.com" in prompt

    async def test_domain_not_in_prompt_when_none(self):
        """When domain is None, no domain parenthetical."""
        with patch(
            "app.services.ai_service.get_cached",
            return_value=None,
        ), patch(
            "app.services.ai_service.claude_json",
            new_callable=AsyncMock,
            return_value={"summary": "test"},
        ) as mock_claude, patch(
            "app.services.ai_service.set_cached",
        ):
            await company_intel("Acme Corp", domain=None)

        prompt = mock_claude.call_args.args[0]
        assert "()" not in prompt

    async def test_uses_smart_model_tier(self):
        """Company intel uses the SMART model tier."""
        with patch(
            "app.services.ai_service.get_cached",
            return_value=None,
        ), patch(
            "app.services.ai_service.claude_json",
            new_callable=AsyncMock,
            return_value={"summary": "test"},
        ) as mock_claude, patch(
            "app.services.ai_service.set_cached",
        ):
            await company_intel("Acme Corp")

        kwargs = mock_claude.call_args.kwargs
        assert kwargs["model_tier"] == SMART

    async def test_uses_web_search_tool(self):
        """Company intel enables the web_search tool."""
        with patch(
            "app.services.ai_service.get_cached",
            return_value=None,
        ), patch(
            "app.services.ai_service.claude_json",
            new_callable=AsyncMock,
            return_value={"summary": "test"},
        ) as mock_claude, patch(
            "app.services.ai_service.set_cached",
        ):
            await company_intel("Acme Corp")

        kwargs = mock_claude.call_args.kwargs
        assert kwargs["tools"] is not None
        tool_types = [t["type"] for t in kwargs["tools"]]
        assert "web_search_20250305" in tool_types

    async def test_string_return_from_claude_returns_none(self):
        """If claude_json somehow returns a string, return None."""
        with patch(
            "app.services.ai_service.get_cached",
            return_value=None,
        ), patch(
            "app.services.ai_service.claude_json",
            new_callable=AsyncMock,
            return_value="just a string",
        ), patch(
            "app.services.ai_service.set_cached",
        ) as mock_set:
            result = await company_intel("String Corp")

        assert result is None
        mock_set.assert_not_called()


# ── Feature 4: Smart RFQ Email Drafts ─────────────────────────────


class TestDraftRfq:
    """Tests for draft_rfq()."""

    async def test_returns_email_body(self):
        """Standard path: returns generated email body."""
        expected_body = (
            "We are looking to source the following parts:\n\n"
            "- LM317T: 1000 pcs (target: $0.50)\n\n"
            "Please provide your best pricing and availability."
        )

        with patch(
            "app.services.ai_service.claude_text",
            new_callable=AsyncMock,
            return_value=expected_body,
        ):
            result = await draft_rfq(
                vendor_name="Arrow Electronics",
                parts=[{"mpn": "LM317T", "qty": 1000, "target_price": 0.50}],
            )

        assert result == expected_body

    async def test_returns_none_on_api_failure(self):
        """Returns None when claude_text returns None."""
        with patch(
            "app.services.ai_service.claude_text",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await draft_rfq(
                vendor_name="Arrow",
                parts=[{"mpn": "LM317T", "qty": 1000}],
            )

        assert result is None

    async def test_vendor_history_included_in_prompt(self):
        """Vendor history context is injected into the prompt."""
        history = {
            "total_rfqs": 15,
            "total_offers": 10,
            "last_contact_date": "2026-01-15",
            "avg_response_hours": 4.5,
            "best_price": "$0.45",
        }

        with patch(
            "app.services.ai_service.claude_text",
            new_callable=AsyncMock,
            return_value="email body",
        ) as mock_claude:
            await draft_rfq(
                vendor_name="Arrow Electronics",
                parts=[{"mpn": "LM317T", "qty": 1000}],
                vendor_history=history,
            )

        prompt = mock_claude.call_args.args[0]
        assert "15" in prompt  # total_rfqs
        assert "10" in prompt  # total_offers
        assert "2026-01-15" in prompt  # last_contact_date
        assert "4.5" in prompt  # avg_response_hours
        assert "$0.45" in prompt  # best_price

    async def test_no_history_context_when_none(self):
        """No history section in prompt when vendor_history is None."""
        with patch(
            "app.services.ai_service.claude_text",
            new_callable=AsyncMock,
            return_value="email body",
        ) as mock_claude:
            await draft_rfq(
                vendor_name="Arrow Electronics",
                parts=[{"mpn": "LM317T", "qty": 1000}],
                vendor_history=None,
            )

        prompt = mock_claude.call_args.args[0]
        assert "Past relationship context" not in prompt

    async def test_target_price_in_parts_string(self):
        """Target price is shown when provided."""
        with patch(
            "app.services.ai_service.claude_text",
            new_callable=AsyncMock,
            return_value="email body",
        ) as mock_claude:
            await draft_rfq(
                vendor_name="Arrow",
                parts=[{"mpn": "LM317T", "qty": 1000, "target_price": 0.50}],
            )

        prompt = mock_claude.call_args.args[0]
        assert "target: $0.5" in prompt

    async def test_no_target_price_when_absent(self):
        """When target_price is missing, no target mentioned."""
        with patch(
            "app.services.ai_service.claude_text",
            new_callable=AsyncMock,
            return_value="email body",
        ) as mock_claude:
            await draft_rfq(
                vendor_name="Arrow",
                parts=[{"mpn": "LM317T", "qty": 1000}],
            )

        prompt = mock_claude.call_args.args[0]
        assert "target:" not in prompt

    async def test_multiple_parts_in_prompt(self):
        """Multiple parts are all listed in the prompt."""
        parts = [
            {"mpn": "LM317T", "qty": 1000, "target_price": 0.50},
            {"mpn": "LM7805", "qty": 500},
            {"mpn": "TL431", "qty": 2000, "target_price": 0.25},
        ]

        with patch(
            "app.services.ai_service.claude_text",
            new_callable=AsyncMock,
            return_value="email body",
        ) as mock_claude:
            await draft_rfq(vendor_name="Arrow", parts=parts)

        prompt = mock_claude.call_args.args[0]
        assert "LM317T" in prompt
        assert "LM7805" in prompt
        assert "TL431" in prompt

    async def test_parts_limited_to_20(self):
        """Only the first 20 parts are included in the prompt."""
        parts = [{"mpn": f"PART-{i:03d}", "qty": 100} for i in range(30)]

        with patch(
            "app.services.ai_service.claude_text",
            new_callable=AsyncMock,
            return_value="email body",
        ) as mock_claude:
            await draft_rfq(vendor_name="Arrow", parts=parts)

        prompt = mock_claude.call_args.args[0]
        assert "PART-019" in prompt
        assert "PART-020" not in prompt

    async def test_uses_fast_model_tier(self):
        """RFQ drafts use the FAST model tier."""
        with patch(
            "app.services.ai_service.claude_text",
            new_callable=AsyncMock,
            return_value="email body",
        ) as mock_claude:
            await draft_rfq(
                vendor_name="Arrow",
                parts=[{"mpn": "LM317T", "qty": 1000}],
            )

        kwargs = mock_claude.call_args.kwargs
        assert kwargs["model_tier"] == FAST

    async def test_user_name_in_prompt(self):
        """User name is passed into the prompt."""
        with patch(
            "app.services.ai_service.claude_text",
            new_callable=AsyncMock,
            return_value="email body",
        ) as mock_claude:
            await draft_rfq(
                vendor_name="Arrow",
                parts=[{"mpn": "LM317T", "qty": 1000}],
                user_name="Alice",
            )

        prompt = mock_claude.call_args.args[0]
        assert "Alice" in prompt

    async def test_default_sender_when_no_user_name(self):
        """Default sender label when user_name is empty."""
        with patch(
            "app.services.ai_service.claude_text",
            new_callable=AsyncMock,
            return_value="email body",
        ) as mock_claude:
            await draft_rfq(
                vendor_name="Arrow",
                parts=[{"mpn": "LM317T", "qty": 1000}],
                user_name="",
            )

        prompt = mock_claude.call_args.args[0]
        assert "the buyer" in prompt

    async def test_vendor_name_in_prompt(self):
        """Vendor name appears in the prompt."""
        with patch(
            "app.services.ai_service.claude_text",
            new_callable=AsyncMock,
            return_value="email body",
        ) as mock_claude:
            await draft_rfq(
                vendor_name="Mouser Electronics",
                parts=[{"mpn": "LM317T", "qty": 1000}],
            )

        prompt = mock_claude.call_args.args[0]
        assert "Mouser Electronics" in prompt

    async def test_empty_parts_list(self):
        """Empty parts list still calls claude_text."""
        with patch(
            "app.services.ai_service.claude_text",
            new_callable=AsyncMock,
            return_value="email body",
        ) as mock_claude:
            result = await draft_rfq(vendor_name="Arrow", parts=[])

        assert result == "email body"
        mock_claude.assert_called_once()

    async def test_parts_with_missing_fields(self):
        """Parts with missing mpn/qty use '?' placeholder."""
        with patch(
            "app.services.ai_service.claude_text",
            new_callable=AsyncMock,
            return_value="email body",
        ) as mock_claude:
            await draft_rfq(
                vendor_name="Arrow",
                parts=[{"target_price": 0.50}],  # no mpn or qty
            )

        prompt = mock_claude.call_args.args[0]
        assert "?" in prompt

    async def test_vendor_history_with_missing_fields(self):
        """Vendor history dict with missing keys uses defaults gracefully."""
        history = {"total_rfqs": 5}  # other fields missing

        with patch(
            "app.services.ai_service.claude_text",
            new_callable=AsyncMock,
            return_value="email body",
        ) as mock_claude:
            await draft_rfq(
                vendor_name="Arrow",
                parts=[{"mpn": "LM317T", "qty": 1000}],
                vendor_history=history,
            )

        prompt = mock_claude.call_args.args[0]
        assert "5" in prompt
        assert "unknown" in prompt  # default for missing fields


# ── Feature 4b: Rephrase RFQ ──────────────────────────────────────


class TestRephraseRfq:
    """Tests for rephrase_rfq()."""

    async def test_returns_rephrased_body(self):
        """Standard path: returns rephrased email body."""
        original = "Please quote LM317T x1000 pcs."
        rephrased = "Could you provide pricing for LM317T, quantity 1000?"

        with patch(
            "app.services.ai_service.claude_text",
            new_callable=AsyncMock,
            return_value=rephrased,
        ):
            result = await rephrase_rfq(original)

        assert result == rephrased

    async def test_returns_none_on_api_failure(self):
        """Returns None when claude_text returns None."""
        with patch(
            "app.services.ai_service.claude_text",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await rephrase_rfq("Please quote LM317T.")

        assert result is None

    async def test_original_body_in_prompt(self):
        """The original email body is included in the prompt."""
        original = "Please quote LM317T x1000 pcs at $0.50 target."

        with patch(
            "app.services.ai_service.claude_text",
            new_callable=AsyncMock,
            return_value="rephrased text",
        ) as mock_claude:
            await rephrase_rfq(original)

        prompt = mock_claude.call_args.args[0]
        assert original in prompt

    async def test_uses_fast_model_tier(self):
        """Rephrase uses the FAST model tier."""
        with patch(
            "app.services.ai_service.claude_text",
            new_callable=AsyncMock,
            return_value="rephrased text",
        ) as mock_claude:
            await rephrase_rfq("Please quote LM317T.")

        kwargs = mock_claude.call_args.kwargs
        assert kwargs["model_tier"] == FAST

    async def test_max_tokens_is_800(self):
        """Rephrase requests 800 max tokens."""
        with patch(
            "app.services.ai_service.claude_text",
            new_callable=AsyncMock,
            return_value="rephrased text",
        ) as mock_claude:
            await rephrase_rfq("Please quote LM317T.")

        kwargs = mock_claude.call_args.kwargs
        assert kwargs["max_tokens"] == 800

    async def test_empty_body_still_calls_claude(self):
        """Even with empty body, the function calls claude_text."""
        with patch(
            "app.services.ai_service.claude_text",
            new_callable=AsyncMock,
            return_value="rephrased empty",
        ) as mock_claude:
            result = await rephrase_rfq("")

        assert result == "rephrased empty"
        mock_claude.assert_called_once()

    async def test_long_body_passed_intact(self):
        """A long email body is passed through without truncation."""
        long_body = "Line of text. " * 500  # ~7500 chars

        with patch(
            "app.services.ai_service.claude_text",
            new_callable=AsyncMock,
            return_value="rephrased",
        ) as mock_claude:
            await rephrase_rfq(long_body)

        prompt = mock_claude.call_args.args[0]
        assert long_body in prompt


# ── Error handling & edge cases ────────────────────────────────────


class TestErrorHandling:
    """Cross-cutting error handling and edge case tests."""

    async def test_enrich_contacts_claude_exception_propagates(self):
        """If claude_json raises an unexpected exception, it propagates."""
        with patch(
            "app.services.ai_service.claude_json",
            new_callable=AsyncMock,
            side_effect=RuntimeError("Unexpected error"),
        ):
            with pytest.raises(RuntimeError, match="Unexpected error"):
                await enrich_contacts_websearch("Error Corp")

    async def test_company_intel_claude_exception_propagates(self):
        """If claude_json raises an unexpected exception, it propagates."""
        with patch(
            "app.services.ai_service.get_cached",
            return_value=None,
        ), patch(
            "app.services.ai_service.claude_json",
            new_callable=AsyncMock,
            side_effect=RuntimeError("API down"),
        ):
            with pytest.raises(RuntimeError, match="API down"):
                await company_intel("Error Corp")

    async def test_draft_rfq_claude_exception_propagates(self):
        """If claude_text raises an unexpected exception, it propagates."""
        with patch(
            "app.services.ai_service.claude_text",
            new_callable=AsyncMock,
            side_effect=TimeoutError("Timed out"),
        ):
            with pytest.raises(TimeoutError, match="Timed out"):
                await draft_rfq(
                    vendor_name="Timeout Vendor",
                    parts=[{"mpn": "X", "qty": 1}],
                )

    async def test_rephrase_rfq_claude_exception_propagates(self):
        """If claude_text raises an unexpected exception, it propagates."""
        with patch(
            "app.services.ai_service.claude_text",
            new_callable=AsyncMock,
            side_effect=ConnectionError("Network failure"),
        ):
            with pytest.raises(ConnectionError, match="Network failure"):
                await rephrase_rfq("Some body")

    async def test_enrich_contacts_empty_company_name(self):
        """Empty company name still calls claude_json."""
        with patch(
            "app.services.ai_service.claude_json",
            new_callable=AsyncMock,
            return_value={"contacts": []},
        ) as mock_claude:
            result = await enrich_contacts_websearch("")

        assert result == []
        mock_claude.assert_called_once()

    async def test_company_intel_empty_company_name(self):
        """Empty company name still proceeds (caching with empty key)."""
        with patch(
            "app.services.ai_service.get_cached",
            return_value=None,
        ), patch(
            "app.services.ai_service.claude_json",
            new_callable=AsyncMock,
            return_value={"summary": "minimal"},
        ), patch(
            "app.services.ai_service.set_cached",
        ):
            result = await company_intel("")

        assert result is not None
        assert result["summary"] == "minimal"

    async def test_enrich_contacts_malformed_contact_entries(self):
        """Gracefully handles a mix of malformed entries."""
        mock_result = {
            "contacts": [
                {"full_name": "Good"},
                {},                       # empty dict, no full_name
                {"full_name": "Also Good", "email": "good@co.com"},
                {"random_key": "value"},  # no full_name
            ]
        }

        with patch(
            "app.services.ai_service.claude_json",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            contacts = await enrich_contacts_websearch("Mixed Corp")

        assert len(contacts) == 2
        assert contacts[0]["full_name"] == "Good"
        assert contacts[1]["full_name"] == "Also Good"
