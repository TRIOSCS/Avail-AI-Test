"""Tests for the Lusha connector (app/connectors/lusha_client.py).

Covers: key gating, company enrichment parsing, the v3 search→enrich contact
flow, email-confidence mapping, and graceful failure on errors/non-200s.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from app.connectors import lusha_client


def _resp(status=200, payload=None, text=""):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload if payload is not None else {}
    r.text = text
    return r


# ── email-confidence label mapping ───────────────────────────────────


class TestEmailConfidenceLabel:
    def test_letter_grades(self):
        assert lusha_client._email_confidence_label("A1") == "high"
        assert lusha_client._email_confidence_label("low") == "low"
        assert lusha_client._email_confidence_label("B") == "medium"

    def test_numeric_percentage(self):
        assert lusha_client._email_confidence_label(90) == "high"
        assert lusha_client._email_confidence_label(60) == "medium"
        assert lusha_client._email_confidence_label(10) == "low"

    def test_none_defaults_medium(self):
        assert lusha_client._email_confidence_label(None) == "medium"


# ── enrich_company ───────────────────────────────────────────────────


class TestEnrichCompany:
    def test_no_key_returns_none(self):
        with patch.object(lusha_client, "get_credential_cached", return_value=None):
            assert asyncio.run(lusha_client.enrich_company("example.com")) is None

    def test_no_domain_returns_none(self):
        with patch.object(lusha_client, "get_credential_cached", return_value="k"):
            assert asyncio.run(lusha_client.enrich_company("")) is None

    def test_success_nested_data(self):
        payload = {
            "data": {
                "name": "Example Corp",
                "mainIndustry": "Electronics",
                "companySize": "201-500",
                "location": {"city": "Austin", "state": "TX", "country": "United States"},
                "website": "https://example.com",
                "social": {"linkedin": "https://linkedin.com/company/example"},
            }
        }
        with patch.object(lusha_client, "get_credential_cached", return_value="k"), \
             patch.object(lusha_client, "http") as mock_http:
            mock_http.get = AsyncMock(return_value=_resp(200, payload))
            result = asyncio.run(lusha_client.enrich_company("example.com"))
        assert result["source"] == "lusha"
        assert result["legal_name"] == "Example Corp"
        assert result["industry"] == "Electronics"
        assert result["hq_city"] == "Austin"
        assert result["hq_country"] == "United States"
        assert result["linkedin_url"].endswith("/example")
        # api_key header (not Bearer)
        assert mock_http.get.call_args.kwargs["headers"]["api_key"] == "k"

    def test_non_200_returns_none(self):
        with patch.object(lusha_client, "get_credential_cached", return_value="k"), \
             patch.object(lusha_client, "http") as mock_http:
            mock_http.get = AsyncMock(return_value=_resp(402, text="Payment Required"))
            assert asyncio.run(lusha_client.enrich_company("example.com")) is None

    def test_exception_returns_none(self):
        with patch.object(lusha_client, "get_credential_cached", return_value="k"), \
             patch.object(lusha_client, "http") as mock_http:
            mock_http.get = AsyncMock(side_effect=Exception("boom"))
            assert asyncio.run(lusha_client.enrich_company("example.com")) is None


# ── search_contacts (search → enrich) ────────────────────────────────


class TestSearchContacts:
    def test_no_key_returns_empty(self):
        with patch.object(lusha_client, "get_credential_cached", return_value=None):
            assert asyncio.run(lusha_client.search_contacts(domain="example.com")) == []

    def test_no_domain_returns_empty(self):
        with patch.object(lusha_client, "get_credential_cached", return_value="k"):
            assert asyncio.run(lusha_client.search_contacts(domain=None)) == []

    def test_full_flow_maps_email_confidence(self):
        search_payload = {"requestId": "req-1", "data": [{"contactId": "c1"}, {"contactId": "c2"}]}
        enrich_payload = {
            "contacts": [
                {
                    "data": {
                        "name": "Jane Doe",
                        "jobTitle": "VP Procurement",
                        "emailAddresses": [{"email": "JANE@example.com", "confidence": "A1"}],
                        "phoneNumbers": [{"number": "+1-555-0100"}],
                        "linkedinUrl": "https://linkedin.com/in/janedoe",
                    }
                }
            ]
        }
        with patch.object(lusha_client, "get_credential_cached", return_value="k"), \
             patch.object(lusha_client, "http") as mock_http:
            mock_http.post = AsyncMock(side_effect=[_resp(200, search_payload), _resp(200, enrich_payload)])
            result = asyncio.run(lusha_client.search_contacts(domain="example.com", limit=5))
        assert len(result) == 1
        c = result[0]
        assert c["source"] == "lusha"
        assert c["full_name"] == "Jane Doe"
        assert c["email"] == "jane@example.com"  # lowercased
        assert c["email_status"] == "high"
        assert c["confidence"] == "high"
        assert c["phone"] == "+1-555-0100"
        # search call uses api_key header + the search endpoint
        search_call = mock_http.post.call_args_list[0]
        assert search_call.args[0].endswith("/prospecting/contact/search")
        assert search_call.kwargs["headers"]["api_key"] == "k"

    def test_search_non_200_returns_empty(self):
        with patch.object(lusha_client, "get_credential_cached", return_value="k"), \
             patch.object(lusha_client, "http") as mock_http:
            mock_http.post = AsyncMock(return_value=_resp(401, text="Unauthorized"))
            assert asyncio.run(lusha_client.search_contacts(domain="example.com")) == []

    def test_no_contact_ids_skips_enrich(self):
        with patch.object(lusha_client, "get_credential_cached", return_value="k"), \
             patch.object(lusha_client, "http") as mock_http:
            mock_http.post = AsyncMock(return_value=_resp(200, {"requestId": "r", "data": []}))
            result = asyncio.run(lusha_client.search_contacts(domain="example.com"))
        assert result == []
        # Only the search call was made (no enrich)
        assert mock_http.post.call_count == 1

    def test_exception_returns_empty(self):
        with patch.object(lusha_client, "get_credential_cached", return_value="k"), \
             patch.object(lusha_client, "http") as mock_http:
            mock_http.post = AsyncMock(side_effect=Exception("network"))
            assert asyncio.run(lusha_client.search_contacts(domain="example.com")) == []
