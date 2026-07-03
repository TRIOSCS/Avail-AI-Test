"""test_hunter_connector.py — Unit tests for the Hunter.io enrichment connector.

Tests: domain_search happy path, auth error, rate limit, empty response, email_finder.

Called by: pytest
Depends on: app.connectors.hunter.HunterConnector
"""

import os

os.environ.setdefault("TESTING", "1")

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _mock_response(status: int, json_data: dict):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = json_data
    return r


class TestHunterDomainSearch:
    @pytest.mark.asyncio
    async def test_returns_contacts(self):
        from app.connectors.hunter import HunterConnector

        mock_resp = _mock_response(
            200,
            {
                "data": {
                    "domain": "example.com",
                    "emails": [
                        {
                            "value": "alice@example.com",
                            "confidence": 90,
                            "type": "professional",
                            "first_name": "Alice",
                            "last_name": "Smith",
                            "position": "Sales Manager",
                            "linkedin": "",
                            "phone_number": "+1-555-0100",
                        }
                    ],
                }
            },
        )
        with patch("app.connectors.hunter.http") as mock_http:
            mock_http.get = AsyncMock(return_value=mock_resp)
            contacts = await HunterConnector("test-key").domain_search("example.com")

        assert len(contacts) == 1
        assert contacts[0]["email"] == "alice@example.com"
        assert contacts[0]["position"] == "Sales Manager"
        assert contacts[0]["confidence"] == 90
        assert contacts[0]["phone_number"] == "+1-555-0100"

    @pytest.mark.asyncio
    async def test_empty_domain_returns_empty(self):
        from app.connectors.hunter import HunterConnector

        result = await HunterConnector("key").domain_search("")
        assert result == []

    @pytest.mark.asyncio
    async def test_no_key_returns_empty(self):
        from app.connectors.hunter import HunterConnector

        result = await HunterConnector("").domain_search("example.com")
        assert result == []

    @pytest.mark.asyncio
    async def test_401_raises_auth_error(self):
        from app.connectors.errors import ConnectorAuthError
        from app.connectors.hunter import HunterConnector

        mock_resp = _mock_response(401, {})
        with patch("app.connectors.hunter.http") as mock_http:
            mock_http.get = AsyncMock(return_value=mock_resp)
            with pytest.raises(ConnectorAuthError):
                await HunterConnector("bad-key").domain_search("example.com")

    @pytest.mark.asyncio
    async def test_429_raises_quota_error(self):
        from app.connectors.hunter import HunterConnector
        from app.services.enrichment_credit_guard import ProviderQuotaError

        mock_resp = _mock_response(429, {})
        with patch("app.connectors.hunter.http") as mock_http:
            mock_http.get = AsyncMock(return_value=mock_resp)
            with pytest.raises(ProviderQuotaError):
                await HunterConnector("key").domain_search("example.com")

    @pytest.mark.asyncio
    async def test_non_200_returns_empty(self):
        from app.connectors.hunter import HunterConnector

        mock_resp = _mock_response(500, {})
        with patch("app.connectors.hunter.http") as mock_http:
            mock_http.get = AsyncMock(return_value=mock_resp)
            result = await HunterConnector("key").domain_search("example.com")
        assert result == []

    @pytest.mark.asyncio
    async def test_network_error_returns_empty(self):
        from app.connectors.hunter import HunterConnector

        with patch("app.connectors.hunter.http") as mock_http:
            mock_http.get = AsyncMock(side_effect=Exception("timeout"))
            result = await HunterConnector("key").domain_search("example.com")
        assert result == []

    @pytest.mark.asyncio
    async def test_200_non_json_body_returns_empty(self):
        """A 200 with a non-JSON body (r.json() raises ValueError) must not escape into
        the enrichment caller — return the empty shape like the siblings."""
        from app.connectors.hunter import HunterConnector

        mock_resp = _mock_response(200, {})
        mock_resp.json.side_effect = ValueError("No JSON object could be decoded")
        with patch("app.connectors.hunter.http") as mock_http:
            mock_http.get = AsyncMock(return_value=mock_resp)
            result = await HunterConnector("key").domain_search("example.com")
        assert result == []

    @pytest.mark.asyncio
    async def test_skips_entries_without_email(self):
        from app.connectors.hunter import HunterConnector

        mock_resp = _mock_response(
            200,
            {
                "data": {
                    "emails": [
                        {"value": "", "confidence": 50},
                        {
                            "value": "bob@example.com",
                            "confidence": 80,
                            "first_name": "Bob",
                            "last_name": "J",
                            "position": "",
                        },
                    ]
                }
            },
        )
        with patch("app.connectors.hunter.http") as mock_http:
            mock_http.get = AsyncMock(return_value=mock_resp)
            contacts = await HunterConnector("key").domain_search("example.com")

        assert len(contacts) == 1
        assert contacts[0]["email"] == "bob@example.com"


class TestHunterEmailFinder:
    @pytest.mark.asyncio
    async def test_returns_email(self):
        from app.connectors.hunter import HunterConnector

        mock_resp = _mock_response(200, {"data": {"email": "alice@example.com", "score": 92}})
        with patch("app.connectors.hunter.http") as mock_http:
            mock_http.get = AsyncMock(return_value=mock_resp)
            result = await HunterConnector("key").email_finder("example.com", "Alice", "Smith")

        assert result is not None
        assert result["email"] == "alice@example.com"
        assert result["score"] == 92

    @pytest.mark.asyncio
    async def test_missing_params_returns_none(self):
        from app.connectors.hunter import HunterConnector

        assert await HunterConnector("key").email_finder("", "Alice", "Smith") is None
        assert await HunterConnector("key").email_finder("example.com", "", "Smith") is None

    @pytest.mark.asyncio
    async def test_402_raises_quota_error(self):
        from app.connectors.hunter import HunterConnector
        from app.services.enrichment_credit_guard import ProviderQuotaError

        mock_resp = _mock_response(402, {})
        with patch("app.connectors.hunter.http") as mock_http:
            mock_http.get = AsyncMock(return_value=mock_resp)
            with pytest.raises(ProviderQuotaError):
                await HunterConnector("key").email_finder("example.com", "Alice", "Smith")

    @pytest.mark.asyncio
    async def test_429_raises_quota_error(self):
        from app.connectors.hunter import HunterConnector
        from app.services.enrichment_credit_guard import ProviderQuotaError

        mock_resp = _mock_response(429, {})
        with patch("app.connectors.hunter.http") as mock_http:
            mock_http.get = AsyncMock(return_value=mock_resp)
            with pytest.raises(ProviderQuotaError):
                await HunterConnector("key").email_finder("example.com", "Alice", "Smith")


class TestHunterVerify:
    @pytest.mark.asyncio
    async def test_429_raises_quota_error(self):
        from app.connectors.hunter import HunterConnector
        from app.services.enrichment_credit_guard import ProviderQuotaError

        mock_resp = _mock_response(429, {})
        with patch("app.connectors.hunter.http") as mock_http:
            mock_http.get = AsyncMock(return_value=mock_resp)
            with pytest.raises(ProviderQuotaError):
                await HunterConnector("key").verify("alice@example.com")

    @pytest.mark.asyncio
    async def test_402_raises_quota_error(self):
        from app.connectors.hunter import HunterConnector
        from app.services.enrichment_credit_guard import ProviderQuotaError

        mock_resp = _mock_response(402, {})
        with patch("app.connectors.hunter.http") as mock_http:
            mock_http.get = AsyncMock(return_value=mock_resp)
            with pytest.raises(ProviderQuotaError):
                await HunterConnector("key").verify("alice@example.com")


class TestHunterWaterfallIntegration:
    @pytest.mark.asyncio
    async def test_hunter_contacts_included_in_waterfall(self):
        """_hunter_find_contacts normalises contacts to find_suggested_contacts
        format."""
        from unittest.mock import patch as _patch

        with (
            _patch("app.enrichment_service.get_credential_cached", return_value="test-key"),
            _patch("app.connectors.hunter.http") as mock_http,
        ):
            mock_http.get = AsyncMock(
                return_value=_mock_response(
                    200,
                    {
                        "data": {
                            "emails": [
                                {
                                    "value": "sales@vendor.com",
                                    "confidence": 88,
                                    "type": "generic",
                                    "first_name": "",
                                    "last_name": "",
                                    "position": "Sales",
                                    "linkedin": "",
                                    "phone_number": "",
                                }
                            ]
                        }
                    },
                )
            )
            from app.enrichment_service import _hunter_find_contacts

            contacts = await _hunter_find_contacts("vendor.com")

        assert len(contacts) == 1
        c = contacts[0]
        assert c["email"] == "sales@vendor.com"
        assert c["source"] == "hunter"
        assert "full_name" in c
        assert "title" in c


class TestHunterEmailFinderEdgeCases:
    @pytest.mark.asyncio
    async def test_network_error_returns_none(self):
        from app.connectors.hunter import HunterConnector

        with patch("app.connectors.hunter.http") as mock_http:
            mock_http.get = AsyncMock(side_effect=Exception("timeout"))
            result = await HunterConnector("key").email_finder("ex.com", "Alice", "Smith")
        assert result is None

    @pytest.mark.asyncio
    async def test_401_raises_auth_error(self):
        from app.connectors.errors import ConnectorAuthError
        from app.connectors.hunter import HunterConnector

        mock_resp = _mock_response(401, {})
        with patch("app.connectors.hunter.http") as mock_http:
            mock_http.get = AsyncMock(return_value=mock_resp)
            with pytest.raises(ConnectorAuthError):
                await HunterConnector("key").email_finder("ex.com", "Alice", "Smith")

    @pytest.mark.asyncio
    async def test_non_200_returns_none(self):
        from app.connectors.hunter import HunterConnector

        mock_resp = _mock_response(500, {})
        with patch("app.connectors.hunter.http") as mock_http:
            mock_http.get = AsyncMock(return_value=mock_resp)
            result = await HunterConnector("key").email_finder("ex.com", "Alice", "Smith")
        assert result is None

    @pytest.mark.asyncio
    async def test_200_no_email_returns_none(self):
        from app.connectors.hunter import HunterConnector

        mock_resp = _mock_response(200, {"data": {"email": "", "score": 0}})
        with patch("app.connectors.hunter.http") as mock_http:
            mock_http.get = AsyncMock(return_value=mock_resp)
            result = await HunterConnector("key").email_finder("ex.com", "Alice", "Smith")
        assert result is None

    @pytest.mark.asyncio
    async def test_200_non_json_body_returns_none(self):
        from app.connectors.hunter import HunterConnector

        mock_resp = _mock_response(200, {})
        mock_resp.json.side_effect = ValueError("No JSON object could be decoded")
        with patch("app.connectors.hunter.http") as mock_http:
            mock_http.get = AsyncMock(return_value=mock_resp)
            result = await HunterConnector("key").email_finder("ex.com", "Alice", "Smith")
        assert result is None


class TestHunterVerifyEdgeCases:
    @pytest.mark.asyncio
    async def test_empty_key_returns_unknown(self):
        from app.connectors.hunter import HunterConnector

        result = await HunterConnector("").verify("alice@ex.com")
        assert result == {"result": "unknown", "score": 0}

    @pytest.mark.asyncio
    async def test_empty_email_returns_unknown(self):
        from app.connectors.hunter import HunterConnector

        result = await HunterConnector("key").verify("")
        assert result == {"result": "unknown", "score": 0}

    @pytest.mark.asyncio
    async def test_network_error_returns_unknown(self):
        from app.connectors.hunter import HunterConnector

        with patch("app.connectors.hunter.http") as mock_http:
            mock_http.get = AsyncMock(side_effect=Exception("timeout"))
            result = await HunterConnector("key").verify("alice@ex.com")
        assert result == {"result": "unknown", "score": 0}

    @pytest.mark.asyncio
    async def test_401_raises_auth_error(self):
        from app.connectors.errors import ConnectorAuthError
        from app.connectors.hunter import HunterConnector

        mock_resp = _mock_response(401, {})
        with patch("app.connectors.hunter.http") as mock_http:
            mock_http.get = AsyncMock(return_value=mock_resp)
            with pytest.raises(ConnectorAuthError):
                await HunterConnector("key").verify("alice@ex.com")

    @pytest.mark.asyncio
    async def test_non_200_returns_unknown(self):
        from app.connectors.hunter import HunterConnector

        mock_resp = _mock_response(500, {})
        with patch("app.connectors.hunter.http") as mock_http:
            mock_http.get = AsyncMock(return_value=mock_resp)
            result = await HunterConnector("key").verify("alice@ex.com")
        assert result == {"result": "unknown", "score": 0}

    @pytest.mark.asyncio
    async def test_200_success(self):
        from app.connectors.hunter import HunterConnector

        mock_resp = _mock_response(200, {"data": {"result": "deliverable", "score": 95}})
        with patch("app.connectors.hunter.http") as mock_http:
            mock_http.get = AsyncMock(return_value=mock_resp)
            result = await HunterConnector("key").verify("alice@ex.com")
        assert result == {"result": "deliverable", "score": 95}

    @pytest.mark.asyncio
    async def test_200_non_json_body_returns_unknown(self):
        from app.connectors.hunter import HunterConnector

        mock_resp = _mock_response(200, {})
        mock_resp.json.side_effect = ValueError("No JSON object could be decoded")
        with patch("app.connectors.hunter.http") as mock_http:
            mock_http.get = AsyncMock(return_value=mock_resp)
            result = await HunterConnector("key").verify("alice@ex.com")
        assert result == {"result": "unknown", "score": 0}
