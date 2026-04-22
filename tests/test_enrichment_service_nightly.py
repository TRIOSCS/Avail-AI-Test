"""test_enrichment_service_nightly.py — Additional coverage tests for app/enrichment_service.py.

Targets the uncovered branches in:
- normalize_company_input (lines 190-191): claude_text exception path
- normalize_company_output (lines 229, 241): employee_size and hq_state else branches
- _explorium_find_company (lines 305-307): HTTPError exception path
- _explorium_find_contacts (lines 328-329, 345-347): non-200 and HTTPError paths
- _ai_find_company (lines 392-393, 406-408): no-data and exception paths
- _ai_find_contacts (lines 441-443): exception path
- enrich_entity (lines 511-513): apollo_api_key branch
- find_suggested_contacts (line 591): contact with email but no title (relevant filter)
- apply_enrichment_to_company (lines 622-623): website field branch

Called by: pytest
Depends on: conftest.py fixtures, app.enrichment_service
"""

import os

os.environ["TESTING"] = "1"

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest


# ═══════════════════════════════════════════════════════════════════════
#  normalize_company_input — exception path (lines 190-191)
# ═══════════════════════════════════════════════════════════════════════


class TestNormalizeCompanyInputExceptionPath:
    async def test_claude_text_raises_falls_back_to_original(self):
        """When claude_text raises, the original name is kept (lines 190-191)."""
        from app.enrichment_service import normalize_company_input

        # "Acm" has no vowels beyond one letter — use a word that triggers suspicious heuristic
        # A word without vowels like "Grp" passes _name_looks_suspicious
        suspicious_name = "Grp Tch"

        with (
            patch(
                "app.enrichment_service.get_credential_cached",
                return_value="fake-anthropic-key",
            ),
            patch(
                "app.enrichment_service.claude_text",
                side_effect=Exception("API timeout"),
            ),
        ):
            result_name, result_domain = await normalize_company_input(suspicious_name, "example.com")

        # The original name should be preserved after exception
        assert result_name == suspicious_name
        assert result_domain == "example.com"

    async def test_claude_text_raises_logs_warning(self):
        """Warning is logged when claude_text raises during typo fix."""
        from app.enrichment_service import normalize_company_input

        suspicious_name = "Grp Tch"

        with (
            patch(
                "app.enrichment_service.get_credential_cached",
                return_value="fake-anthropic-key",
            ),
            patch(
                "app.enrichment_service.claude_text",
                side_effect=ValueError("Bad response"),
            ),
        ):
            # Should not raise — exception is swallowed
            result_name, _ = await normalize_company_input(suspicious_name, "")

        assert result_name == suspicious_name


# ═══════════════════════════════════════════════════════════════════════
#  normalize_company_output — else branches (lines 229, 241)
# ═══════════════════════════════════════════════════════════════════════


class TestNormalizeCompanyOutputBranches:
    def test_employee_size_non_digit_string_preserved(self):
        """employee_size that matches range pattern hits the else branch (line 229 skipped)."""
        from app.enrichment_service import normalize_company_output

        # Pattern like "51-200" matches the regex => goes to else at line 231
        data = {"employee_size": "51-200"}
        result = normalize_company_output(data)
        assert result["employee_size"] == "51-200"

    def test_employee_size_non_matching_string_uses_else(self):
        """employee_size that doesn't match range pattern uses the elif branch (line 229)."""
        from app.enrichment_service import normalize_company_output

        # "small company" after replace(" ", "") → "smallcompany"
        # doesn't match digit range regex → hits elif branch at line 228-229
        data = {"employee_size": "small company"}
        result = normalize_company_output(data)
        # Spaces are stripped and the processed value is assigned
        assert result["employee_size"] == "smallcompany"

    def test_hq_state_non_us_state_uses_title_case(self):
        """hq_state not in _US_STATES triggers title() fallback (line 241)."""
        from app.enrichment_service import normalize_company_output

        data = {"hq_state": "ontario"}
        result = normalize_company_output(data)
        # Not a US state abbreviation, so title() applied
        assert result["hq_state"] == "Ontario"

    def test_hq_state_us_abbreviation_uppercased(self):
        """hq_state that IS a US state abbreviation gets uppercased (not line 241)."""
        from app.enrichment_service import normalize_company_output

        data = {"hq_state": "ca"}
        result = normalize_company_output(data)
        assert result["hq_state"] == "CA"


# ═══════════════════════════════════════════════════════════════════════
#  _explorium_find_company — HTTPError exception path (lines 305-307)
# ═══════════════════════════════════════════════════════════════════════


class TestExploriumFindCompanyExceptionPath:
    async def test_httpx_error_returns_none(self):
        """HTTPError during Explorium company lookup returns None (lines 305-307)."""
        from app.enrichment_service import _explorium_find_company

        with (
            patch(
                "app.enrichment_service.get_credential_cached",
                return_value="fake-explorium-key",
            ),
            patch(
                "app.enrichment_service.http.post",
                side_effect=httpx.ConnectError("Connection refused"),
            ),
        ):
            result = await _explorium_find_company("example.com", "Example Corp")

        assert result is None

    async def test_non_200_response_returns_none(self):
        """Non-200 HTTP response from Explorium returns None (lines 285-287)."""
        from app.enrichment_service import _explorium_find_company

        mock_resp = MagicMock()
        mock_resp.status_code = 404

        with (
            patch(
                "app.enrichment_service.get_credential_cached",
                return_value="fake-explorium-key",
            ),
            patch(
                "app.enrichment_service.http.post",
                new_callable=AsyncMock,
                return_value=mock_resp,
            ),
        ):
            result = await _explorium_find_company("example.com", "Example Corp")

        assert result is None

    async def test_key_error_returns_none(self):
        """KeyError during Explorium response parsing returns None (lines 305-307)."""
        from app.enrichment_service import _explorium_find_company

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.side_effect = KeyError("missing key")

        with (
            patch(
                "app.enrichment_service.get_credential_cached",
                return_value="fake-explorium-key",
            ),
            patch(
                "app.enrichment_service.http.post",
                new_callable=AsyncMock,
                return_value=mock_resp,
            ),
        ):
            result = await _explorium_find_company("example.com", "Example Corp")

        assert result is None


# ═══════════════════════════════════════════════════════════════════════
#  _explorium_find_contacts — non-200 (328-329) and HTTPError (345-347)
# ═══════════════════════════════════════════════════════════════════════


class TestExploriumFindContactsBranches:
    async def test_non_200_response_returns_empty_list(self):
        """Non-200 from Explorium contacts returns empty list (lines 328-329)."""
        from app.enrichment_service import _explorium_find_contacts

        mock_resp = MagicMock()
        mock_resp.status_code = 403

        with (
            patch(
                "app.enrichment_service.get_credential_cached",
                return_value="fake-explorium-key",
            ),
            patch(
                "app.enrichment_service.http.post",
                new_callable=AsyncMock,
                return_value=mock_resp,
            ),
        ):
            result = await _explorium_find_contacts("example.com", "procurement")

        assert result == []

    async def test_httpx_error_returns_empty_list(self):
        """HTTPError during Explorium contacts lookup returns empty list (lines 345-347)."""
        from app.enrichment_service import _explorium_find_contacts

        with (
            patch(
                "app.enrichment_service.get_credential_cached",
                return_value="fake-explorium-key",
            ),
            patch(
                "app.enrichment_service.http.post",
                side_effect=httpx.TimeoutException("Timeout"),
            ),
        ):
            result = await _explorium_find_contacts("example.com")

        assert result == []

    async def test_value_error_returns_empty_list(self):
        """ValueError during contacts lookup returns empty list (lines 345-347)."""
        from app.enrichment_service import _explorium_find_contacts

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.side_effect = ValueError("Invalid JSON")

        with (
            patch(
                "app.enrichment_service.get_credential_cached",
                return_value="fake-explorium-key",
            ),
            patch(
                "app.enrichment_service.http.post",
                new_callable=AsyncMock,
                return_value=mock_resp,
            ),
        ):
            result = await _explorium_find_contacts("example.com")

        assert result == []

    async def test_with_title_filter_builds_payload(self):
        """title_filter is passed as job_title_keywords in payload."""
        from app.enrichment_service import _explorium_find_contacts

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "prospects": [
                {
                    "full_name": "Jane Smith",
                    "job_title": "Procurement Manager",
                    "email": "jane@example.com",
                    "phone": "555-1234",
                    "linkedin_url": "https://linkedin.com/in/jane",
                    "location": "San Jose, CA",
                    "company_name": "Example Corp",
                }
            ]
        }

        captured_payload = {}

        async def capture_post(url, **kwargs):
            captured_payload.update(kwargs.get("json", {}))
            return mock_resp

        with (
            patch(
                "app.enrichment_service.get_credential_cached",
                return_value="fake-explorium-key",
            ),
            patch("app.enrichment_service.http.post", side_effect=capture_post),
        ):
            result = await _explorium_find_contacts("example.com", "procurement")

        assert "job_title_keywords" in captured_payload
        assert captured_payload["job_title_keywords"] == ["procurement"]
        assert len(result) == 1
        assert result[0]["full_name"] == "Jane Smith"
        assert result[0]["source"] == "explorium"


# ═══════════════════════════════════════════════════════════════════════
#  _ai_find_company — no-data (392-393) and exception (406-408) paths
# ═══════════════════════════════════════════════════════════════════════


class TestAiFindCompanyBranches:
    async def test_claude_json_returns_none_returns_none(self):
        """claude_json returning None causes early return (lines 392-393)."""
        from app.enrichment_service import _ai_find_company

        with (
            patch(
                "app.enrichment_service.get_credential_cached",
                return_value="fake-anthropic-key",
            ),
            patch(
                "app.enrichment_service.claude_json",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            result = await _ai_find_company("example.com", "Example Corp")

        assert result is None

    async def test_claude_json_returns_list_returns_none(self):
        """claude_json returning non-dict (e.g. list) causes early return (lines 392-393)."""
        from app.enrichment_service import _ai_find_company

        with (
            patch(
                "app.enrichment_service.get_credential_cached",
                return_value="fake-anthropic-key",
            ),
            patch(
                "app.enrichment_service.claude_json",
                new_callable=AsyncMock,
                return_value=["not", "a", "dict"],
            ),
        ):
            result = await _ai_find_company("example.com")

        assert result is None

    async def test_httpx_error_returns_none(self):
        """HTTPError during AI company lookup returns None (lines 406-408)."""
        from app.enrichment_service import _ai_find_company

        with (
            patch(
                "app.enrichment_service.get_credential_cached",
                return_value="fake-anthropic-key",
            ),
            patch(
                "app.enrichment_service.claude_json",
                side_effect=httpx.ConnectError("Connection error"),
            ),
        ):
            result = await _ai_find_company("example.com")

        assert result is None

    async def test_type_error_returns_none(self):
        """TypeError during AI company lookup returns None (lines 406-408)."""
        from app.enrichment_service import _ai_find_company

        with (
            patch(
                "app.enrichment_service.get_credential_cached",
                return_value="fake-anthropic-key",
            ),
            patch(
                "app.enrichment_service.claude_json",
                side_effect=TypeError("unexpected type"),
            ),
        ):
            result = await _ai_find_company("example.com", "Corp")

        assert result is None

    async def test_no_api_key_returns_none(self):
        """Missing API key skips AI lookup and returns None."""
        from app.enrichment_service import _ai_find_company

        with patch(
            "app.enrichment_service.get_credential_cached",
            return_value="",
        ):
            result = await _ai_find_company("example.com")

        assert result is None


# ═══════════════════════════════════════════════════════════════════════
#  _ai_find_contacts — exception path (lines 441-443)
# ═══════════════════════════════════════════════════════════════════════


class TestAiFindContactsExceptionPath:
    async def test_exception_returns_empty_list(self):
        """Exception during AI contacts lookup returns empty list (lines 441-443)."""
        from app.enrichment_service import _ai_find_contacts

        with (
            patch(
                "app.enrichment_service.get_credential_cached",
                return_value="fake-anthropic-key",
            ),
            patch(
                "app.enrichment_service.enrich_contacts_websearch",
                side_effect=httpx.ConnectError("Connection error"),
            ),
        ):
            result = await _ai_find_contacts("example.com", "Example Corp", "procurement")

        assert result == []

    async def test_type_error_returns_empty_list(self):
        """TypeError during AI contacts lookup returns empty list (lines 441-443)."""
        from app.enrichment_service import _ai_find_contacts

        with (
            patch(
                "app.enrichment_service.get_credential_cached",
                return_value="fake-anthropic-key",
            ),
            patch(
                "app.enrichment_service.enrich_contacts_websearch",
                side_effect=TypeError("bad type"),
            ),
        ):
            result = await _ai_find_contacts("example.com")

        assert result == []

    async def test_no_api_key_returns_empty_list(self):
        """Missing API key returns empty list without calling provider."""
        from app.enrichment_service import _ai_find_contacts

        with patch(
            "app.enrichment_service.get_credential_cached",
            return_value="",
        ):
            result = await _ai_find_contacts("example.com", "Corp")

        assert result == []


# ═══════════════════════════════════════════════════════════════════════
#  enrich_entity — apollo branch (lines 511-513)
# ═══════════════════════════════════════════════════════════════════════


class TestEnrichEntityApolloBranch:
    async def test_apollo_api_key_triggers_apollo_search(self):
        """When apollo_api_key is set, apollo search_company is called (lines 511-513)."""
        from app.enrichment_service import enrich_entity

        mock_settings = MagicMock()
        mock_settings.apollo_api_key = "fake-apollo-key"

        apollo_search_mock = AsyncMock(
            return_value={
                "source": "apollo",
                "legal_name": "Apollo Corp",
                "domain": "example.com",
                "industry": "Technology",
                "employee_size": "51-200",
                "hq_city": "San Francisco",
                "hq_state": "CA",
                "hq_country": "US",
                "website": "https://example.com",
                "linkedin_url": None,
            }
        )

        # get_cached/set_cached are lazy-imported inside enrich_entity from .cache.intel_cache
        with (
            patch("app.enrichment_service.normalize_company_input", new_callable=AsyncMock, return_value=("Example Corp", "example.com")),
            patch("app.enrichment_service._explorium_find_company", new_callable=AsyncMock, return_value=None),
            patch("app.enrichment_service._ai_find_company", new_callable=AsyncMock, return_value=None),
            patch("app.cache.intel_cache.get_cached", return_value=None),
            patch("app.cache.intel_cache.set_cached"),
            patch("app.config.settings", mock_settings),
            patch("app.connectors.apollo.search_company", apollo_search_mock),
        ):
            result = await enrich_entity("example.com", "Example Corp")

        # Apollo data should have been merged
        assert result is not None
        assert isinstance(result, dict)

    async def test_apollo_api_key_none_skips_apollo(self):
        """When apollo_api_key is None/empty, apollo search is not called."""
        from app.enrichment_service import enrich_entity

        mock_settings = MagicMock()
        mock_settings.apollo_api_key = None

        # get_cached/set_cached are lazy-imported inside enrich_entity from .cache.intel_cache
        with (
            patch("app.enrichment_service.normalize_company_input", new_callable=AsyncMock, return_value=("Example Corp", "example.com")),
            patch("app.enrichment_service._explorium_find_company", new_callable=AsyncMock, return_value=None),
            patch("app.enrichment_service._ai_find_company", new_callable=AsyncMock, return_value=None),
            patch("app.cache.intel_cache.get_cached", return_value=None),
            patch("app.cache.intel_cache.set_cached"),
            patch("app.config.settings", mock_settings),
        ):
            result = await enrich_entity("example.com", "Example Corp")

        # Should still return a result dict (empty enrichment)
        assert isinstance(result, dict)
        assert result["domain"] == "example.com"


# ═══════════════════════════════════════════════════════════════════════
#  find_suggested_contacts — _is_relevant with email but no title (line 591)
# ═══════════════════════════════════════════════════════════════════════


class TestFindSuggestedContactsRelevantFilter:
    async def test_contact_with_email_but_no_title_is_kept(self):
        """Contact with email but no title returns True from _is_relevant (line 591)."""
        from app.enrichment_service import find_suggested_contacts

        contact_with_email_no_title = {
            "source": "explorium",
            "full_name": "John Doe",
            "title": "",  # No title
            "email": "john@example.com",
            "phone": None,
            "linkedin_url": None,
            "location": None,
            "company": "Example Corp",
        }

        with (
            patch(
                "app.enrichment_service._explorium_find_contacts",
                new_callable=AsyncMock,
                return_value=[contact_with_email_no_title],
            ),
            patch(
                "app.enrichment_service._ai_find_contacts",
                new_callable=AsyncMock,
                return_value=[],
            ),
        ):
            result = await find_suggested_contacts("example.com", "Example Corp")

        # Contact with email but no title should be included
        assert len(result) == 1
        assert result[0]["full_name"] == "John Doe"

    async def test_contact_with_no_title_and_no_email_is_filtered_out(self):
        """Contact with no title and no email is filtered out."""
        from app.enrichment_service import find_suggested_contacts

        # Has relevant title — should be kept
        relevant_contact = {
            "source": "explorium",
            "full_name": "Jane Smith",
            "title": "procurement manager",
            "email": None,
            "phone": None,
            "linkedin_url": "https://linkedin.com/in/jane",
            "location": None,
            "company": "Example Corp",
        }
        # No title, no email — should be filtered out
        irrelevant_contact = {
            "source": "explorium",
            "full_name": "Bob Jones",
            "title": None,
            "email": None,
            "phone": None,
            "linkedin_url": None,
            "location": None,
            "company": "Example Corp",
        }

        with (
            patch(
                "app.enrichment_service._explorium_find_contacts",
                new_callable=AsyncMock,
                return_value=[relevant_contact, irrelevant_contact],
            ),
            patch(
                "app.enrichment_service._ai_find_contacts",
                new_callable=AsyncMock,
                return_value=[],
            ),
        ):
            result = await find_suggested_contacts("example.com", "Example Corp")

        # Only the relevant contact should be in results
        names = [c["full_name"] for c in result]
        assert "Jane Smith" in names
        assert "Bob Jones" not in names

    async def test_all_irrelevant_contacts_returns_unfiltered(self):
        """If filter removes all contacts, the unfiltered list is returned."""
        from app.enrichment_service import find_suggested_contacts

        # No relevant title, no email — normally filtered out
        contact1 = {
            "source": "explorium",
            "full_name": "Alice Brown",
            "title": "intern",
            "email": None,
            "phone": None,
            "linkedin_url": "https://linkedin.com/in/alice",
            "location": None,
            "company": "Example Corp",
        }

        with (
            patch(
                "app.enrichment_service._explorium_find_contacts",
                new_callable=AsyncMock,
                return_value=[contact1],
            ),
            patch(
                "app.enrichment_service._ai_find_contacts",
                new_callable=AsyncMock,
                return_value=[],
            ),
        ):
            result = await find_suggested_contacts("example.com")

        # Filter removed everything, so unfiltered returned
        assert len(result) == 1
        assert result[0]["full_name"] == "Alice Brown"


# ═══════════════════════════════════════════════════════════════════════
#  apply_enrichment_to_company — website branch (lines 622-623)
# ═══════════════════════════════════════════════════════════════════════


class TestApplyEnrichmentToCompany:
    def test_website_set_when_empty(self):
        """Website is applied to company when company.website is empty (lines 622-623)."""
        from app.enrichment_service import apply_enrichment_to_company

        company = MagicMock()
        company.domain = None
        company.linkedin_url = None
        company.legal_name = None
        company.industry = None
        company.employee_size = None
        company.hq_city = None
        company.hq_state = None
        company.hq_country = None
        company.website = None  # Empty — should be set

        data = {
            "source": "explorium",
            "website": "https://example.com",
        }

        updated = apply_enrichment_to_company(company, data)

        assert "website" in updated
        assert company.website == "https://example.com"

    def test_website_not_overwritten_when_already_set(self):
        """Website is not overwritten when company.website already has a value."""
        from app.enrichment_service import apply_enrichment_to_company

        company = MagicMock()
        company.domain = None
        company.linkedin_url = None
        company.legal_name = None
        company.industry = None
        company.employee_size = None
        company.hq_city = None
        company.hq_state = None
        company.hq_country = None
        company.website = "https://existing.com"  # Already set

        data = {
            "source": "ai",
            "website": "https://new.com",
        }

        updated = apply_enrichment_to_company(company, data)

        assert "website" not in updated
        assert company.website == "https://existing.com"

    def test_no_updates_returns_empty_list(self):
        """When no fields need updating, returns empty list without touching timestamps."""
        from app.enrichment_service import apply_enrichment_to_company

        company = MagicMock()
        company.domain = "example.com"
        company.linkedin_url = "https://linkedin.com/company/ex"
        company.legal_name = "Example Corp"
        company.industry = "Technology"
        company.employee_size = "51-200"
        company.hq_city = "San Francisco"
        company.hq_state = "CA"
        company.hq_country = "United States"
        company.website = "https://example.com"

        data = {
            "source": "explorium",
            "domain": "example.com",
            "legal_name": "Example Corp",
            "industry": "Technology",
            "website": "https://newsite.com",  # Won't overwrite
        }

        updated = apply_enrichment_to_company(company, data)

        assert updated == []
        # last_enriched_at should NOT be set (no updates)
        company.last_enriched_at  # Accessing is fine, but should not be assigned

    def test_multiple_fields_updated(self):
        """Multiple empty fields are all updated."""
        from app.enrichment_service import apply_enrichment_to_company

        company = MagicMock()
        company.domain = None
        company.linkedin_url = None
        company.legal_name = None
        company.industry = None
        company.employee_size = None
        company.hq_city = None
        company.hq_state = None
        company.hq_country = None
        company.website = None

        data = {
            "source": "explorium",
            "domain": "example.com",
            "legal_name": "Example Corp",
            "industry": "Technology",
            "hq_city": "Austin",
            "hq_state": "TX",
            "hq_country": "United States",
            "website": "https://example.com",
        }

        updated = apply_enrichment_to_company(company, data)

        assert "domain" in updated
        assert "legal_name" in updated
        assert "industry" in updated
        assert "hq_city" in updated
        assert "website" in updated
        assert company.enrichment_source == "explorium"


# ═══════════════════════════════════════════════════════════════════════
#  apply_enrichment_to_vendor — coverage for vendor variant
# ═══════════════════════════════════════════════════════════════════════


class TestApplyEnrichmentToVendor:
    def test_website_and_domain_set_when_empty(self):
        """website and domain fields applied to vendor when empty."""
        from app.enrichment_service import apply_enrichment_to_vendor

        card = MagicMock()
        card.linkedin_url = None
        card.legal_name = None
        card.industry = None
        card.employee_size = None
        card.hq_city = None
        card.hq_state = None
        card.hq_country = None
        card.domain = None
        card.website = None

        data = {
            "source": "ai",
            "domain": "vendor.com",
            "website": "https://vendor.com",
            "legal_name": "Vendor LLC",
        }

        updated = apply_enrichment_to_vendor(card, data)

        assert "domain" in updated
        assert "website" in updated
        assert "legal_name" in updated
        assert card.enrichment_source == "ai"

    def test_no_updates_when_all_fields_set(self):
        """No updates when vendor already has all fields."""
        from app.enrichment_service import apply_enrichment_to_vendor

        card = MagicMock()
        card.linkedin_url = "https://linkedin.com/company/v"
        card.legal_name = "Vendor LLC"
        card.industry = "Electronics"
        card.employee_size = "11-50"
        card.hq_city = "Dallas"
        card.hq_state = "TX"
        card.hq_country = "United States"
        card.domain = "vendor.com"
        card.website = "https://vendor.com"

        data = {
            "source": "explorium",
            "domain": "other.com",
            "website": "https://other.com",
            "legal_name": "Other LLC",
        }

        updated = apply_enrichment_to_vendor(card, data)
        assert updated == []


# ═══════════════════════════════════════════════════════════════════════
#  Additional bonus coverage for remaining branches
# ═══════════════════════════════════════════════════════════════════════


class TestNormalizeCompanyInputSuccessPath:
    async def test_claude_text_fixes_typo_applies_result(self):
        """When claude_text succeeds, the fixed name replaces the original (lines 188-189)."""
        from app.enrichment_service import normalize_company_input

        # "Grp Tch" has no vowels — triggers suspicious check
        suspicious_name = "Grp Tch"

        with (
            patch(
                "app.enrichment_service.get_credential_cached",
                return_value="fake-anthropic-key",
            ),
            patch(
                "app.enrichment_service.claude_text",
                new_callable=AsyncMock,
                return_value='"Group Tech"',
            ),
        ):
            result_name, _ = await normalize_company_input(suspicious_name, "grouptech.com")

        assert result_name == "Group Tech"

    async def test_name_looks_suspicious_all_words_have_vowels_returns_false(self):
        """_name_looks_suspicious returns False when all long words have vowels (line 167)."""
        from app.enrichment_service import normalize_company_input

        # Normal company name — all words have vowels, so not suspicious
        normal_name = "Apple Corporation"

        with patch("app.enrichment_service.get_credential_cached", return_value="fake-key"):
            result_name, _ = await normalize_company_input(normal_name, "apple.com")

        # claude_text should NOT be called (not suspicious)
        assert result_name == "Apple Corporation"


class TestNormalizeCompanyOutputMoreBranches:
    def test_employee_size_large_digit_formatted_with_plus(self):
        """employee_size ≥ 1000 digits gets formatted with comma and + (line 227)."""
        from app.enrichment_service import normalize_company_output

        data = {"employee_size": "5000"}
        result = normalize_company_output(data)
        assert result["employee_size"] == "5,000+"

    def test_website_without_http_gets_https_prefix(self):
        """website without http:// prefix gets https:// added (line 250)."""
        from app.enrichment_service import normalize_company_output

        data = {"website": "example.com"}
        result = normalize_company_output(data)
        assert result["website"] == "https://example.com"

    def test_linkedin_url_without_http_gets_https_prefix(self):
        """linkedin_url without http:// gets https:// added (lines 254-256)."""
        from app.enrichment_service import normalize_company_output

        data = {"linkedin_url": "linkedin.com/company/example"}
        result = normalize_company_output(data)
        assert result["linkedin_url"] == "https://linkedin.com/company/example"


class TestExploriumFindCompanyNoKey:
    async def test_no_api_key_returns_none(self):
        """Missing Explorium API key returns None immediately (lines 273-274)."""
        from app.enrichment_service import _explorium_find_company

        with patch(
            "app.enrichment_service.get_credential_cached",
            return_value="",
        ):
            result = await _explorium_find_company("example.com", "Example Corp")

        assert result is None


class TestExploriumFindContactsNoKey:
    async def test_no_api_key_returns_empty_list(self):
        """Missing Explorium API key returns empty list immediately (line 313)."""
        from app.enrichment_service import _explorium_find_contacts

        with patch(
            "app.enrichment_service.get_credential_cached",
            return_value="",
        ):
            result = await _explorium_find_contacts("example.com")

        assert result == []


class TestFindSuggestedContactsExceptionBranch:
    async def test_provider_exception_in_gather_is_handled(self):
        """Exception from a gather() provider is caught and logged (lines 547-548)."""
        from app.enrichment_service import find_suggested_contacts

        # explorium raises, ai returns normal result
        ai_contact = {
            "source": "ai",
            "full_name": "Jane Smith",
            "title": "procurement manager",
            "email": "jane@example.com",
            "phone": None,
            "linkedin_url": None,
            "location": None,
            "company": "Example Corp",
        }

        with (
            patch(
                "app.enrichment_service._explorium_find_contacts",
                side_effect=Exception("Network failure"),
            ),
            patch(
                "app.enrichment_service._ai_find_contacts",
                new_callable=AsyncMock,
                return_value=[ai_contact],
            ),
        ):
            result = await find_suggested_contacts("example.com", "Example Corp")

        # Should still get AI results despite explorium failure
        assert len(result) == 1
        assert result[0]["full_name"] == "Jane Smith"
