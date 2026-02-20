"""Tests for enrichment normalization layers."""
from unittest.mock import AsyncMock, patch

import pytest

from app.enrichment_service import (
    _clean_domain,
    _name_looks_suspicious,
    _title_case_preserve_acronyms,
    normalize_company_input,
    normalize_company_output,
)

# ── _clean_domain ────────────────────────────────────────────────────────

class TestCleanDomain:
    def test_strips_protocol(self):
        assert _clean_domain("https://example.com") == "example.com"
        assert _clean_domain("http://example.com") == "example.com"

    def test_strips_www(self):
        assert _clean_domain("www.example.com") == "example.com"
        assert _clean_domain("https://www.example.com") == "example.com"

    def test_strips_trailing_slash_and_path(self):
        assert _clean_domain("example.com/about") == "example.com"
        assert _clean_domain("example.com/") == "example.com"

    def test_strips_trailing_dot(self):
        assert _clean_domain("example.com.") == "example.com"

    def test_lowercases(self):
        assert _clean_domain("Example.COM") == "example.com"

    def test_strips_whitespace(self):
        assert _clean_domain("  example.com  ") == "example.com"

    def test_empty(self):
        assert _clean_domain("") == ""


# ── _name_looks_suspicious ───────────────────────────────────────────────

class TestNameLooksSuspicious:
    def test_clean_name(self):
        assert _name_looks_suspicious("International Business Machines") is False

    def test_typo_no_vowels(self):
        assert _name_looks_suspicious("Interntnl Bsns Machines") is True

    def test_acronyms_ignored(self):
        assert _name_looks_suspicious("IBM Corporation") is False

    def test_short_words_ignored(self):
        assert _name_looks_suspicious("TI Inc") is False

    def test_empty(self):
        assert _name_looks_suspicious("") is False


# ── _title_case_preserve_acronyms ────────────────────────────────────────

class TestTitleCasePreserve:
    def test_normal(self):
        assert _title_case_preserve_acronyms("texas instruments") == "Texas Instruments"

    def test_preserves_acronyms(self):
        assert _title_case_preserve_acronyms("ibm corporation") == "IBM Corporation"
        assert _title_case_preserve_acronyms("texas instruments ti") == "Texas Instruments TI"

    def test_empty(self):
        assert _title_case_preserve_acronyms("") == ""

    def test_none(self):
        assert _title_case_preserve_acronyms(None) is None


# ── normalize_company_input ──────────────────────────────────────────────

class TestNormalizeCompanyInput:
    @pytest.mark.asyncio
    async def test_cleans_domain(self):
        name, domain = await normalize_company_input("Acme Corp", "https://www.ACME.com/about")
        assert domain == "acme.com"
        assert name == "Acme Corp"

    @pytest.mark.asyncio
    async def test_empty_domain(self):
        name, domain = await normalize_company_input("Acme Corp", "")
        assert domain == ""

    @pytest.mark.asyncio
    async def test_strips_name_whitespace(self):
        name, domain = await normalize_company_input("  Acme Corp  ", "")
        assert name == "Acme Corp"

    @pytest.mark.asyncio
    @patch("app.enrichment_service.get_credential_cached", return_value="test-key")
    async def test_no_ai_call_for_clean_name(self, mock_cred):
        with patch("app.enrichment_service.claude_text") as mock_claude:
            name, _ = await normalize_company_input("International Business Machines", "")
            mock_claude.assert_not_called()
            assert name == "International Business Machines"

    @pytest.mark.asyncio
    @patch("app.enrichment_service.get_credential_cached", return_value="test-key")
    @patch("app.enrichment_service.claude_text", new_callable=AsyncMock)
    async def test_ai_fixes_typo(self, mock_claude, mock_cred):
        mock_claude.return_value = "International Business Machines"
        # "Ntrntnl" and "Bsnss" have no vowels, triggering the heuristic
        name, _ = await normalize_company_input("Ntrntnl Bsnss Machines", "")
        mock_claude.assert_called_once()
        assert name == "International Business Machines"

    @pytest.mark.asyncio
    @patch("app.enrichment_service.get_credential_cached", return_value="")
    async def test_no_ai_without_api_key(self, mock_cred):
        with patch("app.enrichment_service.claude_text") as mock_claude:
            name, _ = await normalize_company_input("Interntnl Bssness Mchns", "")
            mock_claude.assert_not_called()


# ── normalize_company_output ─────────────────────────────────────────────

class TestNormalizeCompanyOutput:
    def test_full_normalization(self):
        result = normalize_company_output({
            "legal_name": "ibm corporation",
            "domain": "HTTPS://WWW.IBM.COM/",
            "industry": "information technology",
            "employee_size": "350000",
            "hq_city": "armonk",
            "hq_state": "ny",
            "hq_country": "US",
            "website": "ibm.com",
            "linkedin_url": "linkedin.com/company/ibm",
            "source": "clay",
        })
        assert result["legal_name"] == "IBM Corporation"
        assert result["domain"] == "ibm.com"
        assert result["industry"] == "Information Technology"
        assert result["employee_size"] == "350,000+"
        assert result["hq_city"] == "Armonk"
        assert result["hq_state"] == "NY"
        assert result["hq_country"] == "United States"
        assert result["website"] == "https://ibm.com"
        assert result["linkedin_url"] == "https://linkedin.com/company/ibm"

    def test_non_us_state(self):
        result = normalize_company_output({"hq_state": "bavaria", "hq_country": "DE"})
        assert result["hq_state"] == "Bavaria"
        assert result["hq_country"] == "Germany"

    def test_employee_range_preserved(self):
        result = normalize_company_output({"employee_size": "51-200"})
        assert result["employee_size"] == "51-200"

    def test_none_fields_unchanged(self):
        result = normalize_company_output({"legal_name": None, "domain": None})
        assert result["legal_name"] is None
        assert result["domain"] is None

    def test_website_already_has_protocol(self):
        result = normalize_company_output({"website": "https://Example.com"})
        assert result["website"] == "https://example.com"

    def test_source_passthrough(self):
        result = normalize_company_output({"source": "clay+ai"})
        assert result["source"] == "clay+ai"
