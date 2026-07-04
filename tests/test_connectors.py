"""Comprehensive tests for app/connectors/ modules.

Covers: sources (BaseConnector, CircuitBreaker,
NexarConnector, BrokerBinConnector), email_mining, digikey, ebay, mouser,
oemsecrets, sourcengine, element14.

All external HTTP calls are mocked — no real API requests.
"""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.connectors.errors import ConnectorAuthError, ConnectorRateLimitError

# ═══════════════════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════════════════


def _mock_response(status_code=200, json_data=None, text="", headers=None):
    """Build a fake httpx.Response."""
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
#  CircuitBreaker tests
# ═══════════════════════════════════════════════════════════════════════


class TestCircuitBreaker:
    def test_initial_state_closed(self):
        from app.connectors.sources import CircuitBreaker

        cb = CircuitBreaker("test_cb_init")
        assert cb.current_state == "closed"

    def test_stays_closed_below_fail_max(self):
        from app.connectors.sources import CircuitBreaker

        cb = CircuitBreaker("test_cb_below", fail_max=3)
        cb.record_failure()
        cb.record_failure()
        assert cb.current_state == "closed"

    def test_opens_at_fail_max(self):
        from app.connectors.sources import CircuitBreaker

        cb = CircuitBreaker("test_cb_open", fail_max=3)
        for _ in range(3):
            cb.record_failure()
        assert cb.current_state == "open"

    def test_half_open_after_timeout(self):
        from app.connectors.sources import CircuitBreaker

        cb = CircuitBreaker("test_cb_half", fail_max=1, reset_timeout=0.01)
        cb.record_failure()
        assert cb.current_state == "open"
        time.sleep(0.02)
        assert cb.current_state == "half_open"

    def test_success_resets(self):
        from app.connectors.sources import CircuitBreaker

        cb = CircuitBreaker("test_cb_reset", fail_max=2)
        cb.record_failure()
        cb.record_failure()
        assert cb.current_state == "open"
        cb.record_success()
        assert cb.current_state == "closed"

    def test_get_breaker_caches(self):
        from app.connectors.sources import get_breaker

        b1 = get_breaker("test_unique_breaker")
        b2 = get_breaker("test_unique_breaker")
        assert b1 is b2


# ═══════════════════════════════════════════════════════════════════════
#  BaseConnector tests
# ═══════════════════════════════════════════════════════════════════════


class TestBaseConnector:
    def test_abstract_do_search_is_enforced(self):
        from app.connectors.sources import BaseConnector

        class MissingImpl(BaseConnector):
            pass

        with pytest.raises(TypeError):
            MissingImpl()

    @pytest.mark.asyncio
    async def test_search_returns_results(self):
        from app.connectors.sources import BaseConnector, _breakers

        class GoodConnector(BaseConnector):
            async def _do_search(self, pn):
                return [{"mpn": pn}]

        _breakers.pop("GoodConnector", None)
        c = GoodConnector()
        result = await c.search("LM317")
        assert result == [{"mpn": "LM317"}]
        _breakers.pop("GoodConnector", None)

    @pytest.mark.asyncio
    async def test_search_raises_when_breaker_open(self):
        """Open breaker raises ConnectorError so health_monitor flips api_sources.status
        to 'error'.

        Was: return [] which silently
        masked the contract — see docs/APP_MAP_INTERACTIONS.md
        § Connector Failure Contract.
        """
        from app.connectors.errors import ConnectorError
        from app.connectors.sources import BaseConnector, _breakers

        class SkipConnector(BaseConnector):
            async def _do_search(self, pn):
                return [{"mpn": pn}]

        _breakers.pop("SkipConnector", None)
        c = SkipConnector()
        for _ in range(10):
            c._breaker.record_failure()
        assert c._breaker.current_state == "open"
        with pytest.raises(ConnectorError, match="circuit breaker open"):
            await c.search("LM317")
        _breakers.pop("SkipConnector", None)

    @pytest.mark.slow
    @pytest.mark.asyncio
    async def test_search_retries_on_generic_error(self):
        from app.connectors.sources import BaseConnector, _breakers

        call_count = 0

        class FlakyConnector(BaseConnector):
            async def _do_search(self, pn):
                nonlocal call_count
                call_count += 1
                if call_count < 3:
                    raise ValueError("flaky")
                return [{"mpn": pn}]

        _breakers.pop("FlakyConnector", None)
        c = FlakyConnector(max_retries=2)
        result = await c.search("LM317")
        assert result == [{"mpn": "LM317"}]
        assert call_count == 3
        _breakers.pop("FlakyConnector", None)

    @pytest.mark.asyncio
    async def test_search_raises_connect_timeout_immediately(self):
        from app.connectors.sources import BaseConnector, _breakers

        class TimeoutConnector(BaseConnector):
            async def _do_search(self, pn):
                raise httpx.ConnectTimeout("timeout")

        _breakers.pop("TimeoutConnector", None)
        c = TimeoutConnector()
        with pytest.raises(httpx.ConnectTimeout):
            await c.search("LM317")
        _breakers.pop("TimeoutConnector", None)

    @pytest.mark.asyncio
    async def test_search_raises_connect_error_immediately(self):
        from app.connectors.sources import BaseConnector, _breakers

        class ConnErrConnector(BaseConnector):
            async def _do_search(self, pn):
                raise httpx.ConnectError("refused")

        _breakers.pop("ConnErrConnector", None)
        c = ConnErrConnector()
        with pytest.raises(httpx.ConnectError):
            await c.search("LM317")
        _breakers.pop("ConnErrConnector", None)

    @pytest.mark.asyncio
    async def test_search_propagates_after_max_retries(self):
        from app.connectors.sources import BaseConnector, _breakers

        class AlwaysFailConn(BaseConnector):
            async def _do_search(self, pn):
                raise RuntimeError("always fails")

        _breakers.pop("AlwaysFailConn", None)
        c = AlwaysFailConn(max_retries=1)
        with pytest.raises(RuntimeError, match="always fails"):
            await c.search("LM317")
        _breakers.pop("AlwaysFailConn", None)


# ═══════════════════════════════════════════════════════════════════════
#  Cross-search OAuth token cache tests (_get_cached_token / _invalidate_token)
# ═══════════════════════════════════════════════════════════════════════
#
# The process-wide bearer cache (keyed by (connector class, client_id)) is what lets a
# token minted by one search be reused by the next until it expires; the per-key
# asyncio.Lock collapses a cold-cache mint burst into ONE POST. conftest's autouse
# `_clear_connector_token_cache` fixture empties `_token_cache` + `_token_locks` around
# each test, so every test here starts cold. `mint` is always a local async callable — no
# live HTTP.


class TestOAuthTokenCache:
    @pytest.mark.asyncio
    async def test_concurrent_cold_cache_mints_once(self):
        """Single-flight: a cold-cache burst of concurrent calls for one key mints ONCE.

        The per-key lock serializes waiters; the re-check after acquiring it means only the
        first waiter mints and the rest reuse that bearer.
        """
        from app.connectors.sources import _get_cached_token

        key = ("TestConn", "client-burst")
        mint_calls = 0

        async def _mint():
            nonlocal mint_calls
            mint_calls += 1
            await asyncio.sleep(0.02)  # widen the race window so the burst truly overlaps
            return f"token-{mint_calls}", 3600

        results = await asyncio.gather(*(_get_cached_token(key, _mint) for _ in range(25)))

        assert mint_calls == 1  # the lock collapsed the burst into a single mint
        assert set(results) == {"token-1"}  # every waiter got the one minted bearer

    @pytest.mark.asyncio
    async def test_two_instances_share_cached_token(self):
        """Two DIFFERENT connector instances with the same (class, client_id) reuse one
        cached token — the second instance's mint must never run."""
        from app.connectors.sources import NexarConnector, _get_cached_token

        c1 = NexarConnector(client_id="shared-id", client_secret="secret-1")
        c2 = NexarConnector(client_id="shared-id", client_secret="secret-2")
        # The cache key is (class name, client_id) — independent of the instance / secret.
        assert c1._token_cache_key() == c2._token_cache_key()

        mint_calls = 0

        async def _mint():
            nonlocal mint_calls
            mint_calls += 1
            return "shared-token", 3600

        async def _must_not_mint():
            raise AssertionError("second instance re-minted instead of reusing the cache")

        first = await _get_cached_token(c1._token_cache_key(), _mint)
        second = await _get_cached_token(c2._token_cache_key(), _must_not_mint)

        assert first == second == "shared-token"
        assert mint_calls == 1

    @pytest.mark.asyncio
    async def test_expired_entry_remints(self):
        """A cached entry past its expiry (minus safety margin) is re-minted, not
        served."""
        from app.connectors.sources import _get_cached_token, _token_cache

        key = ("TestConn", "client-expired")
        # Seed an already-expired entry: expires_at is in the monotonic past.
        _token_cache[key] = ("stale-token", time.monotonic() - 1.0)

        async def _mint():
            return "fresh-token", 3600

        got = await _get_cached_token(key, _mint)
        assert got == "fresh-token"
        assert _token_cache[key][0] == "fresh-token"  # cache updated with the new bearer

    @pytest.mark.asyncio
    async def test_within_safety_margin_remints(self):
        """An entry inside the safety margin (still 'valid' by raw expiry) re-mints
        early — the margin protects against a token dying mid-request."""
        from app.connectors.sources import _get_cached_token, _token_cache

        key = ("TestConn", "client-margin")
        # expires in 10s but safety_margin defaults to 60s → treated as due for refresh.
        _token_cache[key] = ("soon-stale", time.monotonic() + 10.0)

        async def _mint():
            return "refreshed", 3600

        assert await _get_cached_token(key, _mint) == "refreshed"

    @pytest.mark.asyncio
    async def test_invalidate_token_forces_remint(self):
        """`_invalidate_token` drops the cached bearer so the next call re-mints (the
        401 drop-and-retry path)."""
        from app.connectors.sources import _get_cached_token, _invalidate_token

        key = ("TestConn", "client-invalidate")
        mint_calls = 0

        async def _mint():
            nonlocal mint_calls
            mint_calls += 1
            return f"token-{mint_calls}", 3600

        assert await _get_cached_token(key, _mint) == "token-1"
        assert await _get_cached_token(key, _mint) == "token-1"  # reused, no new mint
        assert mint_calls == 1

        _invalidate_token(key)

        assert await _get_cached_token(key, _mint) == "token-2"  # re-minted after drop
        assert mint_calls == 2


# ═══════════════════════════════════════════════════════════════════════
#  DigiKey Connector tests
# ═══════════════════════════════════════════════════════════════════════


class TestDigiKeyConnector:
    def _make_connector(self):
        from app.connectors.digikey import DigiKeyConnector
        from app.connectors.sources import _token_cache

        c = DigiKeyConnector(client_id="test-id", client_secret="test-secret")
        # Seed the process-wide OAuth cache so `_get_token` skips the mint POST.
        _token_cache[c._token_cache_key()] = ("cached-token", 9999999999.0)
        return c

    def test_parse_products(self):
        c = self._make_connector()
        data = {
            "Products": [
                {
                    "ManufacturerPartNumber": "LM317T",
                    "Manufacturer": {"Name": "Texas Instruments"},
                    "DigiKeyPartNumber": "296-1432-5-ND",
                    "QuantityAvailable": 5000,
                    "StandardPricing": [
                        {"BreakQuantity": 1, "UnitPrice": 0.75},
                        {"BreakQuantity": 100, "UnitPrice": 0.55},
                    ],
                    "ProductUrl": "/product-detail/en/296-1432-5-ND",
                    "Description": {"DetailedDescription": "IC REG LINEAR 1.2V"},
                }
            ]
        }
        results = c._parse(data, "LM317T")
        assert len(results) == 1
        r = results[0]
        assert r["vendor_name"] == "DigiKey"
        assert r["manufacturer"] == "Texas Instruments"
        assert r["unit_price"] == 0.75
        assert r["confidence"] == 5
        assert r["click_url"].startswith("https://www.digikey.com")

    def test_parse_empty_products(self):
        c = self._make_connector()
        results = c._parse({"Products": []}, "XYZ123")
        assert results == []

    def test_parse_no_qty_confidence(self):
        c = self._make_connector()
        data = {
            "Products": [
                {
                    "ManufacturerPartNumber": "NRND-PART",
                    "Manufacturer": {"Name": "AD"},
                    "DigiKeyPartNumber": "AD-001",
                    "QuantityAvailable": 0,
                    "ProductUrl": "https://digikey.com/p/1",
                    "Description": {"DetailedDescription": "Obsolete part"},
                }
            ]
        }
        results = c._parse(data, "NRND-PART")
        assert results[0]["confidence"] == 3

    def test_parse_camelcase_keys(self):
        c = self._make_connector()
        data = {
            "products": [
                {
                    "manufacturerPartNumber": "ABC123",
                    "manufacturer": {"Name": "Vishay"},
                    "digiKeyPartNumber": "V-001",
                    "quantityAvailable": 200,
                    "unitPrice": 1.50,
                    "productUrl": "https://digikey.com/p/2",
                    "description": "Resistor 10K",
                }
            ]
        }
        results = c._parse(data, "ABC123")
        assert len(results) == 1
        assert results[0]["mpn_matched"] == "ABC123"
        assert results[0]["unit_price"] == 1.50

    def test_parse_url_relative(self):
        """Relative URL should get digikey prefix."""
        c = self._make_connector()
        data = {
            "Products": [
                {
                    "ManufacturerPartNumber": "X",
                    "Manufacturer": {"Name": "M"},
                    "DigiKeyPartNumber": "DK1",
                    "QuantityAvailable": 1,
                    "ProductUrl": "/product/123",
                    "Description": {"DetailedDescription": "Test"},
                }
            ]
        }
        results = c._parse(data, "X")
        assert results[0]["click_url"] == "https://www.digikey.com/product/123"

    def test_parse_url_empty(self):
        c = self._make_connector()
        data = {
            "Products": [
                {
                    "ManufacturerPartNumber": "X",
                    "Manufacturer": {"Name": "M"},
                    "DigiKeyPartNumber": "DK1",
                    "QuantityAvailable": 1,
                    "ProductUrl": "",
                    "Description": {"DetailedDescription": "Test"},
                }
            ]
        }
        results = c._parse(data, "X")
        assert results[0]["click_url"] == ""

    @pytest.mark.asyncio
    async def test_empty_client_id_returns_empty(self):
        from app.connectors.digikey import DigiKeyConnector

        c = DigiKeyConnector(client_id="", client_secret="secret")
        results = await c._do_search("LM317T")
        assert results == []

    @pytest.mark.asyncio
    async def test_do_search_401_retry(self):
        c = self._make_connector()
        resp_401 = _mock_response(401, text="Unauthorized")
        resp_401.raise_for_status = MagicMock()  # Don't raise for first 401
        token_resp = _mock_response(200, {"access_token": "new-token", "expires_in": 600})
        search_resp = _mock_response(200, {"Products": []})

        with patch("app.connectors.digikey.http") as mock_http:
            mock_http.post = AsyncMock(side_effect=[resp_401, token_resp, search_resp])
            result = await c._do_search("LM317T")
            assert result == []
            assert mock_http.post.call_count == 3

    def test_parse_no_standard_pricing_uses_unit_price(self):
        c = self._make_connector()
        data = {
            "Products": [
                {
                    "ManufacturerPartNumber": "X",
                    "Manufacturer": {"Name": "M"},
                    "DigiKeyPartNumber": "DK1",
                    "QuantityAvailable": 10,
                    "StandardPricing": [],
                    "UnitPrice": 2.50,
                    "ProductUrl": "https://digikey.com/x",
                    "Description": {"DetailedDescription": "Test"},
                }
            ]
        }
        results = c._parse(data, "X")
        assert results[0]["unit_price"] == 2.50

    @pytest.mark.asyncio
    async def test_do_search_persistent_429_raises_for_health_monitor(self):
        """DigiKey: a 429 that persists across the in-method retry must
        raise RuntimeError so health_monitor.ping_source flips status to
        'error'; search_service excludes the source from user searches;
        auto-recovers on next successful ping."""
        c = self._make_connector()  # _make_connector already seeds a valid cached token
        resp_429 = _mock_response(429, text="Too Many Requests")
        resp_429.raise_for_status = MagicMock()
        # Mock asyncio.sleep so the test doesn't actually wait for Retry-After
        with (
            patch("app.connectors.digikey.http") as mock_http,
            patch("app.connectors.digikey.asyncio.sleep", AsyncMock()),
        ):
            mock_http.post = AsyncMock(side_effect=[resp_429, resp_429])
            with pytest.raises(ConnectorRateLimitError, match="DigiKey rate limited"):
                await c._do_search("LM317T")
            assert mock_http.post.call_count == 2  # initial + one retry


# ═══════════════════════════════════════════════════════════════════════
#  eBay Connector tests
# ═══════════════════════════════════════════════════════════════════════


class TestEbayConnector:
    def _make_connector(self):
        from app.connectors.ebay import EbayConnector
        from app.connectors.sources import _token_cache

        c = EbayConnector(client_id="ebay-id", client_secret="ebay-secret")
        # Seed the process-wide OAuth cache so `_get_token` skips the mint POST.
        _token_cache[c._token_cache_key()] = ("cached-token", time.monotonic() + 3600)
        return c

    def test_parse_items(self):
        c = self._make_connector()
        data = {
            "itemSummaries": [
                {
                    "itemId": "v1|123456|0",
                    "title": "LM317T Voltage Regulator IC",
                    "price": {"value": "2.50", "currency": "USD"},
                    "seller": {"username": "chip_seller_99"},
                    "condition": "New",
                    "itemWebUrl": "https://www.ebay.com/itm/123456",
                    "image": {"imageUrl": "https://img.ebay.com/123.jpg"},
                    "estimatedAvailabilities": [{"estimatedAvailableQuantity": "10"}],
                },
            ]
        }
        results = c._parse(data, "LM317T")
        assert len(results) == 1
        r = results[0]
        assert r["vendor_name"] == "chip_seller_99"
        assert r["source_type"] == "ebay"
        assert r["unit_price"] == 2.50
        assert r["is_authorized"] is False
        assert r["qty_available"] == 10
        assert r["confidence"] == 3

    def test_parse_no_seller_username_skipped(self):
        c = self._make_connector()
        data = {
            "itemSummaries": [
                {"itemId": "1", "seller": {"username": ""}, "title": "X"},
                {"itemId": "2", "seller": {}, "title": "Y"},
            ]
        }
        results = c._parse(data, "LM317T")
        assert results == []

    def test_parse_deduplicates(self):
        c = self._make_connector()
        data = {
            "itemSummaries": [
                {
                    "itemId": "123",
                    "seller": {"username": "seller1"},
                    "title": "X",
                    "price": {"value": "1.0", "currency": "USD"},
                },
                {
                    "itemId": "123",
                    "seller": {"username": "seller1"},
                    "title": "X dup",
                    "price": {"value": "1.0", "currency": "USD"},
                },
            ]
        }
        results = c._parse(data, "LM317T")
        assert len(results) == 1

    def test_parse_empty(self):
        c = self._make_connector()
        assert c._parse({}, "LM317T") == []
        assert c._parse({"itemSummaries": []}, "LM317T") == []

    def test_parse_no_availability(self):
        c = self._make_connector()
        data = {
            "itemSummaries": [
                {
                    "itemId": "1",
                    "title": "LM317",
                    "price": {"value": "3.00", "currency": "USD"},
                    "seller": {"username": "s1"},
                }
            ]
        }
        results = c._parse(data, "LM317")
        assert results[0]["qty_available"] is None
        assert results[0]["confidence"] == 2

    def test_parse_availability_zero_est(self):
        c = self._make_connector()
        data = {
            "itemSummaries": [
                {
                    "itemId": "1",
                    "title": "LM317",
                    "price": {"value": "3.00", "currency": "USD"},
                    "seller": {"username": "s1"},
                    "estimatedAvailabilities": [{"estimatedAvailableQuantity": None}],
                }
            ]
        }
        results = c._parse(data, "LM317")
        assert results[0]["qty_available"] is None

    @pytest.mark.asyncio
    async def test_empty_client_id(self):
        from app.connectors.ebay import EbayConnector

        c = EbayConnector(client_id="", client_secret="secret")
        results = await c._do_search("LM317T")
        assert results == []

    @pytest.mark.asyncio
    async def test_do_search_401_retry(self):
        c = self._make_connector()
        resp_401 = _mock_response(401, text="Unauth")
        resp_401.raise_for_status = MagicMock()
        token_resp = _mock_response(200, {"access_token": "new"})
        search_resp = _mock_response(200, {"itemSummaries": []})

        with patch("app.connectors.ebay.http") as mock_http:
            mock_http.get = AsyncMock(side_effect=[resp_401, search_resp])
            mock_http.post = AsyncMock(return_value=token_resp)
            result = await c._do_search("LM317T")
            assert result == []

    @pytest.mark.asyncio
    async def test_do_search_404_returns_empty(self):
        c = self._make_connector()
        resp_404 = _mock_response(404)
        resp_404.raise_for_status = MagicMock()

        with patch("app.connectors.ebay.http") as mock_http:
            mock_http.get = AsyncMock(return_value=resp_404)
            result = await c._do_search("NONEXIST")
            assert result == []

    @pytest.mark.asyncio
    async def test_do_search_persistent_429_raises_rate_limit_error(self):
        """A persistent 429 surfaces a typed ConnectorRateLimitError (Retry-After
        honored with one inline retry) — not lumped into a generic error (Phase-4
        audit)."""
        c = self._make_connector()
        resp_429 = _mock_response(429, text="Too Many Requests", headers={"Retry-After": "0"})

        with (
            patch("app.connectors.ebay.http") as mock_http,
            patch("app.connectors.ebay.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        ):
            mock_http.get = AsyncMock(side_effect=[resp_429, resp_429])
            with pytest.raises(ConnectorRateLimitError, match="eBay rate limited"):
                await c._do_search("LM317T")
        # Honored Retry-After before the single inline retry.
        mock_sleep.assert_awaited_once()
        assert mock_http.get.await_count == 2

    @pytest.mark.asyncio
    async def test_do_search_429_then_recovers(self):
        """A 429 that clears on the inline retry returns parsed results (no error)."""
        c = self._make_connector()
        resp_429 = _mock_response(429, text="Too Many Requests", headers={"Retry-After": "0"})
        resp_ok = _mock_response(200, {"itemSummaries": []})

        with (
            patch("app.connectors.ebay.http") as mock_http,
            patch("app.connectors.ebay.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_http.get = AsyncMock(side_effect=[resp_429, resp_ok])
            result = await c._do_search("LM317T")
        assert result == []
        assert mock_http.get.await_count == 2


# ═══════════════════════════════════════════════════════════════════════
#  Mouser Connector tests
# ═══════════════════════════════════════════════════════════════════════


class TestMouserConnector:
    def _make_connector(self):
        from app.connectors.mouser import MouserConnector

        return MouserConnector(api_key="test-mouser-key")

    def test_parse_parts(self):
        c = self._make_connector()
        data = {
            "SearchResults": {
                "Parts": [
                    {
                        "ManufacturerPartNumber": "LM317T",
                        "Manufacturer": "Texas Instruments",
                        "MouserPartNumber": "595-LM317T",
                        "Availability": "8,500 In Stock",
                        "PriceBreaks": [
                            {"Quantity": 1, "Price": "$0.89"},
                            {"Quantity": 100, "Price": "$0.62"},
                        ],
                        "ProductDetailUrl": "https://mouser.com/ProductDetail/595-LM317T",
                        "Description": "IC REG LINEAR",
                    }
                ]
            }
        }
        results = c._parse(data, "LM317T")
        assert len(results) == 1
        r = results[0]
        assert r["vendor_name"] == "Mouser"
        assert r["qty_available"] == 8500
        assert r["unit_price"] == 0.89
        assert r["confidence"] == 5

    def test_parse_in_stock_no_number(self):
        c = self._make_connector()
        data = {
            "SearchResults": {
                "Parts": [
                    {
                        "ManufacturerPartNumber": "X",
                        "Manufacturer": "M",
                        "MouserPartNumber": "M-X",
                        "Availability": "In Stock",
                        "PriceBreaks": [],
                        "ProductDetailUrl": "",
                        "Description": "",
                    }
                ]
            }
        }
        results = c._parse(data, "X")
        assert results[0]["qty_available"] == 1  # "In Stock" → 1

    def test_parse_no_stock(self):
        c = self._make_connector()
        data = {
            "SearchResults": {
                "Parts": [
                    {
                        "ManufacturerPartNumber": "OBS",
                        "Manufacturer": "M",
                        "MouserPartNumber": "M-OBS",
                        "Availability": "None",
                        "PriceBreaks": [],
                        "ProductDetailUrl": "",
                        "Description": "",
                    }
                ]
            }
        }
        results = c._parse(data, "OBS")
        assert results[0]["qty_available"] is None
        assert results[0]["confidence"] == 3

    def test_parse_empty(self):
        c = self._make_connector()
        results = c._parse({"SearchResults": {"Parts": []}}, "XYZ")
        assert results == []

    @pytest.mark.asyncio
    async def test_empty_api_key(self):
        from app.connectors.mouser import MouserConnector

        c = MouserConnector(api_key="")
        results = await c._do_search("LM317T")
        assert results == []

    def test_parse_null_search_results(self):
        c = self._make_connector()
        results = c._parse({}, "X")
        assert results == []

    @pytest.mark.parametrize(
        ("status", "text", "exc", "match"),
        [
            # 403 raises ConnectorAuthError (prior silent-empty carve-out hid revoked-key
            # cases); 429 raises ConnectorRateLimitError. Both auto-recover on next ping.
            pytest.param(403, "Forbidden", ConnectorAuthError, "Mouser auth error", id="403_auth"),
            pytest.param(429, "Too Many Requests", ConnectorRateLimitError, "Mouser rate limited", id="429_rate"),
        ],
    )
    @pytest.mark.asyncio
    async def test_do_search_error_status_raises(self, status, text, exc, match):
        c = self._make_connector()
        resp = _mock_response(status, text=text)
        resp.raise_for_status = MagicMock()  # Should not be called
        with patch("app.connectors.mouser.http") as mock_http:
            mock_http.post = AsyncMock(return_value=resp)
            with pytest.raises(exc, match=match):
                await c._do_search("LM317T")

    @pytest.mark.parametrize(
        ("errors", "exc", "match"),
        [
            # Generic non-auth, non-rate API errors still raise so
            # BaseConnector._search_with_retry() records a failure for the circuit breaker.
            pytest.param(
                [{"Message": "Internal server error processing part"}],
                RuntimeError,
                "Mouser API: Internal server error",
                id="generic_error",
            ),
            # 'Invalid unique identifier' (bad/revoked API key) must raise ConnectorAuthError
            # so health_monitor.ping_source flips status to 'error'.
            pytest.param(
                [
                    {
                        "Id": 0,
                        "Code": "Invalid",
                        "Message": "Invalid unique identifier.",
                        "ResourceKey": "InvalidIdentifier",
                        "PropertyName": "API Key",
                    }
                ],
                ConnectorAuthError,
                "Mouser auth error",
                id="auth_invalid_identifier",
            ),
            # A catalog-vocabulary 'Invalid ...' error must NOT be treated as auth — guards
            # the auth matcher against the 'invalid' substring false-positive.
            pytest.param(
                [{"Message": "Invalid part number"}],
                RuntimeError,
                "Mouser API: Invalid part number",
                id="invalid_part_number_not_auth",
            ),
            # Quota/rate error in the Errors array raises ConnectorRateLimitError (was return []).
            pytest.param(
                [{"Message": "Too many requests per day"}],
                ConnectorRateLimitError,
                "Mouser rate",
                id="quota_rate",
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_do_search_errors_in_body(self, errors, exc, match):
        c = self._make_connector()
        resp = _mock_response(200, {"Errors": errors})
        with patch("app.connectors.mouser.http") as mock_http:
            mock_http.post = AsyncMock(return_value=resp)
            with pytest.raises(exc, match=match):
                await c._do_search("LM317T")

    def test_parse_picks_lowest_qty_price_break(self):
        """Price should come from the lowest quantity price break."""
        c = self._make_connector()
        data = {
            "SearchResults": {
                "Parts": [
                    {
                        "ManufacturerPartNumber": "X",
                        "Manufacturer": "M",
                        "MouserPartNumber": "M-X",
                        "Availability": "100 In Stock",
                        "PriceBreaks": [
                            {"Quantity": 100, "Price": "$0.50"},
                            {"Quantity": 1, "Price": "$1.25"},
                            {"Quantity": 1000, "Price": "$0.30"},
                        ],
                        "ProductDetailUrl": "",
                        "Description": "",
                    }
                ]
            }
        }
        results = c._parse(data, "X")
        assert results[0]["unit_price"] == 1.25  # qty=1 break

    def test_parse_no_price_breaks(self):
        """Missing PriceBreaks should result in None price."""
        c = self._make_connector()
        data = {
            "SearchResults": {
                "Parts": [
                    {
                        "ManufacturerPartNumber": "X",
                        "Manufacturer": "M",
                        "MouserPartNumber": "M-X",
                        "Availability": "50 In Stock",
                    }
                ]
            }
        }
        results = c._parse(data, "X")
        assert results[0]["unit_price"] is None


# ═══════════════════════════════════════════════════════════════════════
#  OEMSecrets Connector tests
# ═══════════════════════════════════════════════════════════════════════


class TestOEMSecretsConnector:
    def _make_connector(self):
        from app.connectors.oemsecrets import OEMSecretsConnector

        return OEMSecretsConnector(api_key="test-oem-key")

    def test_parse_distributors_dict_format(self):
        c = self._make_connector()
        data = {
            "stock": [
                {
                    "distributor": {"name": "Arrow"},
                    "manufacturer": "TI",
                    "mpn": "LM317T",
                    "sku": "ARW-LM317",
                    "stock": "10000",
                    "currency": "USD",
                    "moq": "1",
                    "url": "https://arrow.com/buy/LM317T",
                    "datasheet_url": "https://arrow.com/ds.pdf",
                    "authorized": True,
                }
            ]
        }
        results = c._parse(data, "LM317T")
        assert len(results) == 1
        assert results[0]["vendor_name"] == "Arrow"
        assert results[0]["source_type"] == "oemsecrets"
        assert results[0]["qty_available"] == 10000

    def test_parse_string_distributor(self):
        c = self._make_connector()
        data = {
            "stock": [
                {
                    "distributor": "Mouser",
                    "mpn": "LM317T",
                    "stock": "5000",
                    "price": "0.89",
                }
            ]
        }
        results = c._parse(data, "LM317T")
        assert results[0]["vendor_name"] == "Mouser"

    def test_parse_list_format(self):
        """OEMSecrets sometimes returns a raw list."""
        c = self._make_connector()
        data = [
            {
                "distributor_name": "Farnell",
                "part_number": "LM317T",
                "quantity": 200,
                "unit_price": 0.75,
                "buy_url": "https://farnell.com/lm317t",
                "distributor_pn": "F-317",
            }
        ]
        results = c._parse(data, "LM317T")
        assert len(results) == 1
        assert results[0]["vendor_name"] == "Farnell"

    def test_parse_no_distributor_name_skipped(self):
        c = self._make_connector()
        data = {"stock": [{"distributor": {"name": ""}, "mpn": "X"}]}
        results = c._parse(data, "X")
        assert results == []

    def test_parse_deduplicates(self):
        c = self._make_connector()
        data = {
            "stock": [
                {"distributor": "Arrow", "mpn": "LM317T", "sku": "A1", "stock": 100},
                {"distributor": "Arrow", "mpn": "LM317T", "sku": "A1", "stock": 200},
            ]
        }
        results = c._parse(data, "LM317T")
        assert len(results) == 1

    def test_parse_non_dict_items_skipped(self):
        c = self._make_connector()
        data = {"stock": ["not a dict", 42, None]}
        results = c._parse(data, "X")
        assert results == []

    def test_parse_non_list_stock_data(self):
        c = self._make_connector()
        data = {"stock": "invalid"}
        results = c._parse(data, "X")
        assert results == []

    def test_parse_results_key(self):
        c = self._make_connector()
        data = {
            "results": [
                {
                    "distributor": "Avnet",
                    "mpn": "X",
                    "stock": 50,
                    "seller": "Avnet",
                }
            ]
        }
        results = c._parse(data, "X")
        assert len(results) == 1

    def test_parse_empty(self):
        c = self._make_connector()
        assert c._parse({"stock": []}, "XYZ") == []

    @pytest.mark.asyncio
    async def test_empty_api_key(self):
        from app.connectors.oemsecrets import OEMSecretsConnector

        c = OEMSecretsConnector(api_key="")
        results = await c._do_search("LM317T")
        assert results == []

    def test_parse_v3_api_format(self):
        """V3 API uses nested distributor dict with distributor_name, quantity_in_stock,
        prices dict."""
        c = self._make_connector()
        data = {
            "version": "3.0",
            "status": "http 200 OK",
            "parts_returned": 1,
            "stock": [
                {
                    "manufacturer": "Texas Instruments",
                    "moq": 5,
                    "sku": "5338209P",
                    "source_part_number": "LM317T/NOPB",
                    "part_number": "LM317TNOPB",
                    "quantity_in_stock": 7182,
                    "buy_now_url": "https://analytics.oemsecrets.com/buy",
                    "datasheet_url": "https://example.com/ds.pdf",
                    "distributor_authorisation_status": "authorised",
                    "prices": {
                        "USD": [
                            {"unit_break": 5, "unit_price": "2.98"},
                            {"unit_break": 10, "unit_price": "2.67"},
                        ]
                    },
                    "distributor": {
                        "distributor_name": "RS UK",
                        "distributor_region": "Europe",
                    },
                }
            ],
        }
        results = c._parse(data, "LM317T")
        assert len(results) == 1
        r = results[0]
        assert r["vendor_name"] == "RS UK"
        assert r["mpn_matched"] == "LM317T/NOPB"
        assert r["qty_available"] == 7182
        assert r["unit_price"] == 2.98
        assert r["is_authorized"] is True
        assert r["click_url"] == "https://analytics.oemsecrets.com/buy"
        assert r["vendor_sku"] == "5338209P"

    def test_parse_v3_unauthorized(self):
        c = self._make_connector()
        data = {
            "stock": [
                {
                    "distributor": {"distributor_name": "Broker X"},
                    "source_part_number": "ABC",
                    "quantity_in_stock": 100,
                    "distributor_authorisation_status": "independent",
                    "prices": {"USD": [{"unit_break": 1, "unit_price": "1.50"}]},
                }
            ]
        }
        results = c._parse(data, "ABC")
        assert results[0]["is_authorized"] is False

    def test_parse_no_auth_signal_defaults_unauthorized(self):
        """No authorization signal at all → is_authorized must default to False.

        OEMSecrets aggregates 140+ gray-market distributors; an absent signal must not
        overstate trust by defaulting to authorized.
        """
        c = self._make_connector()
        data = {
            "stock": [
                {
                    "distributor": {"distributor_name": "Some Broker"},
                    "source_part_number": "ABC",
                    "quantity_in_stock": 100,
                    "prices": {"USD": [{"unit_break": 1, "unit_price": "1.50"}]},
                }
            ]
        }
        results = c._parse(data, "ABC")
        assert results[0]["is_authorized"] is False

    def test_parse_authorized_signal_sets_true(self):
        """A positive authorized signal → is_authorized True."""
        c = self._make_connector()
        data = {
            "stock": [
                {
                    "distributor": {"distributor_name": "Arrow"},
                    "source_part_number": "ABC",
                    "quantity_in_stock": 100,
                    "distributor_authorisation_status": "authorised",
                }
            ]
        }
        results = c._parse(data, "ABC")
        assert results[0]["is_authorized"] is True

    def test_parse_v3_no_usd_prices(self):
        """Falls back to first available currency if USD not in prices."""
        c = self._make_connector()
        data = {
            "stock": [
                {
                    "distributor": {"distributor_name": "RS UK"},
                    "source_part_number": "X",
                    "quantity_in_stock": 50,
                    "prices": {"GBP": [{"unit_break": 1, "unit_price": "1.20"}]},
                }
            ]
        }
        results = c._parse(data, "X")
        assert results[0]["unit_price"] == 1.20

    @pytest.mark.asyncio
    async def test_do_search_non_200(self):
        c = self._make_connector()
        resp = _mock_response(500, text="Server Error")
        with patch("app.connectors.oemsecrets.http") as mock_http:
            mock_http.get = AsyncMock(return_value=resp)
            with pytest.raises(httpx.HTTPStatusError):
                await c._do_search("LM317T")

    @pytest.mark.asyncio
    async def test_do_search_non_json_response(self):
        c = self._make_connector()
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 200
        resp.json.side_effect = ValueError("not json")
        resp.text = "<html>Error</html>"
        resp.raise_for_status = MagicMock()

        with patch("app.connectors.oemsecrets.http") as mock_http:
            mock_http.get = AsyncMock(return_value=resp)
            result = await c._do_search("LM317T")
            assert result == []

    @pytest.mark.parametrize(
        ("distributor", "moq_in", "moq_out"),
        [
            pytest.param("Broker X", 0, None, id="zero_becomes_none"),  # MOQ=0 must become None for chk_sight_moq
            pytest.param("Arrow", 5, 5, id="positive_kept"),
        ],
    )
    def test_parse_moq(self, distributor, moq_in, moq_out):
        c = self._make_connector()
        data = {
            "stock": [
                {
                    "distributor": distributor,
                    "mpn": "ABC",
                    "stock": 100,
                    "moq": moq_in,
                }
            ]
        }
        results = c._parse(data, "ABC")
        assert results[0]["moq"] == moq_out

    @pytest.mark.parametrize(
        ("status", "text", "exc", "match"),
        [
            # 401 (bad key OR quota exhausted) and 429 (rate limit) must each raise so
            # health_monitor.ping_source flips api_sources.status to 'error', stopping the
            # 15-min ping loop from continuing to consume quota.
            pytest.param(401, "User is not accepted", ConnectorAuthError, "OEMSecrets auth/quota error", id="401_auth"),
            pytest.param(429, "Rate limited", ConnectorRateLimitError, "OEMSecrets rate limited", id="429_rate"),
        ],
    )
    @pytest.mark.asyncio
    async def test_do_search_error_status_raises_for_health_monitor(self, status, text, exc, match):
        c = self._make_connector()
        resp = _mock_response(status, text=text)
        resp.raise_for_status = MagicMock()
        with patch("app.connectors.oemsecrets.http") as mock_http:
            mock_http.get = AsyncMock(return_value=resp)
            with pytest.raises(exc, match=match):
                await c._do_search("LM317T")

    def test_parse_empty_prices_dict(self):
        """Empty prices dict should result in None price."""
        c = self._make_connector()
        data = {
            "stock": [
                {
                    "distributor": "Arrow",
                    "mpn": "X",
                    "stock": 100,
                    "prices": {},
                }
            ]
        }
        results = c._parse(data, "X")
        assert results[0]["unit_price"] is None

    def test_parse_confidence_with_stock(self):
        """Confidence should be 5 when qty is available, 3 when not."""
        c = self._make_connector()
        data = {
            "stock": [
                {"distributor": "A", "mpn": "X", "sku": "s1", "stock": 100},
                {"distributor": "B", "mpn": "Y", "sku": "s2", "stock": 0},
            ]
        }
        results = c._parse(data, "X")
        assert results[0]["confidence"] == 5  # qty=100
        assert results[1]["confidence"] == 3  # qty=0 (falsy)


# ═══════════════════════════════════════════════════════════════════════
#  Sourcengine Connector tests
# ═══════════════════════════════════════════════════════════════════════


class TestSourcengineConnector:
    def _make_connector(self):
        from app.connectors.sourcengine import SourcengineConnector

        return SourcengineConnector(api_key="test-src-key")

    def test_parse_results(self):
        c = self._make_connector()
        data = {
            "results": [
                {
                    "supplier": {"name": "Future Electronics"},
                    "manufacturer": "Texas Instruments",
                    "mpn": "LM317T",
                    "sku": "FUT-LM317T",
                    "quantity": 3000,
                    "unit_price": 0.65,
                    "currency": "USD",
                    "url": "https://sourcengine.com/buy/LM317T",
                    "authorized": True,
                    "moq": 10,
                }
            ]
        }
        results = c._parse(data, "LM317T")
        assert len(results) == 1
        assert results[0]["vendor_name"] == "Future Electronics"
        assert results[0]["source_type"] == "sourcengine"

    def test_parse_string_supplier(self):
        c = self._make_connector()
        data = {"results": [{"supplier": "Arrow", "mpn": "X", "quantity": 100}]}
        results = c._parse(data, "X")
        assert results[0]["vendor_name"] == "Arrow"

    def test_parse_dict_manufacturer(self):
        c = self._make_connector()
        data = {
            "results": [
                {
                    "supplier": {"name": "S1"},
                    "manufacturer": {"name": "TI"},
                    "mpn": "X",
                }
            ]
        }
        results = c._parse(data, "X")
        assert results[0]["manufacturer"] == "TI"

    def test_parse_no_supplier_name_skipped(self):
        c = self._make_connector()
        data = {"results": [{"supplier": {"name": ""}, "mpn": "X"}]}
        results = c._parse(data, "X")
        assert results == []

    def test_parse_supplier_name_fallback(self):
        c = self._make_connector()
        data = {"results": [{"supplier": {}, "supplier_name": "Fallback", "mpn": "X"}]}
        results = c._parse(data, "X")
        assert results[0]["vendor_name"] == "Fallback"

    def test_parse_deduplicates(self):
        c = self._make_connector()
        data = {
            "results": [
                {"supplier": "Arrow", "mpn": "X", "sku": "A1"},
                {"supplier": "Arrow", "mpn": "X", "sku": "A1"},
            ]
        }
        results = c._parse(data, "X")
        assert len(results) == 1

    def test_parse_non_dict_items_skipped(self):
        c = self._make_connector()
        data = {"results": ["not a dict", 42]}
        results = c._parse(data, "X")
        assert results == []

    def test_parse_non_list_offers(self):
        c = self._make_connector()
        data = {"offers": "invalid"}
        results = c._parse(data, "X")
        assert results == []

    def test_parse_data_key(self):
        c = self._make_connector()
        data = {"data": [{"supplier": "S1", "mpn": "X"}]}
        results = c._parse(data, "X")
        assert len(results) == 1

    def test_parse_empty(self):
        c = self._make_connector()
        assert c._parse({"results": []}, "XYZ") == []

    @pytest.mark.asyncio
    async def test_empty_api_key(self):
        from app.connectors.sourcengine import SourcengineConnector

        c = SourcengineConnector(api_key="")
        results = await c._do_search("LM317T")
        assert results == []

    @pytest.mark.parametrize(
        ("moq_in", "moq_out"),
        [
            pytest.param(0, None, id="zero_becomes_none"),  # MOQ=0 must become None to satisfy chk_sight_moq
            pytest.param(10, 10, id="positive_kept"),
        ],
    )
    def test_parse_moq(self, moq_in, moq_out):
        c = self._make_connector()
        data = {
            "results": [
                {
                    "supplier": "Future Electronics",
                    "mpn": "ABC",
                    "quantity": 100,
                    "moq": moq_in,
                }
            ]
        }
        results = c._parse(data, "ABC")
        assert results[0]["moq"] == moq_out

    @pytest.mark.asyncio
    async def test_do_search_non_200_raises(self):
        """Sourcengine raises on non-200 (no graceful handling like Mouser)."""
        c = self._make_connector()
        resp = _mock_response(500, text="Internal Error")
        with patch("app.connectors.sourcengine.http") as mock_http:
            mock_http.get = AsyncMock(return_value=resp)
            with pytest.raises(httpx.HTTPStatusError):
                await c._do_search("LM317T")

    def test_parse_authorized_defaults_false(self):
        """Sourcengine defaults is_authorized to False (unlike OEMSecrets)."""
        c = self._make_connector()
        data = {"results": [{"supplier": "Broker X", "mpn": "X", "quantity": 100}]}
        results = c._parse(data, "X")
        assert results[0]["is_authorized"] is False

    def test_parse_authorized_true_when_set(self):
        c = self._make_connector()
        data = {"results": [{"supplier": "Arrow", "mpn": "X", "quantity": 100, "authorized": True}]}
        results = c._parse(data, "X")
        assert results[0]["is_authorized"] is True

    @pytest.mark.parametrize(
        ("offer", "expected_confidence"),
        [
            # Sourcengine confidence is 4 (not 5) when qty is present
            pytest.param({"supplier": "S1", "mpn": "X", "quantity": 100}, 4, id="4_with_qty"),
            pytest.param({"supplier": "S1", "mpn": "X"}, 3, id="3_without_qty"),
        ],
    )
    def test_parse_confidence(self, offer, expected_confidence):
        c = self._make_connector()
        results = c._parse({"results": [offer]}, "X")
        assert results[0]["confidence"] == expected_confidence

    def test_parse_offers_key(self):
        """Sourcengine should also accept 'offers' as top-level key."""
        c = self._make_connector()
        data = {"offers": [{"supplier": "S1", "mpn": "X"}]}
        results = c._parse(data, "X")
        assert len(results) == 1

    @pytest.mark.parametrize(
        ("status", "text", "exc", "match"),
        [
            # 401/403 (auth) and 429 (rate limit) must each raise so
            # health_monitor.ping_source flips status to 'error' and stops the
            # 15-min ping loop from burning quota against bad creds.
            pytest.param(401, "Unauthorized", ConnectorAuthError, "Sourcengine auth error", id="401_auth"),
            pytest.param(403, "Forbidden", ConnectorAuthError, "Sourcengine auth error", id="403_auth"),
            pytest.param(429, "Too Many Requests", ConnectorRateLimitError, "Sourcengine rate limited", id="429_rate"),
        ],
    )
    @pytest.mark.asyncio
    async def test_search_error_status_raises_for_health_monitor(self, status, text, exc, match):
        c = self._make_connector()
        resp = _mock_response(status, text=text)
        resp.raise_for_status = MagicMock()
        with patch("app.connectors.sourcengine.http") as mock_http:
            mock_http.get = AsyncMock(return_value=resp)
            with pytest.raises(exc, match=match):
                await c._do_search("LM317T")


# ═══════════════════════════════════════════════════════════════════════
#  Element14 Connector tests
# ═══════════════════════════════════════════════════════════════════════


class TestElement14Connector:
    def _make_connector(self):
        from app.connectors.element14 import Element14Connector

        return Element14Connector(api_key="test-e14-key")

    def test_parse_products(self):
        c = self._make_connector()
        data = {
            "manufacturerPartNumberSearchReturn": {
                "products": [
                    {
                        "translatedManufacturerPartNumber": "LM317T",
                        "brandName": "Texas Instruments",
                        "displayName": "Voltage Regulator",
                        "sku": "12345",
                        "stock": {"level": "500"},
                        "prices": [{"cost": "0.65"}, {"cost": "0.55"}],
                    }
                ]
            }
        }
        results = c._parse(data, "LM317T")
        assert len(results) == 1
        r = results[0]
        assert r["vendor_name"] == "element14"
        assert r["mpn_matched"] == "LM317T"
        assert r["qty_available"] == 500
        assert r["unit_price"] == 0.65
        assert r["confidence"] == 5

    def test_parse_no_stock(self):
        c = self._make_connector()
        data = {
            "manufacturerPartNumberSearchReturn": {
                "products": [
                    {
                        "translatedManufacturerPartNumber": "X",
                        "brandName": "M",
                        "displayName": "D",
                        "sku": "S",
                        "stock": {},
                        "prices": [],
                    }
                ]
            }
        }
        results = c._parse(data, "X")
        assert results[0]["qty_available"] is None
        assert results[0]["unit_price"] is None
        assert results[0]["confidence"] == 3

    def test_parse_no_translated_mpn(self):
        c = self._make_connector()
        data = {
            "manufacturerPartNumberSearchReturn": {
                "products": [
                    {
                        "translatedManufacturerPartNumber": None,
                        "brandName": "M",
                        "displayName": "D",
                        "sku": "S",
                    }
                ]
            }
        }
        results = c._parse(data, "FALLBACK")
        assert results[0]["mpn_matched"] == "FALLBACK"

    def test_parse_empty(self):
        c = self._make_connector()
        data = {"manufacturerPartNumberSearchReturn": {"products": []}}
        assert c._parse(data, "X") == []

    def test_parse_missing_container(self):
        c = self._make_connector()
        assert c._parse({}, "X") == []

    @pytest.mark.asyncio
    async def test_empty_api_key(self):
        from app.connectors.element14 import Element14Connector

        c = Element14Connector(api_key="")
        results = await c._do_search("LM317T")
        assert results == []

    @pytest.mark.asyncio
    async def test_exact_miss_returns_empty_no_keyword_fallback(self):
        """A 0-result exact-MPN miss returns [] with NO second keyword-search call.

        The keyword fallback was dropped (Optim #4): it doubled call volume against an
        API that 403s for its per-second QPS cap and returned catalog noise the
        relevance guard discards anyway. Only the exact `manuPartNum:` call runs.
        """
        c = self._make_connector()
        exact_resp = _mock_response(200, json_data={"manufacturerPartNumberSearchReturn": {"products": []}})
        call_count = 0

        async def _mock_get(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return exact_resp

        with patch("app.connectors.element14.http") as mock_http:
            mock_http.get = _mock_get
            results = await c._do_search("LM317T")

        assert results == []
        assert call_count == 1  # exact only — no keyword fallback

    @pytest.mark.asyncio
    async def test_no_fallback_when_exact_matches(self):
        """When exact MPN returns results, no fallback search is made."""
        c = self._make_connector()
        resp = _mock_response(
            200,
            json_data={
                "manufacturerPartNumberSearchReturn": {
                    "products": [
                        {
                            "translatedManufacturerPartNumber": "LM317T",
                            "brandName": "TI",
                            "displayName": "VReg",
                            "sku": "456",
                            "stock": {"level": "500"},
                            "prices": [{"cost": "0.65"}],
                        }
                    ]
                }
            },
        )

        with patch("app.connectors.element14.http") as mock_http:
            mock_http.get = AsyncMock(return_value=resp)
            results = await c._do_search("LM317T")

        assert len(results) == 1
        mock_http.get.assert_awaited_once()  # Only one call — no fallback

    @pytest.mark.parametrize(
        ("status", "text", "exc", "match"),
        [
            # 401 (auth), 403 (key rejected for region/store), and 429 (rate limit) must
            # each raise so health_monitor.ping_source flips status to 'error'. Only the
            # single exact-MPN call runs (the keyword fallback was dropped — Optim #4),
            # so a broken-creds search burns exactly one quota call (call_count == 1).
            pytest.param(401, "Unauthorized", ConnectorAuthError, "element14 auth error", id="401_auth"),
            pytest.param(403, "Forbidden", ConnectorAuthError, "element14 auth error", id="403_auth"),
            pytest.param(429, "Too Many Requests", ConnectorRateLimitError, "element14 rate limited", id="429_rate"),
        ],
    )
    @pytest.mark.asyncio
    async def test_search_error_status_raises_for_health_monitor(self, status, text, exc, match):
        c = self._make_connector()
        resp = _mock_response(status, text=text)
        resp.raise_for_status = MagicMock()
        with patch("app.connectors.element14.http") as mock_http:
            mock_http.get = AsyncMock(return_value=resp)
            with pytest.raises(exc, match=match):
                await c._do_search("LM317T")
            # Only the exact-MPN call ran — there is no keyword fallback
            assert mock_http.get.call_count == 1


# ═══════════════════════════════════════════════════════════════════════
#  BrokerBin Connector tests
# ═══════════════════════════════════════════════════════════════════════


class TestBrokerBinConnector:
    def _make_connector(self):
        from app.connectors.sources import BrokerBinConnector

        return BrokerBinConnector(api_key="bb-token", api_secret="triomhk")

    @pytest.mark.asyncio
    async def test_search_returns_empty_when_token_missing(self):
        """Without the bearer token, no request can be made."""
        from app.connectors.sources import BrokerBinConnector

        c = BrokerBinConnector(api_key="", api_secret="triomhk")
        with patch("app.http_client.http_redirect") as mock_client:
            mock_client.get = AsyncMock()
            results = await c._do_search("LM317T")
            assert results == []
            mock_client.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_search_200_with_results(self):
        c = self._make_connector()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "meta": {"total": 2},
            "data": [
                {
                    "company": "Chip Warehouse",
                    "mfg": "TI",
                    "part": "LM317T",
                    "qty": "5000",
                    "price": "0.55",
                    "cond": "New",
                    "description": "IC Regulator",
                    "country": "US",
                    "age_in_days": "3",
                    "phone": "+15551234567",
                    "email": "sales@chipwarehouse.com",
                },
                {
                    "company": "Excess Stock Co",
                    "mfg": "TI",
                    "part": "LM317T",
                    "qty": "2000",
                    "price": "",
                    "cond": "Used",
                    "country": "HK",
                    "age_in_days": "45",
                },
            ],
        }

        with patch("app.http_client.http_redirect") as mock_client:
            mock_client.get = AsyncMock(return_value=mock_resp)
            results = await c._do_search("LM317T")

        assert len(results) == 2
        assert results[0]["vendor_name"] == "Chip Warehouse"
        assert results[0]["confidence"] == 5  # qty + price
        assert results[0]["source_type"] == "brokerbin"
        assert results[0]["vendor_phone"] == "+15551234567"
        assert results[0]["vendor_email"] == "sales@chipwarehouse.com"
        assert results[1]["confidence"] == 4  # qty but no price

    @pytest.mark.asyncio
    async def test_search_non_200(self):
        c = self._make_connector()
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Server Error"

        with patch("app.http_client.http_redirect") as mock_client:
            mock_client.get = AsyncMock(return_value=mock_resp)
            results = await c._do_search("LM317T")
            assert results == []

    @pytest.mark.parametrize(
        ("status", "text", "match"),
        [
            # Rate-limit (429) and auth (401/403) responses must raise so health_monitor
            # flips status. Regression: BrokerBin used to silently swallow these as
            # `return []`, leaving status 'live' with no operator signal — the silent-failure
            # mode the contract is designed to eliminate.
            pytest.param(
                429,
                '{"error":"Too many requests! Request limit reached..."}',
                "rate limited",
                id="429_rate",
            ),
            pytest.param(
                401,
                '{"message":"Unauthenticated. Must use secure protocol."}',
                "auth error",
                id="401_auth",
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_search_error_status_raises_for_health_monitor(self, status, text, match):
        c = self._make_connector()
        mock_resp = MagicMock()
        mock_resp.status_code = status
        mock_resp.text = text

        with patch("app.http_client.http_redirect") as mock_client:
            mock_client.get = AsyncMock(return_value=mock_resp)
            with pytest.raises(RuntimeError, match=match):
                await c._do_search("LM317T")

    @pytest.mark.asyncio
    async def test_search_non_json(self):
        c = self._make_connector()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.side_effect = ValueError("not json")

        with patch("app.http_client.http_redirect") as mock_client:
            mock_client.get = AsyncMock(return_value=mock_resp)
            results = await c._do_search("LM317T")
            assert results == []

    @pytest.mark.asyncio
    async def test_search_data_not_list(self):
        c = self._make_connector()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"data": "not a list"}

        with patch("app.http_client.http_redirect") as mock_client:
            mock_client.get = AsyncMock(return_value=mock_resp)
            results = await c._do_search("LM317T")
            assert results == []

    @pytest.mark.asyncio
    async def test_search_skips_non_dict_items(self):
        c = self._make_connector()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"data": ["not a dict", 42]}

        with patch("app.http_client.http_redirect") as mock_client:
            mock_client.get = AsyncMock(return_value=mock_resp)
            results = await c._do_search("LM317T")
            assert results == []

    @pytest.mark.asyncio
    async def test_search_skips_empty_company(self):
        c = self._make_connector()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"data": [{"company": "", "part": "X", "qty": "1", "price": "1"}]}

        with patch("app.http_client.http_redirect") as mock_client:
            mock_client.get = AsyncMock(return_value=mock_resp)
            results = await c._do_search("LM317T")
            assert results == []

    @pytest.mark.asyncio
    async def test_search_proceeds_without_login(self):
        """Bearer auth doesn't need a username — login field is legacy/ignored."""
        from app.connectors.sources import BrokerBinConnector

        c = BrokerBinConnector(api_key="bb-token", api_secret="")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"data": [], "meta": {"total": 0}}

        with patch("app.http_client.http_redirect") as mock_client:
            mock_client.get = AsyncMock(return_value=mock_resp)
            results = await c._do_search("LM317T")
            assert results == []
            mock_client.get.assert_called_once()

    @pytest.mark.asyncio
    async def test_search_uses_bearer_auth(self):
        """v2.x docs: ``Authorization: Bearer <token>``.

        No HTTP Basic auth tuple.
        """
        c = self._make_connector()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"data": [], "meta": {"total": 0}}

        with patch("app.http_client.http_redirect") as mock_client:
            mock_client.get = AsyncMock(return_value=mock_resp)
            await c._do_search("LM317T")
            kwargs = mock_client.get.call_args.kwargs
            assert kwargs.get("headers", {}).get("Authorization") == "Bearer bb-token"
            assert "auth" not in kwargs, "Must not send HTTP Basic alongside Bearer"


# ═══════════════════════════════════════════════════════════════════════
#  Nexar/Octopart Connector tests
# ═══════════════════════════════════════════════════════════════════════


class TestNexarConnector:
    def _make_connector(self):
        from app.connectors.sources import NexarConnector

        # Parse-only tests need no token; the token-path tests seed the module
        # cache explicitly (see test_run_query_401_retry).
        return NexarConnector(client_id="nexar-id", client_secret="nexar-secret")

    def test_parse_full_with_sellers(self):
        c = self._make_connector()
        results_data = [
            {
                "part": {
                    "mpn": "LM317T",
                    "manufacturer": {"name": "TI"},
                    "sellers": [
                        {
                            "company": {"name": "Arrow", "homepageUrl": "https://arrow.com"},
                            "isAuthorized": True,
                            "offers": [
                                {
                                    "inventoryLevel": 5000,
                                    "prices": [{"price": 0.75, "currency": "USD", "quantity": 1}],
                                    "clickUrl": "https://octo.click/abc",
                                    "sku": "ARW-LM317",
                                }
                            ],
                        }
                    ],
                }
            }
        ]
        results = c._parse_full(results_data, "LM317T")
        assert len(results) == 1
        assert results[0]["vendor_name"] == "Arrow"
        assert results[0]["is_authorized"] is True
        assert results[0]["confidence"] == 5
        assert results[0]["unit_price"] == 0.75

    def test_parse_full_no_sellers(self):
        c = self._make_connector()
        results_data = [
            {
                "part": {
                    "mpn": "RARE",
                    "manufacturer": {"name": "M"},
                    "sellers": [],
                }
            }
        ]
        results = c._parse_full(results_data, "RARE")
        assert len(results) == 1
        assert results[0]["vendor_name"] == "(no sellers listed)"
        assert results[0]["confidence"] == 2

    def test_parse_full_seller_no_company_name(self):
        c = self._make_connector()
        results_data = [
            {
                "part": {
                    "mpn": "X",
                    "manufacturer": {"name": "M"},
                    "sellers": [
                        {
                            "company": {"name": "", "homepageUrl": ""},
                            "isAuthorized": False,
                            "offers": [{"inventoryLevel": 1, "prices": [], "sku": "S"}],
                        }
                    ],
                }
            }
        ]
        results = c._parse_full(results_data, "X")
        assert results == []  # no name → skipped

    def test_parse_full_seller_no_offers(self):
        c = self._make_connector()
        results_data = [
            {
                "part": {
                    "mpn": "X",
                    "manufacturer": {"name": "M"},
                    "sellers": [
                        {
                            "company": {"name": "Vendor", "homepageUrl": ""},
                            "isAuthorized": True,
                            "offers": [],
                        }
                    ],
                }
            }
        ]
        results = c._parse_full(results_data, "X")
        assert len(results) == 1
        assert results[0]["vendor_name"] == "Vendor"
        assert results[0]["qty_available"] is None
        assert results[0]["confidence"] == 3  # authorized, no offers

    def test_parse_full_deduplicates(self):
        c = self._make_connector()
        results_data = [
            {
                "part": {
                    "mpn": "LM317T",
                    "manufacturer": {"name": "TI"},
                    "sellers": [
                        {
                            "company": {"name": "Arrow", "homepageUrl": ""},
                            "isAuthorized": True,
                            "offers": [
                                {"inventoryLevel": 100, "prices": [], "clickUrl": "", "sku": "S1"},
                                {"inventoryLevel": 200, "prices": [], "clickUrl": "", "sku": "S1"},
                            ],
                        }
                    ],
                }
            }
        ]
        results = c._parse_full(results_data, "LM317T")
        assert len(results) == 1

    def test_parse_aggregate(self):
        c = self._make_connector()
        results_data = [
            {
                "part": {
                    "mpn": "LM317T",
                    "manufacturer": {"name": "TI"},
                    "totalAvail": 50000,
                    "avgAvail": 1000,
                    "medianPrice1000": {"price": 0.55, "currency": "USD"},
                    "shortDescription": "Voltage Reg",
                    "octopartUrl": "https://octopart.com/lm317t",
                    "manufacturerUrl": "https://ti.com/lm317t",
                    "category": {"name": "Linear Regulators"},
                }
            }
        ]
        results = c._parse_aggregate(results_data, "LM317T")
        assert len(results) == 1
        r = results[0]
        assert r["vendor_name"] == "Octopart (aggregate)"
        assert r["qty_available"] == 50000
        assert r["unit_price"] == 0.55
        assert r["confidence"] == 4

    def test_parse_aggregate_no_useful_data_skipped(self):
        c = self._make_connector()
        results_data = [
            {
                "part": {
                    "mpn": "X",
                    "manufacturer": {"name": "M"},
                    "totalAvail": None,
                    "medianPrice1000": {},
                }
            }
        ]
        results = c._parse_aggregate(results_data, "X")
        assert results == []

    def test_parse_aggregate_only_price(self):
        c = self._make_connector()
        results_data = [
            {
                "part": {
                    "mpn": "X",
                    "manufacturer": {"name": "M"},
                    "totalAvail": None,
                    "medianPrice1000": {"price": 1.00, "currency": "USD"},
                    "octopartUrl": None,
                    "manufacturerUrl": "",
                    "category": None,
                }
            }
        ]
        results = c._parse_aggregate(results_data, "X")
        assert len(results) == 1
        assert results[0]["confidence"] == 3

    def test_parse_full_handles_non_numeric_qty_price(self):
        c = self._make_connector()
        results_data = [
            {
                "part": {
                    "mpn": "LM317T",
                    "manufacturer": {"name": "TI"},
                    "sellers": [
                        {
                            "company": {"name": "Arrow", "homepageUrl": ""},
                            "isAuthorized": True,
                            "offers": [
                                {
                                    "inventoryLevel": "100+",
                                    "prices": [{"price": "N/A", "currency": "USD", "quantity": 1}],
                                    "clickUrl": "",
                                    "sku": "S1",
                                }
                            ],
                        }
                    ],
                }
            }
        ]
        results = c._parse_full(results_data, "LM317T")
        assert len(results) == 1
        assert results[0]["qty_available"] is None
        assert results[0]["unit_price"] is None

    @pytest.mark.asyncio
    async def test_empty_client_id_and_no_rest_key(self):
        from app.connectors.sources import NexarConnector

        c = NexarConnector(client_id="", client_secret="secret")
        results = await c._do_search("LM317T")
        assert results == []

    @pytest.mark.asyncio
    async def test_do_search_empty_rest_falls_through_to_graphql(self):
        """A REST v4 result of ``[]`` (200 but zero rows) must NOT short-circuit.

        _do_search falls through to the GraphQL seller path, which may surface rows the
        REST key's plan/coverage misses. Regression for the Nexar empty-REST short-
        circuit (Phase-4 audit item).
        """
        c = self._make_connector()
        graphql_resp = {
            "data": {
                "supSearchMpn": {
                    "results": [
                        {
                            "part": {
                                "mpn": "LM317T",
                                "manufacturer": {"name": "TI"},
                                "sellers": [
                                    {
                                        "company": {"name": "Arrow", "homepageUrl": "https://arrow.com"},
                                        "isAuthorized": True,
                                        "offers": [
                                            {
                                                "inventoryLevel": 500,
                                                "sku": "ARW-317",
                                                "clickUrl": "https://arrow.com/lm317t",
                                                "prices": [{"price": 0.60, "currency": "USD", "quantity": 1}],
                                            }
                                        ],
                                    }
                                ],
                            }
                        }
                    ]
                }
            }
        }
        with (
            patch.object(c, "_rest_search", new_callable=AsyncMock, return_value=[]),
            patch.object(c, "_run_query", new_callable=AsyncMock, return_value=graphql_resp) as mock_query,
        ):
            results = await c._do_search("LM317T")
        mock_query.assert_awaited_once()  # GraphQL WAS consulted despite the empty REST result
        assert len(results) == 1
        assert results[0]["vendor_name"] == "Arrow"

    @pytest.mark.asyncio
    async def test_do_search_nonempty_rest_short_circuits_graphql(self):
        """A non-empty REST v4 result wins outright — GraphQL is never queried."""
        c = self._make_connector()
        rest_rows = [{"vendor_name": "Mouser", "source_type": "octopart"}]
        with (
            patch.object(c, "_rest_search", new_callable=AsyncMock, return_value=rest_rows),
            patch.object(c, "_run_query", new_callable=AsyncMock) as mock_query,
        ):
            results = await c._do_search("LM317T")
        assert results == rest_rows
        mock_query.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_do_search_aggregate_query_success(self):
        """_do_search falls back to aggregate query when sellers not authorized."""
        c = self._make_connector()
        # First call (FULL_QUERY) returns error about sellers not authorized
        error_resp = {
            "errors": [{"message": "not authorized to access field 'sellers'"}],
            "data": {"supSearchMpn": {"results": []}},
        }
        agg_resp = {
            "data": {
                "supSearchMpn": {
                    "results": [
                        {
                            "part": {
                                "mpn": "LM317T",
                                "manufacturer": {"name": "TI"},
                                "totalAvail": 1000,
                                "medianPrice1000": {"price": 0.50, "currency": "USD"},
                                "shortDescription": "Reg",
                                "octopartUrl": "https://octopart.com/lm317t",
                                "manufacturerUrl": "",
                                "category": {"name": "Regulators"},
                            }
                        }
                    ]
                }
            }
        }
        with (
            patch.object(c, "_rest_search", new_callable=AsyncMock, return_value=None),
            patch.object(c, "_run_query", new_callable=AsyncMock, side_effect=[error_resp, agg_resp]),
        ):
            results = await c._do_search("LM317T")
            assert len(results) == 1
            assert results[0]["vendor_name"] == "Octopart (aggregate)"

    @pytest.mark.asyncio
    async def test_do_search_aggregate_query_errors(self):
        """_do_search returns empty when aggregate query has errors."""
        c = self._make_connector()
        error_resp = {
            "errors": [{"message": "Some API error"}],
            "data": {"supSearchMpn": {"results": []}},
        }
        with (
            patch.object(c, "_rest_search", new_callable=AsyncMock, return_value=None),
            patch.object(c, "_run_query", new_callable=AsyncMock, return_value=error_resp),
        ):
            results = await c._do_search("LM317T")
            assert results == []

    @pytest.mark.asyncio
    async def test_do_search_no_results(self):
        c = self._make_connector()
        resp = {
            "data": {"supSearchMpn": {"results": []}},
        }
        with (
            patch.object(c, "_rest_search", new_callable=AsyncMock, return_value=None),
            patch.object(c, "_run_query", new_callable=AsyncMock, return_value=resp),
        ):
            results = await c._do_search("NONEXIST")
            assert results == []

    @pytest.mark.asyncio
    async def test_rest_search_no_key(self):
        from app.connectors.sources import NexarConnector

        c = NexarConnector(client_id="id", client_secret="secret", octopart_api_key="")
        result = await c._rest_search("LM317T")
        assert result is None

    @pytest.mark.asyncio
    async def test_rest_search_success(self):
        from app.connectors.sources import NexarConnector

        c = NexarConnector(client_id="id", client_secret="secret", octopart_api_key="restkey")
        resp = _mock_response(
            200,
            {
                "results": [
                    {
                        "item": {
                            "mpn": "LM317T",
                            "manufacturer": {"name": "TI"},
                            "octopart_url": "https://octopart.com/lm317t",
                            "sellers": [
                                {
                                    "seller": {"name": "Arrow", "homepage_url": "https://arrow.com"},
                                    "is_authorized": True,
                                    "offers": [
                                        {
                                            "in_stock_quantity": 500,
                                            "sku": "ARW-317",
                                            "product_url": "https://arrow.com/lm317t",
                                            "prices": [{"price": 0.60, "currency": "USD", "quantity": 1}],
                                        }
                                    ],
                                }
                            ],
                        }
                    }
                ]
            },
        )
        with patch("app.http_client.http") as mock_http:
            mock_http.get = AsyncMock(return_value=resp)
            results = await c._rest_search("LM317T")
            assert len(results) == 1
            assert results[0]["vendor_name"] == "Arrow"

    @pytest.mark.asyncio
    async def test_rest_search_non_200(self):
        from app.connectors.sources import NexarConnector

        c = NexarConnector(client_id="id", client_secret="secret", octopart_api_key="key")
        resp = _mock_response(500, text="Error")
        resp.raise_for_status = MagicMock()  # don't raise
        with patch("app.http_client.http") as mock_http:
            mock_http.get = AsyncMock(return_value=resp)
            result = await c._rest_search("LM317T")
            assert result is None

    @pytest.mark.asyncio
    async def test_rest_search_error_in_response(self):
        from app.connectors.sources import NexarConnector

        c = NexarConnector(client_id="id", client_secret="secret", octopart_api_key="key")
        resp = _mock_response(200, {"error": "no token found"})
        with patch("app.http_client.http") as mock_http:
            mock_http.get = AsyncMock(return_value=resp)
            result = await c._rest_search("LM317T")
            assert result is None

    @pytest.mark.asyncio
    async def test_rest_search_error_dict(self):
        from app.connectors.sources import NexarConnector

        c = NexarConnector(client_id="id", client_secret="secret", octopart_api_key="key")
        resp = _mock_response(200, {"error": {"message": "unauthorized"}})
        with patch("app.http_client.http") as mock_http:
            mock_http.get = AsyncMock(return_value=resp)
            result = await c._rest_search("LM317T")
            assert result is None

    @pytest.mark.asyncio
    async def test_rest_search_exception(self):
        from app.connectors.sources import NexarConnector

        c = NexarConnector(client_id="id", client_secret="secret", octopart_api_key="key")
        with patch("app.http_client.http") as mock_http:
            mock_http.get = AsyncMock(side_effect=Exception("timeout"))
            result = await c._rest_search("LM317T")
            assert result is None

    def test_parse_rest_v4_prices_dict_format(self):
        c = self._make_connector()
        data = {
            "results": [
                {
                    "item": {
                        "mpn": "X",
                        "manufacturer": {"name": "M"},
                        "sellers": [
                            {
                                "seller": {"name": "S1"},
                                "is_authorized": False,
                                "offers": [
                                    {
                                        "in_stock_quantity": 10,
                                        "sku": "SK1",
                                        "prices": {"USD": [[1, 0.50]]},
                                    }
                                ],
                            }
                        ],
                    }
                }
            ]
        }
        results = c._parse_rest_v4(data, "X")
        assert len(results) == 1
        assert results[0]["unit_price"] == 0.50

    def test_parse_rest_v4_no_seller_name_skipped(self):
        c = self._make_connector()
        data = {
            "results": [
                {
                    "item": {
                        "mpn": "X",
                        "manufacturer": {"name": "M"},
                        "sellers": [
                            {
                                "seller": {"name": ""},
                                "offers": [{"sku": "S", "in_stock_quantity": 1, "prices": []}],
                            }
                        ],
                    }
                }
            ]
        }
        results = c._parse_rest_v4(data, "X")
        assert results == []

    def test_parse_rest_v4_deduplicates(self):
        c = self._make_connector()
        data = {
            "results": [
                {
                    "item": {
                        "mpn": "X",
                        "manufacturer": {"name": "M"},
                        "sellers": [
                            {
                                "seller": {"name": "S1"},
                                "offers": [
                                    {"sku": "SK1", "in_stock_quantity": 10, "prices": []},
                                    {"sku": "SK1", "in_stock_quantity": 20, "prices": []},
                                ],
                            }
                        ],
                    }
                }
            ]
        }
        results = c._parse_rest_v4(data, "X")
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_get_token(self):
        from app.connectors.sources import NexarConnector, _token_cache

        c = NexarConnector(client_id="id", client_secret="secret")
        assert c._token_cache_key() not in _token_cache  # cold process-wide cache
        token_resp = _mock_response(200, {"access_token": "tok123"})
        with patch("app.http_client.http") as mock_http:
            mock_http.post = AsyncMock(return_value=token_resp)
            token = await c._get_token()
            assert token == "tok123"
            # Second call should hit the process-wide cache — no extra mint POST.
            token2 = await c._get_token()
            assert token2 == "tok123"
            assert mock_http.post.call_count == 1

    @pytest.mark.asyncio
    async def test_run_query_401_retry(self):
        import time

        from app.connectors.sources import _token_cache

        c = self._make_connector()
        # Seed a valid cached bearer; the 401 handler must invalidate it and re-mint.
        _token_cache[c._token_cache_key()] = ("old-token", time.monotonic() + 600)
        resp_401 = _mock_response(401, text="Unauthorized")
        resp_401.raise_for_status = MagicMock()
        token_resp = _mock_response(200, {"access_token": "new-token"})
        ok_resp = _mock_response(200, {"data": {}})

        with patch("app.http_client.http") as mock_http:
            mock_http.post = AsyncMock(side_effect=[resp_401, token_resp, ok_resp])
            result = await c._run_query("query { test }", "LM317")
            assert result == {"data": {}}

    @pytest.mark.asyncio
    async def test_do_search_rest_returns_results(self):
        """When REST search returns results, skip GraphQL entirely."""
        from app.connectors.sources import NexarConnector

        c = NexarConnector(client_id="id", client_secret="secret", octopart_api_key="key")
        with patch.object(c, "_rest_search", new_callable=AsyncMock, return_value=[{"mpn": "X"}]):
            results = await c._do_search("X")
            assert results == [{"mpn": "X"}]

    @pytest.mark.asyncio
    async def test_do_search_rest_none_no_client_id(self):
        """REST returns None and no client_id -> empty."""
        from app.connectors.sources import NexarConnector

        c = NexarConnector(client_id="", client_secret="", octopart_api_key="key")
        with patch.object(c, "_rest_search", new_callable=AsyncMock, return_value=None):
            results = await c._do_search("X")
            assert results == []

    @pytest.mark.asyncio
    async def test_do_search_graphql_full_success(self):
        """Line 333: REST None, GraphQL full query succeeds with results -> _parse_full."""
        from app.connectors.sources import NexarConnector

        c = NexarConnector(client_id="id", client_secret="secret")
        graphql_resp = {
            "data": {
                "supSearchMpn": {
                    "results": [{"part": {"mpn": "LM317T"}}],
                }
            }
        }
        with (
            patch.object(c, "_rest_search", new_callable=AsyncMock, return_value=None),
            patch.object(c, "_run_query", new_callable=AsyncMock, return_value=graphql_resp),
            patch.object(c, "_parse_full", return_value=[{"mpn": "LM317T"}]) as mock_parse,
        ):
            results = await c._do_search("LM317T")
            assert results == [{"mpn": "LM317T"}]
            mock_parse.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════
#  Email Mining tests
# ═══════════════════════════════════════════════════════════════════════


class TestEmailMiner:
    def _make_miner(self, db=None, user_id=None):
        with patch("app.utils.graph_client.GraphClient") as MockGC:
            mock_gc = MagicMock()
            MockGC.return_value = mock_gc
            from app.connectors.email_mining import EmailMiner

            miner = EmailMiner("fake-token", db=db, user_id=user_id)
            miner.gc = mock_gc
        return miner

    # ── Helper method tests ─────────────────────────────────────────

    def test_extract_vendor_info(self):
        miner = self._make_miner()
        body = """
        Best regards,
        John Smith
        Phone: +1 (555) 123-4567
        www.chipvendor.com
        """
        result = miner._extract_vendor_info("John Smith", "john@chipvendor.com", body, "RFQ Response")
        assert result["vendor_name"] == "John Smith"
        assert len(result["phones"]) >= 1
        assert "chipvendor.com" in result["websites"]

    def test_extract_vendor_info_no_sender_name(self):
        miner = self._make_miner()
        result = miner._extract_vendor_info("", "sales@arrow.com", "", "")
        assert result["vendor_name"] == "Arrow"

    def test_extract_vendor_info_sender_name_equals_email(self):
        miner = self._make_miner()
        result = miner._extract_vendor_info("sales@arrow.com", "sales@arrow.com", "", "")
        assert result["vendor_name"] == "Arrow"

    def test_extract_vendor_info_skip_social_domains(self):
        miner = self._make_miner()
        body = "Visit us at linkedin.com and facebook.com and twitter.com"
        result = miner._extract_vendor_info("Name", "e@x.com", body, "")
        websites = result["websites"]
        for social in ["linkedin.com", "facebook.com", "twitter.com"]:
            assert social not in websites

    def test_extract_part_numbers(self):
        miner = self._make_miner()
        text = "We have LM317T and STM32F407VG in stock. Also ABC and STYLE."
        parts = miner._extract_part_numbers(text)
        assert "LM317T" in parts
        assert "STM32F407VG" in parts
        # false positives should be filtered
        assert "STYLE" not in parts

    def test_extract_part_numbers_filters(self):
        miner = self._make_miner()
        # Too short, no digits, no letters
        text = "ABC 1234 HTTP HTTPS FONT SIZE"
        parts = miner._extract_part_numbers(text)
        assert "HTTP" not in parts
        assert "HTTPS" not in parts
        assert "FONT" not in parts

    def test_normalize_vendor_from_email(self):
        miner = self._make_miner()
        assert miner._normalize_vendor_from_email("sales@arrow.com") == "arrow"
        assert miner._normalize_vendor_from_email("sales@www.arrow.co.uk") == "arrow"
        assert miner._normalize_vendor_from_email("noemail") == "noemail"

    # ── Dedup helpers ────────────────────────────────────────────────

    def test_already_processed_no_db(self):
        miner = self._make_miner(db=None)
        result = miner._already_processed(["msg1"], "mining")
        assert result == set()

    def test_already_processed_empty_ids(self):
        miner = self._make_miner(db=MagicMock())
        result = miner._already_processed([], "mining")
        assert result == set()

    def test_mark_processed_no_db(self):
        miner = self._make_miner(db=None)
        miner._mark_processed("msg1", "mining")  # should not raise

    def test_mark_processed_duplicate(self):
        mock_db = MagicMock()
        mock_db.flush.side_effect = Exception("duplicate key")
        mock_savepoint = MagicMock()
        mock_db.begin_nested.return_value = mock_savepoint
        miner = self._make_miner(db=mock_db)
        miner._mark_processed("msg1", "mining")  # should not raise
        mock_db.begin_nested.assert_called_once()
        mock_savepoint.rollback.assert_called_once()

    # ── Delta token helpers ──────────────────────────────────────────

    def test_get_delta_token_no_db(self):
        miner = self._make_miner(db=None, user_id=1)
        assert miner._get_delta_token("inbox_mining") is None

    def test_get_delta_token_no_user_id(self):
        miner = self._make_miner(db=MagicMock(), user_id=None)
        assert miner._get_delta_token("inbox_mining") is None

    def test_save_delta_token_no_db(self):
        miner = self._make_miner(db=None, user_id=1)
        miner._save_delta_token("inbox_mining", "token")  # should not raise

    def test_save_delta_token_no_user(self):
        miner = self._make_miner(db=MagicMock(), user_id=None)
        miner._save_delta_token("inbox_mining", "token")  # should not raise

    def test_clear_delta_token_no_db(self):
        miner = self._make_miner(db=None, user_id=1)
        miner._clear_delta_token("inbox_mining")  # should not raise

    def test_clear_delta_token_no_user(self):
        miner = self._make_miner(db=MagicMock(), user_id=None)
        miner._clear_delta_token("inbox_mining")  # should not raise

    # ── scan_inbox ───────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_scan_inbox_basic(self):
        miner = self._make_miner()
        miner.gc.get_all_pages = AsyncMock(
            return_value=[
                {
                    "id": "msg1",
                    "from": {"emailAddress": {"address": "vendor@chips.com", "name": "Chip Vendor"}},
                    "subject": "RFQ Response - Quote for LM317T",
                    "body": {"content": "We have LM317T in stock. Unit price $0.50. Lead time 2 weeks."},
                    "receivedDateTime": "2026-01-15T10:00:00Z",
                }
            ]
        )
        result = await miner.scan_inbox(use_delta=False)
        assert result["messages_scanned"] == 1
        assert result["vendors_found"] == 1
        assert result["used_delta"] is False

    @pytest.mark.asyncio
    async def test_scan_inbox_delta_success(self):
        miner = self._make_miner(user_id=1)
        miner.gc.delta_query = AsyncMock(
            return_value=(
                [
                    {
                        "id": "msg2",
                        "from": {"emailAddress": {"address": "sales@arrow.com", "name": "Arrow"}},
                        "subject": "Availability - LM317T",
                        "body": {"content": "In stock available. Unit price $0.60"},
                        "receivedDateTime": "2026-01-20T14:00:00Z",
                    }
                ],
                "new-delta-token",
            )
        )
        result = await miner.scan_inbox(use_delta=True)
        assert result["used_delta"] is True

    @pytest.mark.asyncio
    async def test_scan_inbox_delta_expired_falls_back(self):
        from app.utils.graph_client import GraphSyncStateExpired

        miner = self._make_miner(user_id=1)
        miner.gc.delta_query = AsyncMock(side_effect=GraphSyncStateExpired("expired"))
        miner.gc.get_all_pages = AsyncMock(return_value=[])
        result = await miner.scan_inbox(use_delta=True)
        assert result["used_delta"] is False

    @pytest.mark.asyncio
    async def test_scan_inbox_delta_generic_error(self):
        miner = self._make_miner(user_id=1)
        miner.gc.delta_query = AsyncMock(side_effect=Exception("network"))
        miner.gc.get_all_pages = AsyncMock(return_value=[])
        result = await miner.scan_inbox(use_delta=True)
        assert result["used_delta"] is False

    @pytest.mark.asyncio
    async def test_scan_inbox_skips_no_sender(self):
        miner = self._make_miner()
        miner.gc.get_all_pages = AsyncMock(
            return_value=[
                {
                    "id": "msg3",
                    "from": {"emailAddress": {"address": "", "name": ""}},
                    "subject": "Test",
                    "body": {"content": "body"},
                }
            ]
        )
        result = await miner.scan_inbox(use_delta=False)
        assert result["vendors_found"] == 0

    @pytest.mark.asyncio
    async def test_scan_inbox_last_contact_tracking(self):
        miner = self._make_miner()
        miner.gc.get_all_pages = AsyncMock(
            return_value=[
                {
                    "id": "m1",
                    "from": {"emailAddress": {"address": "v@chips.com", "name": "V"}},
                    "subject": "Hi",
                    "body": {"content": "test"},
                    "receivedDateTime": "2026-01-10T10:00:00Z",
                },
                {
                    "id": "m2",
                    "from": {"emailAddress": {"address": "v@chips.com", "name": "V"}},
                    "subject": "Hi again",
                    "body": {"content": "test2"},
                    "receivedDateTime": "2026-01-20T10:00:00Z",
                },
            ]
        )
        result = await miner.scan_inbox(use_delta=False)
        assert result["vendors_found"] == 1
        enriched = result["contacts_enriched"][0]
        assert enriched["message_count"] == 2
        assert "2026-01-20" in enriched["last_contact"]

    @pytest.mark.asyncio
    async def test_scan_inbox_bad_datetime(self):
        miner = self._make_miner()
        miner.gc.get_all_pages = AsyncMock(
            return_value=[
                {
                    "id": "m1",
                    "from": {"emailAddress": {"address": "v@x.com", "name": "V"}},
                    "subject": "Hi",
                    "body": {"content": "body"},
                    "receivedDateTime": "not-a-date",
                },
            ]
        )
        result = await miner.scan_inbox(use_delta=False)
        assert result["vendors_found"] == 1

    # ── scan_for_stock_lists ─────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_scan_for_stock_lists(self):
        miner = self._make_miner()
        miner.gc.get_all_pages = AsyncMock(
            return_value=[
                {
                    "id": "sl1",
                    "from": {"emailAddress": {"address": "vendor@parts.com", "name": "Parts Co"}},
                    "subject": "Stock List - January 2026",
                    "receivedDateTime": "2026-01-15T10:00:00Z",
                    "attachments": [
                        {"name": "stock_list.xlsx", "size": 12345, "id": "att1"},
                        {"name": "logo.png", "size": 5000, "id": "att2"},
                    ],
                }
            ]
        )
        result = await miner.scan_for_stock_lists()
        assert len(result) == 1
        assert len(result[0]["stock_files"]) == 1
        assert result[0]["stock_files"][0]["filename"] == "stock_list.xlsx"

    @pytest.mark.asyncio
    async def test_scan_for_stock_lists_no_matching_ext(self):
        miner = self._make_miner()
        miner.gc.get_all_pages = AsyncMock(
            return_value=[
                {
                    "id": "sl2",
                    "from": {"emailAddress": {"address": "v@x.com", "name": "V"}},
                    "subject": "Stock List",
                    "attachments": [{"name": "readme.txt", "size": 100, "id": "a1"}],
                }
            ]
        )
        result = await miner.scan_for_stock_lists()
        assert result == []

    @pytest.mark.asyncio
    async def test_scan_for_stock_lists_csv(self):
        miner = self._make_miner()
        miner.gc.get_all_pages = AsyncMock(
            return_value=[
                {
                    "id": "sl3",
                    "from": {"emailAddress": {"address": "v@x.com", "name": "V"}},
                    "subject": "Excess list",
                    "receivedDateTime": "2026-01-01T00:00:00Z",
                    "attachments": [{"name": "parts.csv", "size": 500, "id": "a1"}],
                }
            ]
        )
        result = await miner.scan_for_stock_lists()
        assert len(result) == 1

    # ── scan_sent_items ──────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_scan_sent_items_basic(self):
        miner = self._make_miner(user_id=1)
        miner.gc.delta_query = AsyncMock(
            return_value=(
                [
                    {
                        "id": "s1",
                        "subject": "[AVAIL-42] RFQ for LM317T",
                        "toRecipients": [{"emailAddress": {"address": "vendor@chips.com"}}],
                        "sentDateTime": "2026-01-15T10:00:00Z",
                    },
                    {
                        "id": "s2",
                        "subject": "Meeting notes",
                        "toRecipients": [{"emailAddress": {"address": "colleague@trioscs.com"}}],
                        "sentDateTime": "2026-01-15T11:00:00Z",
                    },
                ],
                "delta-token",
            )
        )
        result = await miner.scan_sent_items()
        assert result["messages_scanned"] == 2
        assert result["rfqs_detected"] == 1
        assert result["vendors_contacted"]["chips.com"] == 1
        assert result["used_delta"] is True

    @pytest.mark.asyncio
    async def test_scan_sent_items_delta_expired(self):
        from app.utils.graph_client import GraphSyncStateExpired

        miner = self._make_miner(user_id=1)
        miner.gc.delta_query = AsyncMock(side_effect=GraphSyncStateExpired("expired"))
        miner.gc.get_all_pages = AsyncMock(return_value=[])
        result = await miner.scan_sent_items()
        assert result["used_delta"] is False

    @pytest.mark.asyncio
    async def test_scan_sent_items_delta_generic_error(self):
        miner = self._make_miner(user_id=1)
        miner.gc.delta_query = AsyncMock(side_effect=Exception("fail"))
        miner.gc.get_all_pages = AsyncMock(return_value=[])
        result = await miner.scan_sent_items()
        assert result["used_delta"] is False

    @pytest.mark.asyncio
    async def test_scan_sent_items_fallback_search_fails(self):
        miner = self._make_miner(user_id=None)
        miner.gc.get_all_pages = AsyncMock(side_effect=Exception("search failed"))
        result = await miner.scan_sent_items()
        assert result["messages_scanned"] == 0
        assert result["rfqs_detected"] == 0

    @pytest.mark.asyncio
    async def test_scan_sent_items_flush_error(self):
        mock_db = MagicMock()
        # First flush calls succeed (during _mark_processed), final flush fails
        flush_calls = [0]

        def flush_side_effect():
            flush_calls[0] += 1
            if flush_calls[0] > 2:
                raise Exception("db error")

        mock_db.flush.side_effect = flush_side_effect
        miner = self._make_miner(db=mock_db, user_id=1)
        miner.gc.delta_query = AsyncMock(
            return_value=(
                [
                    {
                        "id": "s1",
                        "subject": "[AVAIL-1] RFQ",
                        "toRecipients": [{"emailAddress": {"address": "v@x.com"}}],
                    },
                ],
                "token",
            )
        )
        result = await miner.scan_sent_items()
        assert result["rfqs_detected"] == 1
        mock_db.rollback.assert_called()

    # ── _search_messages ─────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_search_messages_error(self):
        miner = self._make_miner()
        miner.gc.get_all_pages = AsyncMock(side_effect=Exception("search error"))
        result = await miner._search_messages("test query")
        assert result == []


# ═══════════════════════════════════════════════════════════════════════
#  safe_int / safe_float helper tests
# ═══════════════════════════════════════════════════════════════════════


class TestSafeHelpers:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            pytest.param(42, 42, id="int"),
            pytest.param("100", 100, id="numeric_str"),
            pytest.param(None, None, id="none"),
            pytest.param("abc", None, id="non_numeric_str"),
        ],
    )
    def test_safe_int(self, value, expected):
        from app.utils import safe_int

        assert safe_int(value) == expected

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            pytest.param(1.25, 1.25, id="float"),
            pytest.param("3.14", 3.14, id="numeric_str"),
            pytest.param(None, None, id="none"),
            pytest.param("N/A", None, id="non_numeric_str"),
        ],
    )
    def test_safe_float(self, value, expected):
        from app.utils import safe_float

        assert safe_float(value) == expected


# ═══════════════════════════════════════════════════════════════════════
#  Email Mining: AI classification edge cases (lines 343-344, 362-363)
# ═══════════════════════════════════════════════════════════════════════


class TestEmailMinerAIClassification:
    """Cover the AI classification branch in scan_inbox (lines 330-363)."""

    def _make_miner(self, db=None, user_id=None):
        with patch("app.utils.graph_client.GraphClient") as MockGC:
            mock_gc = MagicMock()
            MockGC.return_value = mock_gc
            from app.connectors.email_mining import EmailMiner

            miner = EmailMiner("fake-token", db=db, user_id=user_id)
            miner.gc = mock_gc
        return miner

    @pytest.mark.asyncio
    async def test_ai_classification_bad_datetime(self):
        """Lines 343-344: invalid receivedDateTime is silently ignored."""
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = None
        mock_db.begin_nested.return_value = MagicMock()
        miner = self._make_miner(db=mock_db, user_id=1)
        miner.gc.get_all_pages = AsyncMock(
            return_value=[
                {
                    "id": "msg-bad-dt",
                    "from": {"emailAddress": {"address": "vendor@test.com", "name": "Vendor"}},
                    "subject": "Quote for parts",
                    "body": {"content": "We have items"},
                    "receivedDateTime": "NOT-A-VALID-DATE",
                }
            ]
        )
        with patch(
            "app.services.email_intelligence_service.process_email_intelligence", new_callable=AsyncMock
        ) as mock_intel:
            result = await miner.scan_inbox(use_delta=False)
            assert result["messages_scanned"] == 1
            # process_email_intelligence should be called with received_at=None
            if mock_intel.called:
                assert mock_intel.call_args.kwargs.get("received_at") is None

    @pytest.mark.asyncio
    async def test_ai_classification_exception_caught(self):
        """Lines 362-363: exception in process_email_intelligence is caught."""
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = None
        mock_db.begin_nested.return_value = MagicMock()
        miner = self._make_miner(db=mock_db, user_id=1)
        miner.gc.get_all_pages = AsyncMock(
            return_value=[
                {
                    "id": "msg-err",
                    "from": {"emailAddress": {"address": "vendor@test.com", "name": "Vendor"}},
                    "subject": "Quote for LM317T",
                    "body": {"content": "We have LM317T in stock"},
                    "receivedDateTime": "2026-01-15T10:00:00Z",
                }
            ]
        )
        with patch(
            "app.services.email_intelligence_service.process_email_intelligence",
            new_callable=AsyncMock,
            side_effect=Exception("AI service down"),
        ):
            result = await miner.scan_inbox(use_delta=False)
            # Should not raise — error is caught and logged
            assert result["messages_scanned"] == 1


# ═══════════════════════════════════════════════════════════════════════
#  CircuitBreaker Edge Cases
# ═══════════════════════════════════════════════════════════════════════


class TestCircuitBreakerEdgeCases:
    """New edge cases: half-open failure, independent breakers."""

    def test_half_open_failure_reopens(self):
        from app.connectors.sources import CircuitBreaker

        cb = CircuitBreaker("test-reopen", fail_max=2, reset_timeout=0.01)
        cb.record_failure()
        cb.record_failure()
        assert cb.current_state == "open"
        time.sleep(0.02)
        assert cb.current_state == "half_open"
        cb.record_failure()
        assert cb.current_state == "open"

    def test_independent_breakers(self):
        from app.connectors.sources import CircuitBreaker

        cb1 = CircuitBreaker("breaker-a", fail_max=2, reset_timeout=60)
        cb2 = CircuitBreaker("breaker-b", fail_max=2, reset_timeout=60)
        cb1.record_failure()
        cb1.record_failure()
        assert cb1.current_state == "open"
        assert cb2.current_state == "closed"

    def test_success_from_half_open_closes(self):
        from app.connectors.sources import CircuitBreaker

        cb = CircuitBreaker("test-close", fail_max=1, reset_timeout=0.01)
        cb.record_failure()
        assert cb.current_state == "open"
        time.sleep(0.02)
        assert cb.current_state == "half_open"
        cb.record_success()
        assert cb.current_state == "closed"


# ═══════════════════════════════════════════════════════════════════════
#  Connector Malformed Response Edge Cases
# ═══════════════════════════════════════════════════════════════════════


class TestConnectorMalformedResponses:
    """Test connectors handle malformed API responses gracefully."""

    def _make_connector(self):
        """Create a concrete BaseConnector subclass for testing."""
        from app.connectors.sources import BaseConnector, _breakers

        _breakers.pop("TestEdgeConnector", None)

        class TestEdgeConnector(BaseConnector):
            source_name = "TestEdge"

            async def _do_search(self, mpn):
                return []

        return TestEdgeConnector()

    @pytest.mark.asyncio
    async def test_empty_json_response(self):
        """Connector returns [] → should return empty results, no crash."""
        connector = self._make_connector()
        results = await connector.search("LM317T")
        assert results == []

    @pytest.mark.asyncio
    async def test_search_empty_mpn_returns_empty(self):
        connector = self._make_connector()
        results = await connector.search("")
        assert results == []

    @pytest.mark.asyncio
    async def test_search_whitespace_mpn_returns_empty(self):
        connector = self._make_connector()
        results = await connector.search("   ")
        assert results == []

    @pytest.mark.asyncio
    async def test_search_unicode_mpn_no_crash(self):
        connector = self._make_connector()
        results = await connector.search("LM317T-éè")
        assert isinstance(results, list)

    @pytest.mark.asyncio
    async def test_search_special_chars_mpn(self):
        connector = self._make_connector()
        results = await connector.search("IC/123#A&B")
        assert isinstance(results, list)

    @pytest.mark.asyncio
    async def test_search_very_long_mpn(self):
        connector = self._make_connector()
        results = await connector.search("A" * 500)
        assert isinstance(results, list)
