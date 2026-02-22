"""
test_enrichment_service.py — Tests for the unified enrichment service.

Covers:
- _clean_domain, _name_looks_suspicious, _title_case_preserve_acronyms
- normalize_company_output, normalize_company_input
- _clay_find_company, _clay_find_contacts
- _explorium_find_company, _explorium_find_contacts
- _gradient_find_company
- _ai_find_company, _ai_find_contacts
- enrich_entity (orchestrator)
- find_suggested_contacts
- apply_enrichment_to_company, apply_enrichment_to_vendor
"""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.enrichment_service import (
    _clean_domain,
    _name_looks_suspicious,
    _title_case_preserve_acronyms,
    normalize_company_output,
)


# ═══════════════════════════════════════════════════════════════════════
# _clean_domain
# ═══════════════════════════════════════════════════════════════════════


class TestCleanDomain:
    def test_strips_protocol_and_www(self):
        assert _clean_domain("https://www.example.com") == "example.com"

    def test_strips_http(self):
        assert _clean_domain("http://example.com") == "example.com"

    def test_strips_trailing_slash(self):
        assert _clean_domain("example.com/") == "example.com"

    def test_strips_path(self):
        assert _clean_domain("https://www.example.com/about") == "example.com"

    def test_strips_trailing_dot(self):
        assert _clean_domain("example.com.") == "example.com"

    def test_lowercases(self):
        assert _clean_domain("EXAMPLE.COM") == "example.com"

    def test_strips_whitespace(self):
        assert _clean_domain("  example.com  ") == "example.com"

    def test_empty_string(self):
        assert _clean_domain("") == ""

    def test_already_clean(self):
        assert _clean_domain("example.com") == "example.com"


# ═══════════════════════════════════════════════════════════════════════
# _name_looks_suspicious
# ═══════════════════════════════════════════════════════════════════════


class TestNameLooksSuspicious:
    def test_normal_name_not_suspicious(self):
        assert _name_looks_suspicious("Arrow Electronics") is False

    def test_all_consonants_suspicious(self):
        assert _name_looks_suspicious("Xyzwrk Corp") is True

    def test_known_acronym_not_suspicious(self):
        assert _name_looks_suspicious("IBM Corporation") is False

    def test_short_word_ignored(self):
        # Words <= 2 chars are skipped in the check
        assert _name_looks_suspicious("TX Corp") is False

    def test_empty_string(self):
        assert _name_looks_suspicious("") is False

    def test_single_acronym(self):
        assert _name_looks_suspicious("AMD") is False


# ═══════════════════════════════════════════════════════════════════════
# _title_case_preserve_acronyms
# ═══════════════════════════════════════════════════════════════════════


class TestTitleCasePreserveAcronyms:
    def test_normal_title_case(self):
        assert _title_case_preserve_acronyms("arrow electronics") == "Arrow Electronics"

    def test_preserves_known_acronyms(self):
        assert _title_case_preserve_acronyms("ibm corporation") == "IBM Corporation"

    def test_multiple_acronyms(self):
        result = _title_case_preserve_acronyms("amd gpu division")
        assert "AMD" in result
        assert "GPU" in result

    def test_empty_string(self):
        assert _title_case_preserve_acronyms("") == ""

    def test_none_returns_none(self):
        assert _title_case_preserve_acronyms(None) is None

    def test_mixed_case_acronym(self):
        assert _title_case_preserve_acronyms("te connectivity") == "TE Connectivity"


# ═══════════════════════════════════════════════════════════════════════
# normalize_company_output
# ═══════════════════════════════════════════════════════════════════════


class TestNormalizeCompanyOutput:
    def test_title_cases_legal_name(self):
        result = normalize_company_output({"legal_name": "arrow electronics"})
        assert result["legal_name"] == "Arrow Electronics"

    def test_cleans_domain(self):
        result = normalize_company_output({"domain": "https://www.arrow.com/"})
        assert result["domain"] == "arrow.com"

    def test_title_cases_industry(self):
        result = normalize_company_output({"industry": "electronic components"})
        assert result["industry"] == "Electronic Components"

    def test_formats_large_employee_size(self):
        result = normalize_company_output({"employee_size": "5000"})
        assert result["employee_size"] == "5,000+"

    def test_keeps_range_employee_size(self):
        result = normalize_company_output({"employee_size": "51-200"})
        assert result["employee_size"] == "51-200"

    def test_uppercases_us_state(self):
        result = normalize_company_output({"hq_state": "ca"})
        assert result["hq_state"] == "CA"

    def test_title_cases_non_us_state(self):
        result = normalize_company_output({"hq_state": "bavaria"})
        assert result["hq_state"] == "Bavaria"

    def test_maps_country_code(self):
        result = normalize_company_output({"hq_country": "US"})
        assert result["hq_country"] == "United States"

    def test_title_cases_unknown_country(self):
        result = normalize_company_output({"hq_country": "estonia"})
        assert result["hq_country"] == "Estonia"

    def test_prefixes_website_with_https(self):
        result = normalize_company_output({"website": "arrow.com"})
        assert result["website"] == "https://arrow.com"

    def test_keeps_existing_https(self):
        result = normalize_company_output({"website": "https://arrow.com"})
        assert result["website"] == "https://arrow.com"

    def test_prefixes_linkedin_with_https(self):
        result = normalize_company_output({"linkedin_url": "linkedin.com/company/arrow"})
        assert result["linkedin_url"] == "https://linkedin.com/company/arrow"

    def test_title_cases_city(self):
        result = normalize_company_output({"hq_city": "san francisco"})
        assert result["hq_city"] == "San Francisco"

    def test_empty_fields_unchanged(self):
        result = normalize_company_output({"legal_name": None, "domain": None})
        assert result["legal_name"] is None
        assert result["domain"] is None

    def test_strips_employee_suffix(self):
        result = normalize_company_output({"employee_size": "500 employees"})
        assert "employees" not in result["employee_size"].lower()


# ═══════════════════════════════════════════════════════════════════════
# normalize_company_input (async)
# ═══════════════════════════════════════════════════════════════════════


class TestNormalizeCompanyInput:
    @pytest.fixture(autouse=True)
    def _no_credentials(self):
        with patch("app.enrichment_service.get_credential_cached", return_value=None):
            yield

    def test_cleans_name_and_domain(self):
        from app.enrichment_service import normalize_company_input
        name, domain = asyncio.get_event_loop().run_until_complete(
            normalize_company_input("  Arrow Electronics  ", "https://www.arrow.com/")
        )
        assert name == "Arrow Electronics"
        assert domain == "arrow.com"

    def test_empty_name(self):
        from app.enrichment_service import normalize_company_input
        name, domain = asyncio.get_event_loop().run_until_complete(
            normalize_company_input("", "example.com")
        )
        assert name == ""
        assert domain == "example.com"

    def test_no_domain(self):
        from app.enrichment_service import normalize_company_input
        name, domain = asyncio.get_event_loop().run_until_complete(
            normalize_company_input("Arrow", "")
        )
        assert name == "Arrow"
        assert domain == ""

    def test_suspicious_name_with_api_key(self):
        """When API key is present and name looks suspicious, Claude is called."""
        from app.enrichment_service import normalize_company_input
        with patch("app.enrichment_service.get_credential_cached", return_value="sk-test"):
            with patch("app.enrichment_service.claude_text", new_callable=AsyncMock, return_value="Fixed Name"):
                name, domain = asyncio.get_event_loop().run_until_complete(
                    normalize_company_input("Xyzwrk Corp", "example.com")
                )
                assert name == "Fixed Name"

    def test_suspicious_name_no_api_key(self):
        """Without API key, suspicious name passes through unchanged."""
        from app.enrichment_service import normalize_company_input
        name, domain = asyncio.get_event_loop().run_until_complete(
            normalize_company_input("Xyzwrk Corp", "example.com")
        )
        assert name == "Xyzwrk Corp"


# ═══════════════════════════════════════════════════════════════════════
# Provider: Clay
# ═══════════════════════════════════════════════════════════════════════


class TestClayFindCompany:
    def test_no_api_key_returns_none(self):
        from app.enrichment_service import _clay_find_company
        with patch("app.enrichment_service.get_credential_cached", return_value=None):
            result = asyncio.get_event_loop().run_until_complete(_clay_find_company("example.com"))
            assert result is None

    def test_success(self):
        from app.enrichment_service import _clay_find_company
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "name": "Example Corp",
            "industry": "Electronics",
            "size": "100-500",
            "locality": "Austin, TX",
            "country": "US",
            "website": "https://example.com",
        }
        with patch("app.enrichment_service.get_credential_cached", return_value="clay-key"):
            with patch("app.enrichment_service.http") as mock_http:
                mock_http.post = AsyncMock(return_value=mock_resp)
                result = asyncio.get_event_loop().run_until_complete(
                    _clay_find_company("example.com")
                )
                assert result["source"] == "clay"
                assert result["legal_name"] == "Example Corp"
                assert result["industry"] == "Electronics"
                assert result["hq_city"] == "Austin"
                assert result["hq_state"] == "TX"

    def test_api_error_returns_none(self):
        from app.enrichment_service import _clay_find_company
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Internal Server Error"
        with patch("app.enrichment_service.get_credential_cached", return_value="clay-key"):
            with patch("app.enrichment_service.http") as mock_http:
                mock_http.post = AsyncMock(return_value=mock_resp)
                result = asyncio.get_event_loop().run_until_complete(
                    _clay_find_company("example.com")
                )
                assert result is None

    def test_exception_returns_none(self):
        from app.enrichment_service import _clay_find_company
        with patch("app.enrichment_service.get_credential_cached", return_value="clay-key"):
            with patch("app.enrichment_service.http") as mock_http:
                mock_http.post = AsyncMock(side_effect=Exception("timeout"))
                result = asyncio.get_event_loop().run_until_complete(
                    _clay_find_company("example.com")
                )
                assert result is None


class TestClayFindContacts:
    def test_no_api_key_returns_empty(self):
        from app.enrichment_service import _clay_find_contacts
        with patch("app.enrichment_service.get_credential_cached", return_value=None):
            result = asyncio.get_event_loop().run_until_complete(
                _clay_find_contacts("example.com")
            )
            assert result == []

    def test_success(self):
        from app.enrichment_service import _clay_find_contacts
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "people": [
                {"name": "Jane Doe", "title": "VP Sales", "email": "jane@example.com"},
                {"name": "John Smith", "title": "Buyer", "email": "john@example.com"},
            ]
        }
        with patch("app.enrichment_service.get_credential_cached", return_value="clay-key"):
            with patch("app.enrichment_service.http") as mock_http:
                mock_http.post = AsyncMock(return_value=mock_resp)
                result = asyncio.get_event_loop().run_until_complete(
                    _clay_find_contacts("example.com")
                )
                assert len(result) == 2
                assert result[0]["full_name"] == "Jane Doe"
                assert result[0]["source"] == "clay"

    def test_filters_nameless_contacts(self):
        from app.enrichment_service import _clay_find_contacts
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "people": [
                {"name": "Jane Doe", "email": "jane@example.com"},
                {"email": "nope@example.com"},  # no name
            ]
        }
        with patch("app.enrichment_service.get_credential_cached", return_value="clay-key"):
            with patch("app.enrichment_service.http") as mock_http:
                mock_http.post = AsyncMock(return_value=mock_resp)
                result = asyncio.get_event_loop().run_until_complete(
                    _clay_find_contacts("example.com")
                )
                assert len(result) == 1


# ═══════════════════════════════════════════════════════════════════════
# Provider: Explorium
# ═══════════════════════════════════════════════════════════════════════


class TestExploriumFindCompany:
    def test_no_api_key_returns_none(self):
        from app.enrichment_service import _explorium_find_company
        with patch("app.enrichment_service.get_credential_cached", return_value=None):
            result = asyncio.get_event_loop().run_until_complete(
                _explorium_find_company("example.com")
            )
            assert result is None

    def test_success_strips_firmo_prefix(self):
        from app.enrichment_service import _explorium_find_company
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "firmo_name": "Example Corp",
            "firmo_linkedin_industry_category": "Semiconductors",
            "firmo_number_of_employees_range": "50-100",
            "firmo_city_name": "Dallas",
            "firmo_region_name": "TX",
            "firmo_country_name": "US",
            "firmo_website": "https://example.com",
        }
        with patch("app.enrichment_service.get_credential_cached", return_value="exp-key"):
            with patch("app.enrichment_service.http") as mock_http:
                mock_http.post = AsyncMock(return_value=mock_resp)
                result = asyncio.get_event_loop().run_until_complete(
                    _explorium_find_company("example.com")
                )
                assert result["source"] == "explorium"
                assert result["legal_name"] == "Example Corp"
                assert result["industry"] == "Semiconductors"


class TestExploriumFindContacts:
    def test_no_api_key_returns_empty(self):
        from app.enrichment_service import _explorium_find_contacts
        with patch("app.enrichment_service.get_credential_cached", return_value=None):
            result = asyncio.get_event_loop().run_until_complete(
                _explorium_find_contacts("example.com")
            )
            assert result == []

    def test_success(self):
        from app.enrichment_service import _explorium_find_contacts
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "prospects": [
                {"full_name": "Alice", "job_title": "Sales Director", "email": "alice@example.com"},
            ]
        }
        with patch("app.enrichment_service.get_credential_cached", return_value="exp-key"):
            with patch("app.enrichment_service.http") as mock_http:
                mock_http.post = AsyncMock(return_value=mock_resp)
                result = asyncio.get_event_loop().run_until_complete(
                    _explorium_find_contacts("example.com")
                )
                assert len(result) == 1
                assert result[0]["source"] == "explorium"
                assert result[0]["full_name"] == "Alice"


# ═══════════════════════════════════════════════════════════════════════
# Provider: Gradient AI
# ═══════════════════════════════════════════════════════════════════════


class TestGradientFindCompany:
    def test_no_api_key_returns_none(self):
        from app.enrichment_service import _gradient_find_company
        with patch("app.config.settings", SimpleNamespace(do_gradient_api_key="")):
            result = asyncio.get_event_loop().run_until_complete(
                _gradient_find_company("example.com")
            )
            assert result is None

    def test_success(self):
        from app.enrichment_service import _gradient_find_company
        mock_settings = SimpleNamespace(do_gradient_api_key="grad-key")
        with patch("app.config.settings", mock_settings):
            with patch(
                "app.services.gradient_service.gradient_json",
                new_callable=AsyncMock,
                return_value={
                    "legal_name": "Grad Corp",
                    "industry": "Tech",
                    "hq_city": "NYC",
                    "hq_state": "NY",
                    "hq_country": "US",
                },
            ):
                result = asyncio.get_event_loop().run_until_complete(
                    _gradient_find_company("example.com", "Grad Corp")
                )
                assert result["source"] == "gradient"
                assert result["legal_name"] == "Grad Corp"

    def test_exception_returns_none(self):
        from app.enrichment_service import _gradient_find_company
        mock_settings = SimpleNamespace(do_gradient_api_key="grad-key")
        with patch("app.config.settings", mock_settings):
            with patch(
                "app.services.gradient_service.gradient_json",
                new_callable=AsyncMock,
                side_effect=Exception("fail"),
            ):
                result = asyncio.get_event_loop().run_until_complete(
                    _gradient_find_company("example.com")
                )
                assert result is None


# ═══════════════════════════════════════════════════════════════════════
# Provider: AI (Claude + Web Search)
# ═══════════════════════════════════════════════════════════════════════


class TestAiFindCompany:
    def test_no_api_key_returns_none(self):
        from app.enrichment_service import _ai_find_company
        with patch("app.enrichment_service.get_credential_cached", return_value=None):
            result = asyncio.get_event_loop().run_until_complete(
                _ai_find_company("example.com")
            )
            assert result is None

    def test_success(self):
        from app.enrichment_service import _ai_find_company
        with patch("app.enrichment_service.get_credential_cached", return_value="sk-key"):
            with patch(
                "app.enrichment_service.claude_json",
                new_callable=AsyncMock,
                return_value={
                    "legal_name": "AI Corp",
                    "industry": "AI",
                    "hq_city": "SF",
                    "hq_state": "CA",
                    "hq_country": "US",
                    "website": "https://ai.com",
                },
            ):
                result = asyncio.get_event_loop().run_until_complete(
                    _ai_find_company("ai.com", "AI Corp")
                )
                assert result["source"] == "ai"
                assert result["legal_name"] == "AI Corp"

    def test_null_response_returns_none(self):
        from app.enrichment_service import _ai_find_company
        with patch("app.enrichment_service.get_credential_cached", return_value="sk-key"):
            with patch(
                "app.enrichment_service.claude_json",
                new_callable=AsyncMock,
                return_value=None,
            ):
                result = asyncio.get_event_loop().run_until_complete(
                    _ai_find_company("example.com")
                )
                assert result is None


class TestAiFindContacts:
    def test_no_api_key_returns_empty(self):
        from app.enrichment_service import _ai_find_contacts
        with patch("app.enrichment_service.get_credential_cached", return_value=None):
            result = asyncio.get_event_loop().run_until_complete(
                _ai_find_contacts("example.com")
            )
            assert result == []

    def test_success(self):
        from app.enrichment_service import _ai_find_contacts
        with patch("app.enrichment_service.get_credential_cached", return_value="sk-key"):
            with patch(
                "app.enrichment_service.enrich_contacts_websearch",
                new_callable=AsyncMock,
                return_value=[
                    {"full_name": "Bob", "title": "Sales", "email": "bob@ai.com"},
                ],
            ):
                result = asyncio.get_event_loop().run_until_complete(
                    _ai_find_contacts("ai.com", "AI Corp")
                )
                assert len(result) == 1
                assert result[0]["source"] == "ai"
                assert result[0]["full_name"] == "Bob"


# ═══════════════════════════════════════════════════════════════════════
# enrich_entity (orchestrator)
# ═══════════════════════════════════════════════════════════════════════


class TestEnrichEntity:
    @pytest.fixture(autouse=True)
    def _no_cache_no_creds(self):
        with patch("app.enrichment_service.get_credential_cached", return_value=None):
            with patch("app.cache.intel_cache.get_cached", return_value=None):
                with patch("app.cache.intel_cache.set_cached"):
                    yield

    def test_cache_hit(self):
        from app.enrichment_service import enrich_entity
        cached = {"legal_name": "Cached Corp", "domain": "cached.com", "source": "cache"}
        with patch("app.cache.intel_cache.get_cached", return_value=cached):
            with patch("app.enrichment_service.normalize_company_input", new_callable=AsyncMock, return_value=("Cached", "cached.com")):
                result = asyncio.get_event_loop().run_until_complete(
                    enrich_entity("cached.com")
                )
                assert result["legal_name"] == "Cached Corp"

    def test_all_providers_fail_returns_empty_result(self):
        from app.enrichment_service import enrich_entity
        with patch("app.enrichment_service.normalize_company_input", new_callable=AsyncMock, return_value=("Test", "test.com")):
            with patch("app.enrichment_service._ai_find_company", new_callable=AsyncMock, return_value=None):
                result = asyncio.get_event_loop().run_until_complete(
                    enrich_entity("test.com")
                )
                assert result["domain"] == "test.com"
                assert result["legal_name"] is None

    def test_clay_data_merged(self):
        from app.enrichment_service import enrich_entity
        clay_data = {
            "source": "clay",
            "legal_name": "Clay Corp",
            "domain": "clay.com",
            "industry": "Electronics",
            "hq_city": "Austin",
            "hq_state": "TX",
            "hq_country": "US",
        }
        with patch("app.enrichment_service.normalize_company_input", new_callable=AsyncMock, return_value=("Clay", "clay.com")):
            with patch("app.enrichment_service._clay_find_company", new_callable=AsyncMock, return_value=clay_data):
                with patch("app.enrichment_service._ai_find_company", new_callable=AsyncMock, return_value=None):
                    result = asyncio.get_event_loop().run_until_complete(
                        enrich_entity("clay.com")
                    )
                    assert "clay" in result.get("source", "")
                    assert result["legal_name"] == "Clay CORP"


# ═══════════════════════════════════════════════════════════════════════
# find_suggested_contacts
# ═══════════════════════════════════════════════════════════════════════


class TestFindSuggestedContacts:
    @pytest.fixture(autouse=True)
    def _no_creds(self):
        with patch("app.enrichment_service.get_credential_cached", return_value=None):
            yield

    def test_all_providers_no_keys_returns_empty(self):
        from app.enrichment_service import find_suggested_contacts
        result = asyncio.get_event_loop().run_until_complete(
            find_suggested_contacts("example.com")
        )
        assert result == []

    def test_deduplicates_by_email(self):
        from app.enrichment_service import find_suggested_contacts
        contacts = [
            {"full_name": "Jane", "title": "Sales Manager", "email": "jane@example.com", "source": "clay"},
            {"full_name": "Jane Doe", "title": "Sales Manager", "email": "jane@example.com", "source": "explorium"},
        ]
        with patch("app.enrichment_service._clay_find_contacts", new_callable=AsyncMock, return_value=contacts[:1]):
            with patch("app.enrichment_service._explorium_find_contacts", new_callable=AsyncMock, return_value=contacts[1:]):
                result = asyncio.get_event_loop().run_until_complete(
                    find_suggested_contacts("example.com")
                )
                # Should be deduped to 1 contact
                assert len(result) == 1

    def test_filters_irrelevant_titles(self):
        from app.enrichment_service import find_suggested_contacts
        contacts = [
            {"full_name": "Sales VP", "title": "VP Sales", "email": "vp@example.com", "source": "clay"},
            {"full_name": "Janitor", "title": "Facilities Janitor", "email": "janitor@example.com", "source": "clay"},
        ]
        with patch("app.enrichment_service._clay_find_contacts", new_callable=AsyncMock, return_value=contacts):
            result = asyncio.get_event_loop().run_until_complete(
                find_suggested_contacts("example.com")
            )
            # VP Sales is relevant, Janitor is not
            assert len(result) == 1
            assert result[0]["full_name"] == "Sales VP"

    def test_returns_all_if_filter_removes_everything(self):
        from app.enrichment_service import find_suggested_contacts
        contacts = [
            {"full_name": "Receptionist", "title": "Receptionist", "email": "front@example.com", "source": "clay"},
        ]
        with patch("app.enrichment_service._clay_find_contacts", new_callable=AsyncMock, return_value=contacts):
            result = asyncio.get_event_loop().run_until_complete(
                find_suggested_contacts("example.com")
            )
            # Should return unfiltered since filter removed everything
            assert len(result) == 1


# ═══════════════════════════════════════════════════════════════════════
# apply_enrichment_to_company
# ═══════════════════════════════════════════════════════════════════════


class TestApplyEnrichmentToCompany:
    def _make_company(self, **overrides):
        defaults = dict(
            domain=None, linkedin_url=None, legal_name=None,
            industry=None, employee_size=None, hq_city=None,
            hq_state=None, hq_country=None, website=None,
            last_enriched_at=None, enrichment_source=None,
        )
        defaults.update(overrides)
        return SimpleNamespace(**defaults)

    def test_fills_empty_fields(self):
        from app.enrichment_service import apply_enrichment_to_company
        company = self._make_company()
        data = {
            "domain": "example.com",
            "legal_name": "Example Corp",
            "industry": "Electronics",
            "hq_city": "Austin",
            "source": "clay",
        }
        updated = apply_enrichment_to_company(company, data)
        assert "domain" in updated
        assert "legal_name" in updated
        assert "industry" in updated
        assert "hq_city" in updated
        assert company.domain == "example.com"
        assert company.legal_name == "Example Corp"
        assert company.last_enriched_at is not None
        assert company.enrichment_source == "clay"

    def test_does_not_overwrite_existing(self):
        from app.enrichment_service import apply_enrichment_to_company
        company = self._make_company(domain="existing.com", industry="Existing")
        data = {"domain": "new.com", "industry": "New"}
        updated = apply_enrichment_to_company(company, data)
        assert updated == []
        assert company.domain == "existing.com"
        assert company.industry == "Existing"

    def test_website_only_if_empty(self):
        from app.enrichment_service import apply_enrichment_to_company
        company = self._make_company()
        data = {"website": "https://example.com", "source": "clay"}
        updated = apply_enrichment_to_company(company, data)
        assert "website" in updated
        assert company.website == "https://example.com"

    def test_no_update_returns_empty_list(self):
        from app.enrichment_service import apply_enrichment_to_company
        company = self._make_company(
            domain="ex.com", legal_name="Ex", industry="Tech",
            hq_city="NYC", hq_state="NY", hq_country="US",
            employee_size="100", linkedin_url="https://li.com", website="https://ex.com",
        )
        data = {
            "domain": "other.com", "legal_name": "Other",
            "industry": "Other", "website": "https://other.com",
        }
        updated = apply_enrichment_to_company(company, data)
        assert updated == []
        assert company.last_enriched_at is None


# ═══════════════════════════════════════════════════════════════════════
# apply_enrichment_to_vendor
# ═══════════════════════════════════════════════════════════════════════


class TestApplyEnrichmentToVendor:
    def _make_vendor(self, **overrides):
        defaults = dict(
            domain=None, linkedin_url=None, legal_name=None,
            industry=None, employee_size=None, hq_city=None,
            hq_state=None, hq_country=None, website=None,
            last_enriched_at=None, enrichment_source=None,
        )
        defaults.update(overrides)
        return SimpleNamespace(**defaults)

    def test_fills_empty_fields(self):
        from app.enrichment_service import apply_enrichment_to_vendor
        card = self._make_vendor()
        data = {
            "domain": "vendor.com",
            "legal_name": "Vendor Inc",
            "industry": "Distribution",
            "website": "https://vendor.com",
            "source": "explorium",
        }
        updated = apply_enrichment_to_vendor(card, data)
        assert "domain" in updated
        assert "legal_name" in updated
        assert "website" in updated
        assert card.domain == "vendor.com"
        assert card.enrichment_source == "explorium"

    def test_does_not_overwrite_existing(self):
        from app.enrichment_service import apply_enrichment_to_vendor
        card = self._make_vendor(domain="existing.com", website="https://existing.com")
        data = {"domain": "new.com", "website": "https://new.com"}
        updated = apply_enrichment_to_vendor(card, data)
        assert updated == []
        assert card.domain == "existing.com"

    def test_no_changes_returns_empty(self):
        from app.enrichment_service import apply_enrichment_to_vendor
        card = self._make_vendor(
            domain="v.com", linkedin_url="https://li.com", legal_name="V",
            industry="Tech", employee_size="50", hq_city="LA",
            hq_state="CA", hq_country="US", website="https://v.com",
        )
        updated = apply_enrichment_to_vendor(card, {"domain": "other.com"})
        assert updated == []
        assert card.last_enriched_at is None
