"""Comprehensive tests for app/connectors/ modules.

Covers: sources (BaseConnector, CircuitBreaker,
NexarConnector, BrokerBinConnector), email_mining, digikey, ebay, mouser,
oemsecrets, sourcengine, element14.

All external HTTP calls are mocked — no real API requests.
"""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

# ═══════════════════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════════════════


def _mock_response(status_code=200, json_data=None, text=""):
    """Build a fake httpx.Response."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    resp.text = text or str(json_data)
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
    async def test_search_skips_when_breaker_open(self):
        from app.connectors.sources import BaseConnector, _breakers

        class SkipConnector(BaseConnector):
            async def _do_search(self, pn):
                return [{"mpn": pn}]

        _breakers.pop("SkipConnector", None)
        c = SkipConnector()
        for _ in range(10):
            c._breaker.record_failure()
        assert c._breaker.current_state == "open"
        result = await c.search("LM317")
        assert result == []
        _breakers.pop("SkipConnector", None)

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
#  DigiKey Connector tests
# ═══════════════════════════════════════════════════════════════════════


class TestDigiKeyConnector:
    def _make_connector(self):
        from app.connectors.digikey import DigiKeyConnector

        c = DigiKeyConnector(client_id="test-id", client_secret="test-secret")
        c._token = "cached-token"
        c._token_expires_at = 9999999999  # far future — skip refresh
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


# ═══════════════════════════════════════════════════════════════════════
#  eBay Connector tests
# ═══════════════════════════════════════════════════════════════════════


class TestEbayConnector:
    def _make_connector(self):
        from app.connectors.ebay import EbayConnector

        c = EbayConnector(client_id="ebay-id", client_secret="ebay-secret")
        c._token = "cached-token"
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

    @pytest.mark.asyncio
    async def test_do_search_api_errors_in_body(self):
        c = self._make_connector()
        resp = _mock_response(
            200,
            {
                "Errors": [{"Message": "Invalid API key"}],
            },
        )
        with patch("app.connectors.mouser.http") as mock_http:
            mock_http.post = AsyncMock(return_value=resp)
            with pytest.raises(RuntimeError, match="Mouser API: Invalid API key"):
                await c._do_search("LM317T")

    def test_parse_null_search_results(self):
        c = self._make_connector()
        results = c._parse({}, "X")
        assert results == []


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
        """V3 API uses nested distributor dict with distributor_name, quantity_in_stock, prices dict."""
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

    def test_parse_moq_zero_becomes_none(self):
        """MOQ=0 from API must become None to satisfy chk_sight_moq."""
        c = self._make_connector()
        data = {
            "stock": [
                {
                    "distributor": "Broker X",
                    "mpn": "ABC",
                    "stock": 100,
                    "moq": 0,
                }
            ]
        }
        results = c._parse(data, "ABC")
        assert results[0]["moq"] is None

    def test_parse_moq_positive_kept(self):
        c = self._make_connector()
        data = {
            "stock": [
                {
                    "distributor": "Arrow",
                    "mpn": "ABC",
                    "stock": 100,
                    "moq": 5,
                }
            ]
        }
        results = c._parse(data, "ABC")
        assert results[0]["moq"] == 5


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

    def test_parse_moq_zero_becomes_none(self):
        """MOQ=0 from API must become None to satisfy chk_sight_moq."""
        c = self._make_connector()
        data = {
            "results": [
                {
                    "supplier": "Future Electronics",
                    "mpn": "ABC",
                    "quantity": 100,
                    "moq": 0,
                }
            ]
        }
        results = c._parse(data, "ABC")
        assert results[0]["moq"] is None

    def test_parse_moq_positive_kept(self):
        c = self._make_connector()
        data = {
            "results": [
                {
                    "supplier": "Future Electronics",
                    "mpn": "ABC",
                    "quantity": 100,
                    "moq": 10,
                }
            ]
        }
        results = c._parse(data, "ABC")
        assert results[0]["moq"] == 10


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
    async def test_fallback_keyword_search(self):
        """When exact MPN match returns 0, falls back to keyword search."""
        c = self._make_connector()
        exact_resp = _mock_response(200, json_data={"manufacturerPartNumberSearchReturn": {"products": []}})
        keyword_resp = _mock_response(
            200,
            json_data={
                "manufacturerPartNumberSearchReturn": {
                    "products": [
                        {
                            "translatedManufacturerPartNumber": "LM317T/NOPB",
                            "brandName": "TI",
                            "displayName": "VReg",
                            "sku": "123",
                            "stock": {"level": "100"},
                            "prices": [{"cost": "0.50"}],
                        }
                    ]
                }
            },
        )
        call_count = 0

        async def _mock_get(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return exact_resp if call_count == 1 else keyword_resp

        with patch("app.connectors.element14.http") as mock_http:
            mock_http.get = _mock_get
            results = await c._do_search("LM317T")

        assert len(results) == 1
        assert results[0]["mpn_matched"] == "LM317T/NOPB"
        assert call_count == 2  # exact + fallback

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


# ═══════════════════════════════════════════════════════════════════════
#  BrokerBin Connector tests
# ═══════════════════════════════════════════════════════════════════════


class TestBrokerBinConnector:
    def _make_connector(self):
        from app.connectors.sources import BrokerBinConnector

        return BrokerBinConnector(api_key="bb-token", api_secret="triomhk")

    @pytest.mark.asyncio
    async def test_empty_token(self):
        from app.connectors.sources import BrokerBinConnector

        c = BrokerBinConnector(api_key="", api_secret="")
        results = await c._do_search("LM317T")
        assert results == []

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
    async def test_search_no_login_header(self):
        from app.connectors.sources import BrokerBinConnector

        c = BrokerBinConnector(api_key="token", api_secret="")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"data": [], "meta": {"total": 0}}

        with patch("app.http_client.http_redirect") as mock_client:
            mock_client.get = AsyncMock(return_value=mock_resp)
            results = await c._do_search("LM317T")
            assert results == []


# ═══════════════════════════════════════════════════════════════════════
#  Nexar/Octopart Connector tests
# ═══════════════════════════════════════════════════════════════════════


class TestNexarConnector:
    def _make_connector(self):
        from app.connectors.sources import NexarConnector

        c = NexarConnector(client_id="nexar-id", client_secret="nexar-secret")
        c._token = "cached"
        return c

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

    @pytest.mark.asyncio
    async def test_empty_client_id_and_no_rest_key(self):
        from app.connectors.sources import NexarConnector

        c = NexarConnector(client_id="", client_secret="secret")
        results = await c._do_search("LM317T")
        assert results == []

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
        from app.connectors.sources import NexarConnector

        c = NexarConnector(client_id="id", client_secret="secret")
        assert c._token is None
        token_resp = _mock_response(200, {"access_token": "tok123"})
        with patch("app.http_client.http") as mock_http:
            mock_http.post = AsyncMock(return_value=token_resp)
            token = await c._get_token()
            assert token == "tok123"
            # Second call should use cache
            token2 = await c._get_token()
            assert token2 == "tok123"
            assert mock_http.post.call_count == 1

    @pytest.mark.asyncio
    async def test_run_query_401_retry(self):
        c = self._make_connector()
        c._token = "old-token"
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

    def test_is_offer_email(self):
        miner = self._make_miner()
        assert miner._is_offer_email("RFQ Response", "We have in stock, unit price $0.50")
        assert not miner._is_offer_email("Meeting invite", "Please join the call")

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

    # ── deep_scan_inbox ──────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_deep_scan_basic(self):
        miner = self._make_miner()
        miner.gc.get_all_pages = AsyncMock(
            return_value=[
                {
                    "id": "d1",
                    "from": {"emailAddress": {"address": "sales@chipco.com", "name": "Chip Co"}},
                    "subject": "Availability - LM317T from TI",
                    "body": {"content": "Phone: +1 555 123 4567\nwww.chipco.com"},
                    "receivedDateTime": "2026-01-15T10:00:00Z",
                }
            ]
        )
        with (
            patch("app.services.specialty_detector.detect_brands_from_text", return_value=["TI"]),
            patch("app.services.specialty_detector.detect_commodities_from_text", return_value=["Regulators"]),
        ):
            result = await miner.deep_scan_inbox()
            assert result["contacts_found"] == 1
            assert result["signatures_extracted"] == 1
            assert "chipco.com" in result["per_domain"]

    @pytest.mark.asyncio
    async def test_deep_scan_skips_system_domains(self):
        miner = self._make_miner()
        miner.gc.get_all_pages = AsyncMock(
            return_value=[
                {
                    "id": "d2",
                    "from": {"emailAddress": {"address": "noreply@microsoft.com", "name": "MS"}},
                    "subject": "Notification",
                    "body": {"content": "System notification"},
                }
            ]
        )
        result = await miner.deep_scan_inbox()
        assert result["contacts_found"] == 0

    @pytest.mark.asyncio
    async def test_deep_scan_skips_no_email(self):
        miner = self._make_miner()
        miner.gc.get_all_pages = AsyncMock(
            return_value=[
                {
                    "id": "d3",
                    "from": {"emailAddress": {"address": "", "name": ""}},
                    "subject": "Test",
                    "body": {"content": "body"},
                }
            ]
        )
        result = await miner.deep_scan_inbox()
        assert result["contacts_found"] == 0

    @pytest.mark.asyncio
    async def test_deep_scan_skips_no_at_sign(self):
        miner = self._make_miner()
        miner.gc.get_all_pages = AsyncMock(
            return_value=[
                {
                    "id": "d4",
                    "from": {"emailAddress": {"address": "invalid", "name": "X"}},
                    "subject": "Test",
                    "body": {"content": "body"},
                }
            ]
        )
        result = await miner.deep_scan_inbox()
        assert result["contacts_found"] == 0

    @pytest.mark.asyncio
    async def test_deep_scan_api_error(self):
        miner = self._make_miner()
        miner.gc.get_all_pages = AsyncMock(side_effect=Exception("API down"))
        result = await miner.deep_scan_inbox()
        assert result["messages_scanned"] == 0
        assert result["per_domain"] == {}

    @pytest.mark.asyncio
    async def test_deep_scan_specialty_detector_error(self):
        miner = self._make_miner()
        miner.gc.get_all_pages = AsyncMock(
            return_value=[
                {
                    "id": "d5",
                    "from": {"emailAddress": {"address": "s@vendor.com", "name": "V"}},
                    "subject": "Hi",
                    "body": {"content": "body"},
                }
            ]
        )
        with patch("app.services.specialty_detector.detect_brands_from_text", side_effect=ImportError("no module")):
            result = await miner.deep_scan_inbox()
            assert result["contacts_found"] == 1

    @pytest.mark.asyncio
    async def test_deep_scan_commit_error(self):
        mock_db = MagicMock()
        mock_db.commit.side_effect = Exception("commit failed")
        miner = self._make_miner(db=mock_db)
        miner.gc.get_all_pages = AsyncMock(return_value=[])
        result = await miner.deep_scan_inbox()
        assert result["messages_scanned"] == 0

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
    def test_safe_int_valid(self):
        from app.utils import safe_int

        assert safe_int(42) == 42
        assert safe_int("100") == 100

    def test_safe_int_invalid(self):
        from app.utils import safe_int

        assert safe_int(None) is None
        assert safe_int("abc") is None

    def test_safe_float_valid(self):
        from app.utils import safe_float

        assert safe_float(1.25) == 1.25
        assert safe_float("3.14") == 3.14

    def test_safe_float_invalid(self):
        from app.utils import safe_float

        assert safe_float(None) is None
        assert safe_float("N/A") is None


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
