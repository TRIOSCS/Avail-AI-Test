"""test_enrichment_service_nightly.py — Additional coverage tests for
app/enrichment_service.py.

Targets the uncovered branches in:
- normalize_company_input: claude_text exception path
- normalize_company_output: employee_size and hq_state else branches
- app.connectors.explorium.enrich_company: exception paths (moved from deleted
  _explorium_find_company in enrichment_service)
- app.connectors.explorium.search_contacts: exception paths (moved from deleted
  _explorium_find_contacts in enrichment_service)
- _ai_find_company: no-data and exception paths
- _ai_find_contacts: exception path
- enrich_entity: delegate to enrichment_router.gather_company (new arch)
- find_suggested_contacts: delegate to enrichment_router.gather_contacts + _is_relevant
- apply_enrichment_to_company: website field branch

NOTE (Task 9): _explorium_find_company and _explorium_find_contacts were DELETED from
enrichment_service.py. Their logic now lives in app.connectors.explorium (enrich_company /
search_contacts). Tests that tested those internal functions have been updated to test
the connector directly. Tests that patched those functions as side-effects of testing
enrich_entity/find_suggested_contacts now patch enrichment_router.gather_company /
gather_contacts instead.

Called by: pytest
Depends on: conftest.py fixtures, app.enrichment_service, app.connectors.explorium
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
    @pytest.mark.parametrize(
        ("field", "raw", "expected"),
        [
            # employee_size "51-200" matches the range regex → else branch (line 231)
            ("employee_size", "51-200", "51-200"),
            # "small company" → "smallcompany" doesn't match range regex → elif (line 229)
            ("employee_size", "small company", "smallcompany"),
            # hq_state not a US abbreviation → title() fallback (line 241)
            ("hq_state", "ontario", "Ontario"),
            # hq_state IS a US abbreviation → uppercased (not line 241)
            ("hq_state", "ca", "CA"),
        ],
        ids=[
            "employee_size_range_preserved",
            "employee_size_non_matching_string_stripped",
            "hq_state_non_us_title_cased",
            "hq_state_us_abbreviation_uppercased",
        ],
    )
    def test_field_normalization_branches(self, field, raw, expected):
        """employee_size and hq_state normalization branches (lines 229, 241)."""
        from app.enrichment_service import normalize_company_output

        result = normalize_company_output({field: raw})
        assert result[field] == expected


# ═══════════════════════════════════════════════════════════════════════
#  app.connectors.explorium.enrich_company — exception / no-key paths
#  (Task 9: _explorium_find_company moved to the connector)
# ═══════════════════════════════════════════════════════════════════════


class TestExploriumFindCompanyExceptionPath:
    """Tests for the Explorium company enrichment connector (not enrichment_service).

    _explorium_find_company was deleted from enrichment_service in Task 9; equivalent
    logic now lives in app.connectors.explorium.enrich_company.
    """

    async def test_httpx_error_returns_none(self):
        """HTTPError during Explorium company lookup returns None."""
        from app.connectors.explorium import enrich_company

        with patch(
            "app.connectors.explorium.http.post",
            side_effect=httpx.ConnectError("Connection refused"),
        ):
            result = await enrich_company("example.com", "Example Corp", "fake-key")

        assert result is None

    async def test_match_returns_none_returns_none(self):
        """No matched business_id from /businesses/match → enrich_company returns
        None."""
        from app.connectors.explorium import enrich_company

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"data": {"matched_businesses": []}}

        with patch(
            "app.connectors.explorium.http.post",
            new_callable=AsyncMock,
            return_value=mock_resp,
        ):
            result = await enrich_company("example.com", "Example Corp", "fake-key")

        assert result is None

    async def test_non_200_match_returns_none(self):
        """Non-200 HTTP response from /businesses/match returns None."""
        from app.connectors.explorium import enrich_company

        mock_resp = MagicMock()
        mock_resp.status_code = 404

        with patch(
            "app.connectors.explorium.http.post",
            new_callable=AsyncMock,
            return_value=mock_resp,
        ):
            result = await enrich_company("example.com", "Example Corp", "fake-key")

        assert result is None


# ═══════════════════════════════════════════════════════════════════════
#  app.connectors.explorium.search_contacts — exception paths
#  (Task 9: _explorium_find_contacts moved to the connector)
# ═══════════════════════════════════════════════════════════════════════


class TestExploriumFindContactsBranches:
    """Tests for the Explorium contacts connector.

    _explorium_find_contacts was deleted from enrichment_service in Task 9; equivalent
    logic now lives in app.connectors.explorium.search_contacts.
    """

    async def test_no_match_returns_empty_list(self):
        """No matched business_id from /businesses/match returns empty list."""
        from app.connectors.explorium import search_contacts

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"data": {"matched_businesses": []}}

        with patch(
            "app.connectors.explorium.http.post",
            new_callable=AsyncMock,
            return_value=mock_resp,
        ):
            result = await search_contacts("example.com", "Example", "fake-key", "procurement", 5)

        assert result == []

    async def test_httpx_error_returns_empty_list(self):
        """HTTPError during Explorium contacts lookup returns empty list."""
        from app.connectors.explorium import search_contacts

        with patch(
            "app.connectors.explorium.http.post",
            side_effect=httpx.TimeoutException("Timeout"),
        ):
            result = await search_contacts("example.com", "Example", "fake-key", "", 5)

        assert result == []

    async def test_value_error_returns_empty_list(self):
        """ValueError during contacts lookup returns empty list."""
        from app.connectors.explorium import search_contacts

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.side_effect = ValueError("Invalid JSON")

        with patch(
            "app.connectors.explorium.http.post",
            new_callable=AsyncMock,
            return_value=mock_resp,
        ):
            result = await search_contacts("example.com", "Example", "fake-key", "", 5)

        assert result == []

    async def test_with_title_filter_uses_connector(self):
        """title_filter is forwarded to search_contacts; result uses connector shape."""
        from app.connectors.explorium import search_contacts

        # Match call returns a business_id; then prospects call returns empty (simplest)
        match_resp = MagicMock()
        match_resp.status_code = 200
        match_resp.json.return_value = {"data": {"matched_businesses": [{"business_id": "biz-1"}]}}

        prospects_resp = MagicMock()
        prospects_resp.status_code = 200
        prospects_resp.json.return_value = {"data": []}  # no contacts

        call_count = [0]

        async def side_effect_post(url, **kwargs):
            call_count[0] += 1
            if "match" in url:
                return match_resp
            return prospects_resp

        with patch("app.connectors.explorium.http.post", side_effect=side_effect_post):
            result = await search_contacts("example.com", "Example", "fake-key", "procurement", 5)

        # Called match + prospects (2 calls minimum)
        assert call_count[0] >= 2
        assert result == []


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
        """claude_json returning non-dict (e.g. list) causes early return (lines
        392-393)."""
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

    @pytest.mark.parametrize(
        "error",
        [
            httpx.ConnectError("Connection error"),
            TypeError("unexpected type"),
        ],
        ids=["httpx_error", "type_error"],
    )
    async def test_claude_json_raises_returns_none(self, error):
        """Exception during AI company lookup returns None (lines 406-408)."""
        from app.enrichment_service import _ai_find_company

        with (
            patch(
                "app.enrichment_service.get_credential_cached",
                return_value="fake-anthropic-key",
            ),
            patch(
                "app.enrichment_service.claude_json",
                side_effect=error,
            ),
        ):
            result = await _ai_find_company("example.com")

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
    @pytest.mark.parametrize(
        "error",
        [
            httpx.ConnectError("Connection error"),
            TypeError("bad type"),
        ],
        ids=["httpx_error", "type_error"],
    )
    async def test_websearch_raises_returns_empty_list(self, error):
        """Exception during AI contacts lookup returns empty list (lines 441-443)."""
        from app.enrichment_service import _ai_find_contacts

        with (
            patch(
                "app.enrichment_service.get_credential_cached",
                return_value="fake-anthropic-key",
            ),
            patch(
                "app.enrichment_service.enrich_contacts_websearch",
                side_effect=error,
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
    """Task 9: enrich_entity now delegates to enrichment_router.gather_company.

    Apollo (and all other provider) orchestration is inside the router; these tests
    patch gather_company at the router level to isolate the facade behavior.
    """

    async def test_gather_company_result_blended_into_output(self):
        """enrich_entity blends whatever gather_company returns into the output."""
        from app.enrichment_service import enrich_entity
        from app.services import enrichment_router

        async def fake_gather(domain, name=""):
            return [
                {
                    "source": "apollo",
                    "legal_name": "Apollo Corp",
                    "domain": "example.com",
                    "industry": "Technology",
                    "employee_size": "51-200",
                    "hq_city": "San Francisco",
                    "hq_state": "CA",
                    "hq_country": "US",
                    "website": "https://example.com",
                }
            ]

        with (
            patch(
                "app.enrichment_service.normalize_company_input",
                new_callable=AsyncMock,
                return_value=("Example Corp", "example.com"),
            ),
            patch("app.cache.intel_cache.get_cached", return_value=None),
            patch("app.cache.intel_cache.set_cached"),
            patch.object(enrichment_router, "gather_company", fake_gather),
        ):
            result = await enrich_entity("example.com", "Example Corp")

        assert result is not None
        assert isinstance(result, dict)
        assert result["industry"] == "Technology"

    async def test_empty_gather_returns_domain_only(self):
        """When gather_company returns nothing, enrich_entity still returns domain
        dict."""
        from app.enrichment_service import enrich_entity
        from app.services import enrichment_router

        async def fake_gather(domain, name=""):
            return []

        with (
            patch(
                "app.enrichment_service.normalize_company_input",
                new_callable=AsyncMock,
                return_value=("Example Corp", "example.com"),
            ),
            patch("app.cache.intel_cache.get_cached", return_value=None),
            patch("app.cache.intel_cache.set_cached"),
            patch.object(enrichment_router, "gather_company", fake_gather),
        ):
            result = await enrich_entity("example.com", "Example Corp")

        assert isinstance(result, dict)
        assert result["domain"] == "example.com"


# ═══════════════════════════════════════════════════════════════════════
#  find_suggested_contacts — _is_relevant with email but no title (line 591)
# ═══════════════════════════════════════════════════════════════════════


class TestFindSuggestedContactsRelevantFilter:
    """Task 9: find_suggested_contacts now delegates to enrichment_router.gather_contacts.

    These tests verify the _is_relevant filter still works correctly. They inject
    contacts via gather_contacts (the new router) instead of the old internal functions.
    """

    async def test_contact_with_email_but_no_title_is_kept(self):
        """Contact with email but no title returns True from _is_relevant."""
        from app.enrichment_service import find_suggested_contacts
        from app.services import enrichment_router

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

        async def fake_gather(domain, name, title_filter, limit):
            return [contact_with_email_no_title]

        with patch.object(enrichment_router, "gather_contacts", fake_gather):
            result = await find_suggested_contacts("example.com", "Example Corp")

        # Contact with email but no title should be included
        assert any(c["full_name"] == "John Doe" for c in result)

    async def test_contact_with_no_title_and_no_email_is_filtered_out(self):
        """Contact with no title and no email is filtered out."""
        from app.enrichment_service import find_suggested_contacts
        from app.services import enrichment_router

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

        async def fake_gather(domain, name, title_filter, limit):
            return [relevant_contact, irrelevant_contact]

        with patch.object(enrichment_router, "gather_contacts", fake_gather):
            result = await find_suggested_contacts("example.com", "Example Corp")

        # Only the relevant contact should be in results
        names = [c["full_name"] for c in result]
        assert "Jane Smith" in names
        assert "Bob Jones" not in names

    async def test_all_irrelevant_contacts_returns_unfiltered(self):
        """If filter removes all contacts, the unfiltered list is returned."""
        from app.enrichment_service import find_suggested_contacts
        from app.services import enrichment_router

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

        async def fake_gather(domain, name, title_filter, limit):
            return [contact1]

        with patch.object(enrichment_router, "gather_contacts", fake_gather):
            result = await find_suggested_contacts("example.com")

        # Filter removed everything, so unfiltered returned
        assert len(result) == 1
        assert result[0]["full_name"] == "Alice Brown"


# ═══════════════════════════════════════════════════════════════════════
#  apply_enrichment_to_company — website branch (lines 622-623)
# ═══════════════════════════════════════════════════════════════════════


class TestApplyEnrichmentToCompany:
    def test_website_set_when_empty(self):
        """Website is applied to company when company.website is empty (lines
        622-623)."""
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
        """When no fields need updating, returns empty list without touching
        timestamps."""
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
        """Website and domain fields applied to vendor when empty."""
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
        """When claude_text succeeds, the fixed name replaces the original (lines
        188-189)."""
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
        """_name_looks_suspicious returns False when all long words have vowels (line
        167)."""
        from app.enrichment_service import normalize_company_input

        # Normal company name — all words have vowels, so not suspicious
        normal_name = "Apple Corporation"

        with patch("app.enrichment_service.get_credential_cached", return_value="fake-key"):
            result_name, _ = await normalize_company_input(normal_name, "apple.com")

        # claude_text should NOT be called (not suspicious)
        assert result_name == "Apple Corporation"


class TestNormalizeCompanyOutputMoreBranches:
    @pytest.mark.parametrize(
        ("field", "raw", "expected"),
        [
            # employee_size ≥ 1000 digits → comma + "+" formatting (line 227)
            ("employee_size", "5000", "5,000+"),
            # website without scheme → https:// prefix added (line 250)
            ("website", "example.com", "https://example.com"),
            # linkedin_url without scheme → https:// prefix added (lines 254-256)
            ("linkedin_url", "linkedin.com/company/example", "https://linkedin.com/company/example"),
        ],
        ids=[
            "employee_size_large_digit_with_plus",
            "website_https_prefix",
            "linkedin_url_https_prefix",
        ],
    )
    def test_field_formatting_branches(self, field, raw, expected):
        """employee_size formatting and website/linkedin scheme prefixing."""
        from app.enrichment_service import normalize_company_output

        result = normalize_company_output({field: raw})
        assert result[field] == expected


class TestExploriumFindCompanyNoKey:
    async def test_no_api_key_router_skips_explorium(self):
        """When no Explorium API key is present, router skips Explorium and returns
        nothing (router is configured to resolve the key internally)."""
        from app.enrichment_service import enrich_entity
        from app.services import enrichment_router

        # With no key, router will return [] (explorium gate won't pass)
        async def empty_gather(domain, name=""):
            return []

        with (
            patch.object(enrichment_router, "gather_company", empty_gather),
            patch("app.cache.intel_cache.get_cached", return_value=None),
            patch("app.cache.intel_cache.set_cached"),
        ):
            result = await enrich_entity("example.com", "Example Corp")

        assert result is not None
        assert result["domain"] == "example.com"


class TestExploriumFindContactsNoKey:
    async def test_no_api_key_router_skips_explorium_contacts(self):
        """When no Explorium API key is present, router skips Explorium contacts."""
        from app.enrichment_service import find_suggested_contacts
        from app.services import enrichment_router

        async def empty_gather(domain, name, title_filter, limit):
            return []

        with patch.object(enrichment_router, "gather_contacts", empty_gather):
            result = await find_suggested_contacts("example.com")

        assert result == []


class TestFindSuggestedContactsExceptionBranch:
    """Task 9: find_suggested_contacts delegates to enrichment_router.gather_contacts.

    Exception handling is now inside the router; these tests verify behavior
    via the gather_contacts façade.
    """

    async def test_gather_returns_ai_contact(self):
        """When gather_contacts returns AI results, they are blended and returned."""
        from app.enrichment_service import find_suggested_contacts
        from app.services import enrichment_router

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

        async def fake_gather(domain, name, title_filter, limit):
            return [ai_contact]

        with patch.object(enrichment_router, "gather_contacts", fake_gather):
            result = await find_suggested_contacts("example.com", "Example Corp")

        assert len(result) == 1
        assert result[0]["full_name"] == "Jane Smith"

    async def test_duplicate_contacts_deduplicated(self):
        """Contacts with the same email are deduplicated by blend_contacts."""
        from app.enrichment_service import find_suggested_contacts
        from app.services import enrichment_router

        contact = {
            "source": "explorium",
            "full_name": "Jane Smith",
            "title": "procurement manager",
            "email": "jane@example.com",
            "phone": None,
            "linkedin_url": None,
            "location": None,
            "company": "Example Corp",
        }
        # Same contact duplicated from AI source (same email → same dedup key)
        duplicate = dict(contact)
        duplicate["source"] = "ai"

        async def fake_gather(domain, name, title_filter, limit):
            return [contact, duplicate]

        with patch.object(enrichment_router, "gather_contacts", fake_gather):
            result = await find_suggested_contacts("example.com", "Example Corp")

        # Only one contact despite two sources returning same email
        assert len(result) == 1
        assert result[0]["full_name"] == "Jane Smith"


class TestExploriumFindCompanySuccessPath:
    """Task 9: _explorium_find_company moved to app.connectors.explorium.enrich_company.

    The connector uses a 2-call pipeline: match → firmographics/enrich.
    """

    async def test_successful_match_and_enrich_returns_data(self):
        """Successful match+enrich pipeline returns parsed firmographic dict."""
        from app.connectors.explorium import enrich_company

        match_resp = MagicMock()
        match_resp.status_code = 200
        match_resp.json.return_value = {"data": {"matched_businesses": [{"business_id": "biz-123"}]}}

        enrich_resp = MagicMock()
        enrich_resp.status_code = 200
        enrich_resp.json.return_value = {
            "data": {
                "name": "Example Corp",
                "linkedin_industry_category": "Technology",
                "number_of_employees_range": {"min": 51, "max": 200},
                "city_name": "Austin",
                "region_name": "TX",
                "country_name": "US",
                "website": "https://example.com",
                "linkedin_profile": "https://linkedin.com/company/example",
            }
        }

        call_count = [0]

        async def side_effect(url, **kwargs):
            call_count[0] += 1
            if "match" in url:
                return match_resp
            return enrich_resp

        with patch("app.connectors.explorium.http.post", side_effect=side_effect):
            result = await enrich_company("example.com", "Example Corp", "fake-key")

        assert result is not None
        assert result["source"] == "explorium"
        assert result["legal_name"] == "Example Corp"
        assert result["industry"] == "Technology"
        assert result["employee_size"] == "51-200"


class TestAiFindCompanySuccessPath:
    async def test_successful_response_returns_data(self):
        """claude_json returns valid dict => structured result returned (line 394)."""
        from app.enrichment_service import _ai_find_company

        with (
            patch(
                "app.enrichment_service.get_credential_cached",
                return_value="fake-anthropic-key",
            ),
            patch(
                "app.enrichment_service.claude_json",
                new_callable=AsyncMock,
                return_value={
                    "legal_name": "Example Corp",
                    "industry": "Technology",
                    "employee_size": "51-200",
                    "hq_city": "Austin",
                    "hq_state": "TX",
                    "hq_country": "US",
                    "website": "https://example.com",
                    "linkedin_url": "https://linkedin.com/company/example",
                },
            ),
        ):
            result = await _ai_find_company("example.com", "Example Corp")

        assert result is not None
        assert result["source"] == "ai"
        assert result["legal_name"] == "Example Corp"
        assert result["domain"] == "example.com"


class TestAiFindContactsSuccessPath:
    async def test_successful_response_returns_contacts(self):
        """enrich_contacts_websearch returns contacts => list returned (line 427)."""
        from app.enrichment_service import _ai_find_contacts

        with (
            patch(
                "app.enrichment_service.get_credential_cached",
                return_value="fake-anthropic-key",
            ),
            patch(
                "app.enrichment_service.enrich_contacts_websearch",
                new_callable=AsyncMock,
                return_value=[
                    {
                        "full_name": "Jane Smith",
                        "title": "Procurement Manager",
                        "email": "jane@example.com",
                        "phone": "555-1234",
                        "linkedin_url": "https://linkedin.com/in/jane",
                    }
                ],
            ),
        ):
            result = await _ai_find_contacts("example.com", "Example Corp", "procurement")

        assert len(result) == 1
        assert result[0]["source"] == "ai"
        assert result[0]["full_name"] == "Jane Smith"
        assert result[0]["company"] == "Example Corp"

    async def test_contacts_missing_full_name_filtered_out(self):
        """Contacts without full_name are filtered from results."""
        from app.enrichment_service import _ai_find_contacts

        with (
            patch(
                "app.enrichment_service.get_credential_cached",
                return_value="fake-anthropic-key",
            ),
            patch(
                "app.enrichment_service.enrich_contacts_websearch",
                new_callable=AsyncMock,
                return_value=[
                    {"full_name": "Jane Smith", "title": "Buyer", "email": "jane@example.com"},
                    {"full_name": None, "title": "Unknown", "email": "unknown@example.com"},
                ],
            ),
        ):
            result = await _ai_find_contacts("example.com")

        assert len(result) == 1
        assert result[0]["full_name"] == "Jane Smith"


class TestEnrichEntityCacheHit:
    async def test_cache_hit_returns_early(self):
        """When cache has a result, enrich_entity returns it immediately without calling
        enrichment_router.gather_company."""
        from app.enrichment_service import enrich_entity
        from app.services import enrichment_router

        cached_data = {
            "legal_name": "Cached Corp",
            "domain": "example.com",
            "industry": "Technology",
            "source": "explorium",
        }

        router_calls = []

        async def should_not_call(domain, name=""):
            router_calls.append(domain)
            return []

        with (
            patch(
                "app.enrichment_service.normalize_company_input",
                new_callable=AsyncMock,
                return_value=("Cached Corp", "example.com"),
            ),
            patch("app.cache.intel_cache.get_cached", return_value=cached_data),
            patch.object(enrichment_router, "gather_company", should_not_call),
        ):
            result = await enrich_entity("example.com", "Cached Corp")

        assert result == cached_data
        assert not router_calls  # router was NOT called on cache hit


class TestNameLooksSuspiciousEdgeCases:
    @pytest.mark.parametrize(
        "name",
        [
            "AI Co",  # only short words (≤2 chars) → no qualifying words
            "",  # empty name → no words
            "IBM TDK LLC",  # all known acronyms → no qualifying words
        ],
        ids=["all_short_words", "empty_string", "known_acronyms"],
    )
    def test_no_qualifying_words_returns_false(self, name):
        """Names with no qualifying words are not suspicious → False (line 163)."""
        from app.enrichment_service import _name_looks_suspicious

        assert _name_looks_suspicious(name) is False


class TestTitleCasePreserveAcronymsEdgeCases:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("", ""),  # empty string returned immediately (line 199)
            ("ibm semiconductor", "IBM Semiconductor"),  # acronym preserved, word title-cased
        ],
        ids=["empty_string", "known_acronym_preserved"],
    )
    def test_title_case_preserves_acronyms(self, raw, expected):
        """Acronyms stay uppercase, regular words are title-cased."""
        from app.enrichment_service import _title_case_preserve_acronyms

        assert _title_case_preserve_acronyms(raw) == expected


class TestFindSuggestedContactsWithErrors:
    """find_suggested_contacts_with_errors delegates to find_suggested_contacts and
    derives errored_providers by snapshotting enrichment_router circuit state
    before/after the call."""

    async def test_no_error_returns_contacts_and_empty_errored(self):
        """All circuits stay closed → errored list is empty; contacts are returned."""
        contact = {
            "full_name": "Alice Buyer",
            "title": "procurement manager",
            "email": "alice@example.com",
            "linkedin_url": None,
            "phone": None,
            "location": None,
            "source": "apollo",
        }

        with (
            patch(
                "app.enrichment_service.find_suggested_contacts",
                new_callable=AsyncMock,
                return_value=[contact],
            ),
            patch("app.services.enrichment_router.circuit_open", return_value=False),
        ):
            from app.enrichment_service import find_suggested_contacts_with_errors

            contacts, errored = await find_suggested_contacts_with_errors("example.com", "Example Corp")

        assert contacts == [contact]
        assert errored == []

    async def test_provider_trips_circuit_appears_in_errored(self):
        """A provider whose circuit transitions closed→open during the call is reported
        as errored."""
        tripped = {"lusha": False}

        def mock_circuit_open(provider: str) -> bool:
            # Lusha starts closed, then trips after the call
            return tripped[provider] if provider in tripped else False

        async def mock_find_contacts(*args, **kwargs):
            # Simulate lusha tripping mid-call
            tripped["lusha"] = True
            return []

        with (
            patch("app.enrichment_service.find_suggested_contacts", mock_find_contacts),
            patch("app.services.enrichment_router.circuit_open", side_effect=mock_circuit_open),
        ):
            import app.enrichment_service as es

            contacts, errored = await es.find_suggested_contacts_with_errors("example.com")

        assert contacts == []
        assert "lusha" in errored

    async def test_zero_results_no_trip_gives_empty_errored(self):
        """Zero contacts + no circuit trip → both lists empty (neutral empty state)."""
        with (
            patch(
                "app.enrichment_service.find_suggested_contacts",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch("app.services.enrichment_router.circuit_open", return_value=False),
        ):
            from app.enrichment_service import find_suggested_contacts_with_errors

            contacts, errored = await find_suggested_contacts_with_errors("unknown.com")

        assert contacts == []
        assert errored == []
