"""Tests for connector rate-limit and quota handling.

Covers: DigiKey 429 retry, Mouser 403 graceful degradation, OEMSecrets 401
quota exhaustion, BaseConnector 429 handling, per-connector semaphores,
and _parse_retry_after helper.

All external HTTP calls are mocked — no real API requests.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest


def _mock_response(status_code=200, json_data=None, text="", headers=None):
    """Build a fake httpx.Response with optional headers."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    resp.text = text or str(json_data)
    resp.headers = headers or {}
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError("error", request=MagicMock(), response=resp)
    return resp


# ═══════════════════════════════════════════════════════════════════════
#  _parse_retry_after helper
# ═══════════════════════════════════════════════════════════════════════


class TestParseRetryAfter:
    def test_with_numeric_header(self):
        from app.connectors.sources import _parse_retry_after

        resp = _mock_response(429, headers={"Retry-After": "10"})
        assert _parse_retry_after(resp) == 10.0

    def test_with_small_header_clamps_to_1(self):
        from app.connectors.sources import _parse_retry_after

        resp = _mock_response(429, headers={"Retry-After": "0.5"})
        assert _parse_retry_after(resp) == 1.0

    def test_without_header_returns_default(self):
        from app.connectors.sources import _parse_retry_after

        resp = _mock_response(429, headers={})
        result = _parse_retry_after(resp)
        assert 5.0 <= result <= 7.0  # 5 + jitter(0, 2)

    def test_with_non_numeric_header_returns_default(self):
        from app.connectors.sources import _parse_retry_after

        resp = _mock_response(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})
        result = _parse_retry_after(resp)
        assert 5.0 <= result <= 7.0


# ═══════════════════════════════════════════════════════════════════════
#  Per-connector semaphore
# ═══════════════════════════════════════════════════════════════════════


class TestConnectorSemaphore:
    def test_digikey_concurrency_limit(self):
        from app.connectors.sources import _get_connector_semaphore

        sem = _get_connector_semaphore("DigiKeyConnector")
        # DigiKey should be limited to 2 concurrent requests
        assert sem._value == 2

    def test_default_concurrency_limit(self):
        from app.connectors.sources import _get_connector_semaphore

        sem = _get_connector_semaphore("SomeUnknownConnector")
        assert sem._value == 3


# ═══════════════════════════════════════════════════════════════════════
#  DigiKey 429 handling
# ═══════════════════════════════════════════════════════════════════════


class TestDigiKey429:
    def _make_connector(self):
        from app.connectors.digikey import DigiKeyConnector

        c = DigiKeyConnector(client_id="test-id", client_secret="test-secret")
        c._token = "cached-token"
        c._token_expires_at = 9999999999  # far future
        return c

    @pytest.mark.asyncio
    async def test_429_retry_then_success(self):
        """DigiKey retries once on 429, then succeeds."""
        c = self._make_connector()
        rate_limited = _mock_response(429, headers={"Retry-After": "0.01"})
        success = _mock_response(200, json_data={"Products": []})

        with patch("app.connectors.digikey.http") as mock_http:
            mock_http.post = AsyncMock(side_effect=[rate_limited, success])
            results = await c._do_search("LM317T")
            assert results == []
            assert mock_http.post.call_count == 2

    @pytest.mark.asyncio
    async def test_429_twice_returns_empty(self):
        """DigiKey returns empty after two 429s (no raise)."""
        c = self._make_connector()
        rate_limited = _mock_response(429, headers={"Retry-After": "0.01"})

        with patch("app.connectors.digikey.http") as mock_http:
            mock_http.post = AsyncMock(return_value=rate_limited)
            results = await c._do_search("LM317T")
            assert results == []

    @pytest.mark.asyncio
    async def test_token_expiry_refresh(self):
        """DigiKey refreshes expired token before search."""
        from app.connectors.digikey import DigiKeyConnector

        c = DigiKeyConnector(client_id="test-id", client_secret="test-secret")
        c._token = "old-token"
        c._token_expires_at = 0  # expired

        token_resp = _mock_response(200, json_data={"access_token": "new-token", "expires_in": 600})
        search_resp = _mock_response(200, json_data={"Products": []})

        with patch("app.connectors.digikey.http") as mock_http:
            mock_http.post = AsyncMock(side_effect=[token_resp, search_resp])
            results = await c._do_search("LM317T")
            assert results == []
            assert c._token == "new-token"


# ═══════════════════════════════════════════════════════════════════════
#  Mouser 403 handling
# ═══════════════════════════════════════════════════════════════════════


class TestMouser403:
    def _make_connector(self):
        from app.connectors.mouser import MouserConnector

        return MouserConnector(api_key="test-key")

    @pytest.mark.asyncio
    async def test_403_returns_empty(self):
        """Mouser 403 returns empty list, no exception."""
        c = self._make_connector()
        resp_403 = _mock_response(403, text="Forbidden")

        with patch("app.connectors.mouser.http") as mock_http:
            mock_http.post = AsyncMock(return_value=resp_403)
            results = await c._do_search("SN74HC595N")
            assert results == []

    @pytest.mark.asyncio
    async def test_429_returns_empty(self):
        """Mouser 429 returns empty list, no exception."""
        c = self._make_connector()
        resp_429 = _mock_response(429, text="Too Many Requests")

        with patch("app.connectors.mouser.http") as mock_http:
            mock_http.post = AsyncMock(return_value=resp_429)
            results = await c._do_search("SN74HC595N")
            assert results == []

    @pytest.mark.asyncio
    async def test_body_rate_error_returns_empty(self):
        """Mouser rate error in body (HTTP 200) returns empty."""
        c = self._make_connector()
        resp = _mock_response(
            200,
            json_data={
                "Errors": [{"Code": "429", "Message": "Too many requests per second"}],
                "SearchResults": {},
            },
        )

        with patch("app.connectors.mouser.http") as mock_http:
            mock_http.post = AsyncMock(return_value=resp)
            results = await c._do_search("SN74HC595N")
            assert results == []


# ═══════════════════════════════════════════════════════════════════════
#  OEMSecrets 401 handling
# ═══════════════════════════════════════════════════════════════════════


class TestOEMSecrets401:
    def _make_connector(self):
        from app.connectors.oemsecrets import OEMSecretsConnector

        return OEMSecretsConnector(api_key="test-key")

    @pytest.mark.asyncio
    async def test_401_quota_returns_empty(self):
        """OEMSecrets 401 quota exhaustion returns empty, no exception."""
        c = self._make_connector()
        resp_401 = _mock_response(401, text="User is not accepted or has run out of api calls")

        with patch("app.connectors.oemsecrets.http") as mock_http:
            mock_http.get = AsyncMock(return_value=resp_401)
            results = await c._do_search("LM358N")
            assert results == []

    @pytest.mark.asyncio
    async def test_429_returns_empty(self):
        """OEMSecrets 429 returns empty, no exception."""
        c = self._make_connector()
        resp_429 = _mock_response(429, text="Too Many Requests")

        with patch("app.connectors.oemsecrets.http") as mock_http:
            mock_http.get = AsyncMock(return_value=resp_429)
            results = await c._do_search("LM358N")
            assert results == []

    @pytest.mark.asyncio
    async def test_200_still_works(self):
        """OEMSecrets normal 200 response still parsed correctly."""
        c = self._make_connector()
        resp = _mock_response(
            200,
            json_data={
                "stock": [
                    {
                        "distributor": {"distributor_name": "DigiKey"},
                        "source_part_number": "LM358N",
                        "manufacturer": "TI",
                        "quantity_in_stock": 1000,
                        "prices": {"USD": [{"unit_break": 1, "unit_price": 0.50}]},
                        "buy_now_url": "https://digikey.com/p/1",
                    }
                ]
            },
        )

        with patch("app.connectors.oemsecrets.http") as mock_http:
            mock_http.get = AsyncMock(return_value=resp)
            results = await c._do_search("LM358N")
            assert len(results) == 1
            assert results[0]["vendor_name"] == "DigiKey"


# ═══════════════════════════════════════════════════════════════════════
#  BaseConnector 429 retry logic
# ═══════════════════════════════════════════════════════════════════════


class TestBaseConnector429:
    @pytest.mark.asyncio
    async def test_429_retried_with_backoff(self):
        """BaseConnector retries on 429 instead of failing fast."""
        from app.connectors.sources import BaseConnector

        class FakeConnector(BaseConnector):
            call_count = 0

            async def _do_search(self, part_number):
                self.call_count += 1
                if self.call_count <= 2:
                    resp = _mock_response(429, headers={"Retry-After": "0.01"})
                    raise httpx.HTTPStatusError("429", request=MagicMock(), response=resp)
                return [{"result": True}]

        c = FakeConnector(timeout=5.0, max_retries=2)
        results = await c.search("TEST123")
        assert results == [{"result": True}]
        assert c.call_count == 3

    @pytest.mark.asyncio
    async def test_429_exhausted_returns_empty(self):
        """BaseConnector returns empty after all 429 retries exhausted."""
        from app.connectors.sources import BaseConnector

        class FakeConnector(BaseConnector):
            async def _do_search(self, part_number):
                resp = _mock_response(429, headers={"Retry-After": "0.01"})
                raise httpx.HTTPStatusError("429", request=MagicMock(), response=resp)

        c = FakeConnector(timeout=5.0, max_retries=1)
        results = await c.search("TEST123")
        assert results == []
