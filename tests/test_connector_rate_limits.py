"""Tests for connector rate-limit and quota handling.

Covers: DigiKey 429 retry, Mouser 403 graceful degradation, OEMSecrets 401
quota exhaustion, BaseConnector 429 handling, per-connector semaphores,
and _parse_retry_after helper.

All external HTTP calls are mocked — no real API requests.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.connectors.errors import ConnectorAuthError, ConnectorRateLimitError


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
    @pytest.mark.parametrize(
        ("headers", "expected"),
        [
            pytest.param({"Retry-After": "10"}, 10.0, id="numeric_header"),
            pytest.param({"Retry-After": "0.5"}, 1.0, id="small_header_clamps_to_1"),
        ],
    )
    def test_explicit_value(self, headers, expected):
        from app.connectors.sources import _parse_retry_after

        resp = _mock_response(429, headers=headers)
        assert _parse_retry_after(resp) == expected

    @pytest.mark.parametrize(
        "headers",
        [
            pytest.param({}, id="without_header"),
            pytest.param({"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}, id="non_numeric_header"),
        ],
    )
    def test_returns_default_with_jitter(self, headers):
        """Missing or non-numeric header falls back to 5 + jitter(0, 2)."""
        from app.connectors.sources import _parse_retry_after

        resp = _mock_response(429, headers=headers)
        result = _parse_retry_after(resp)
        assert 5.0 <= result <= 7.0


# ═══════════════════════════════════════════════════════════════════════
#  Per-connector semaphore
# ═══════════════════════════════════════════════════════════════════════


class TestConnectorSemaphore:
    @pytest.mark.parametrize(
        ("connector_name", "expected_value"),
        [
            ("DigiKeyConnector", 2),  # DigiKey limited to 2 concurrent requests
            ("SomeUnknownConnector", 3),  # default limit
        ],
    )
    def test_concurrency_limit(self, connector_name, expected_value):
        from app.connectors.sources import _get_connector_semaphore

        sem = _get_connector_semaphore(connector_name)
        assert sem._value == expected_value


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
    async def test_429_twice_raises_for_health_monitor(self):
        """DigiKey raises RuntimeError after persistent 429 so health_monitor flips
        api_sources.status to 'error'; search_service excludes the source from user
        searches; auto-recovers on next successful ping.

        Replaces the prior silent-empty contract per connector convention. See
        docs/APP_MAP_INTERACTIONS.md § Connector Failure Contract.
        """
        c = self._make_connector()
        rate_limited = _mock_response(429, headers={"Retry-After": "0.01"})

        with patch("app.connectors.digikey.http") as mock_http:
            mock_http.post = AsyncMock(return_value=rate_limited)
            with pytest.raises(ConnectorRateLimitError, match="DigiKey rate limited"):
                await c._do_search("LM317T")

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
    """Mouser HTTP-403/429 must raise (not return []).

    Revoked keys also return 403; the prior silent-empty carve-out hid that case. Auto-
    recovery handles transient overload — when upstream returns 200 on the next ping,
    status flips back to 'live' automatically.
    """

    def _make_connector(self):
        from app.connectors.mouser import MouserConnector

        return MouserConnector(api_key="test-key")

    @pytest.mark.asyncio
    async def test_403_raises_auth_error(self):
        """HTTP 403 raises ConnectorAuthError so health_monitor flips status to 'error'.

        Bad/revoked keys, quota-rejected keys, and region-locked keys all surface the
        same operator action.
        """
        from app.connectors.errors import ConnectorAuthError

        c = self._make_connector()
        resp_403 = _mock_response(403, text="Forbidden")

        with patch("app.connectors.mouser.http") as mock_http:
            mock_http.post = AsyncMock(return_value=resp_403)
            with pytest.raises(ConnectorAuthError, match="Mouser auth error"):
                await c._do_search("SN74HC595N")

    @pytest.mark.asyncio
    async def test_429_raises_rate_limit_error(self):
        """HTTP 429 raises ConnectorRateLimitError.

        Auto-recovers on next ping success.
        """
        from app.connectors.errors import ConnectorRateLimitError

        c = self._make_connector()
        resp_429 = _mock_response(429, text="Too Many Requests")

        with patch("app.connectors.mouser.http") as mock_http:
            mock_http.post = AsyncMock(return_value=resp_429)
            with pytest.raises(ConnectorRateLimitError, match="Mouser rate limited"):
                await c._do_search("SN74HC595N")

    @pytest.mark.asyncio
    async def test_body_rate_error_raises_rate_limit(self):
        """Mouser body-level 'too many requests' raises (was return [])."""
        from app.connectors.errors import ConnectorRateLimitError

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
            with pytest.raises(ConnectorRateLimitError, match="Mouser rate"):
                await c._do_search("SN74HC595N")


# ═══════════════════════════════════════════════════════════════════════
#  OEMSecrets 401 handling
# ═══════════════════════════════════════════════════════════════════════


class TestOEMSecrets401:
    def _make_connector(self):
        from app.connectors.oemsecrets import OEMSecretsConnector

        return OEMSecretsConnector(api_key="test-key")

    @pytest.mark.asyncio
    async def test_401_quota_raises_for_health_monitor(self):
        """OEMSecrets 401 (bad key OR quota exhausted) raises RuntimeError so
        health_monitor flips api_sources.status to 'error' and search_service excludes
        from user searches; persistent failures keep flipping back to 'error' on each
        ping until operator rotates the key (or tops up quota), at which point auto-
        recovery on the next 200 ping kicks in.

        Replaces the prior silent-empty contract per connector convention. See
        docs/APP_MAP_INTERACTIONS.md § Connector Failure Contract.
        """
        c = self._make_connector()
        resp_401 = _mock_response(401, text="User is not accepted or has run out of api calls")

        with patch("app.connectors.oemsecrets.http") as mock_http:
            mock_http.get = AsyncMock(return_value=resp_401)
            with pytest.raises(ConnectorAuthError, match="OEMSecrets auth/quota error"):
                await c._do_search("LM358N")

    @pytest.mark.asyncio
    async def test_429_raises_for_health_monitor(self):
        """OEMSecrets 429 raises RuntimeError — same contract as 401."""
        c = self._make_connector()
        resp_429 = _mock_response(429, text="Too Many Requests")

        with patch("app.connectors.oemsecrets.http") as mock_http:
            mock_http.get = AsyncMock(return_value=resp_429)
            with pytest.raises(ConnectorRateLimitError, match="OEMSecrets rate limited"):
                await c._do_search("LM358N")

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
    async def test_429_exhausted_raises_rate_limit_error(self):
        """BaseConnector raises ConnectorRateLimitError after all 429 retries exhausted.

        Replaces the prior silent-empty contract — see docs/APP_MAP_INTERACTIONS.md §
        Connector Failure Contract.
        """
        from app.connectors.errors import ConnectorRateLimitError
        from app.connectors.sources import BaseConnector

        class FakeConnector(BaseConnector):
            async def _do_search(self, part_number):
                resp = _mock_response(429, headers={"Retry-After": "0.01"})
                raise httpx.HTTPStatusError("429", request=MagicMock(), response=resp)

        c = FakeConnector(timeout=5.0, max_retries=1)
        with pytest.raises(ConnectorRateLimitError):
            await c.search("TEST123")


# ═══════════════════════════════════════════════════════════════════════
#  BaseConnector contract — open breaker raises, ConnectorError fast-fails
# ═══════════════════════════════════════════════════════════════════════


class TestBaseConnectorContract:
    """Verify BaseConnector wraps the connector contract correctly.

    The new contract (per docs/APP_MAP_INTERACTIONS.md § Connector Failure Contract):
    open circuit breaker raises ConnectorError, ConnectorError from _do_search bypasses
    retry, persistent httpx 429 raises ConnectorRateLimitError instead of silently
    returning [].
    """

    @pytest.mark.asyncio
    async def test_open_breaker_raises_connector_error(self):
        """When the breaker is open, BaseConnector.search() must raise ConnectorError
        (not return []).

        Returning [] previously masked the contract — health_monitor saw 'success' and
        flipped status back to 'live', defeating the whole fix.
        """
        from app.connectors.errors import ConnectorError
        from app.connectors.sources import BaseConnector

        class FakeConnector(BaseConnector):
            async def _do_search(self, part_number):
                return [{"ok": True}]

        c = FakeConnector(timeout=5.0, max_retries=0)
        # Force the breaker open by recording enough failures
        for _ in range(10):
            c._breaker.record_failure()
        assert c._breaker.current_state == "open"

        with pytest.raises(ConnectorError, match="circuit breaker open"):
            await c.search("TEST123")

    @pytest.mark.asyncio
    async def test_connector_error_in_do_search_bypasses_retry(self):
        """When _do_search raises a ConnectorError, BaseConnector must re-raise
        immediately without retrying.

        ConnectorError signals a hard failure (auth/quota); retrying just burns more
        upstream calls against an already-broken endpoint.
        """
        from app.connectors.errors import ConnectorAuthError
        from app.connectors.sources import BaseConnector

        class FakeConnector(BaseConnector):
            call_count = 0

            async def _do_search(self, part_number):
                self.call_count += 1
                raise ConnectorAuthError("test auth error")

        c = FakeConnector(timeout=5.0, max_retries=2)
        # Breakers are cached globally by class name — reset so prior tests
        # in this class don't leave the breaker open.
        c._breaker.record_success()
        with pytest.raises(ConnectorAuthError):
            await c.search("TEST123")
        # Exactly one attempt — no retry
        assert c.call_count == 1

    @pytest.mark.asyncio
    async def test_httpx_429_exhausted_raises_rate_limit_error(self):
        """When BaseConnector exhausts retries on httpx 429, it must raise
        ConnectorRateLimitError (not return []).

        Returning [] was a pre-existing silent-failure path that contradicts the new
        contract.
        """
        from app.connectors.errors import ConnectorRateLimitError
        from app.connectors.sources import BaseConnector

        class FakeConnector(BaseConnector):
            async def _do_search(self, part_number):
                resp = _mock_response(429, headers={"Retry-After": "0.01"})
                raise httpx.HTTPStatusError("429", request=MagicMock(), response=resp)

        c = FakeConnector(timeout=5.0, max_retries=1)
        # Breakers are cached globally by class name — reset so prior tests
        # in this class don't leave the breaker open.
        c._breaker.record_success()
        with pytest.raises(ConnectorRateLimitError, match="rate limited"):
            await c.search("TEST123")
