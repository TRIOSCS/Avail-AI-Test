"""
test_connectors.py — Unit tests for all supplier API connectors.

Mocks httpx to test parse logic without hitting real APIs.
Tests: DigiKey, eBay, Mouser, OEMSecrets, Sourcengine, BrokerBin, Nexar.

Called by: pytest
Depends on: app/connectors/*
"""

import pytest
import httpx
from unittest.mock import AsyncMock, patch, MagicMock

# ── DigiKey ─────────────────────────────────────────────────────────


class TestDigiKeyConnector:
    """Tests for DigiKeyConnector parse logic and search flow."""

    def _make_connector(self):
        from app.connectors.digikey import DigiKeyConnector
        c = DigiKeyConnector(client_id="test-id", client_secret="test-secret")
        c._token = "cached-token"  # skip OAuth
        return c

    def test_parse_products(self):
        from app.connectors.digikey import DigiKeyConnector
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
        assert r["mpn_matched"] == "LM317T"
        assert r["qty_available"] == 5000
        assert r["unit_price"] == 0.75  # smallest break qty
        assert r["source_type"] == "digikey"
        assert r["is_authorized"] is True
        assert r["confidence"] == 5
        assert r["vendor_sku"] == "296-1432-5-ND"

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
                    "Manufacturer": {"Name": "Analog Devices"},
                    "DigiKeyPartNumber": "AD-001",
                    "QuantityAvailable": 0,
                    "ProductUrl": "https://digikey.com/p/1",
                    "Description": {"DetailedDescription": "Obsolete part"},
                }
            ]
        }
        results = c._parse(data, "NRND-PART")
        assert results[0]["confidence"] == 3  # no qty → lower confidence

    def test_parse_camelcase_keys(self):
        """DigiKey sometimes returns camelCase keys."""
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

    @pytest.mark.asyncio
    async def test_empty_client_id_returns_empty(self):
        from app.connectors.digikey import DigiKeyConnector
        c = DigiKeyConnector(client_id="", client_secret="secret")
        results = await c._do_search("LM317T")
        assert results == []


# ── DigiKey safe helpers ────────────────────────────────────────────


class TestDigiKeySafeHelpers:
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


# ── eBay ────────────────────────────────────────────────────────────


class TestEbayConnector:
    """Tests for EbayConnector parse logic."""

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
                    "buyingOptions": ["FIXED_PRICE"],
                },
                {
                    "itemId": "v1|789012|0",
                    "title": "LM317T Used",
                    "price": {"value": "1.00", "currency": "USD"},
                    "seller": {"username": "parts_reseller"},
                    "condition": "Used",
                    "itemWebUrl": "https://www.ebay.com/itm/789012",
                },
            ]
        }
        results = c._parse(data, "LM317T")
        assert len(results) == 2
        assert results[0]["vendor_name"] == "chip_seller_99"
        assert results[0]["source_type"] == "ebay"
        assert results[0]["unit_price"] == 2.50
        assert results[0]["is_authorized"] is False

    def test_parse_empty(self):
        c = self._make_connector()
        assert c._parse({}, "LM317T") == []
        assert c._parse({"itemSummaries": []}, "LM317T") == []

    @pytest.mark.asyncio
    async def test_empty_client_id(self):
        from app.connectors.ebay import EbayConnector
        c = EbayConnector(client_id="", client_secret="secret")
        results = await c._do_search("LM317T")
        assert results == []


# ── Mouser ──────────────────────────────────────────────────────────


class TestMouserConnector:
    """Tests for MouserConnector parse logic."""

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
                        "Description": "IC REG LINEAR 1.2V 1.5A TO-220-3",
                    }
                ]
            }
        }
        results = c._parse(data, "LM317T")
        assert len(results) == 1
        r = results[0]
        assert r["vendor_name"] == "Mouser"
        assert r["mpn_matched"] == "LM317T"
        assert r["qty_available"] == 8500
        assert r["source_type"] == "mouser"
        assert r["is_authorized"] is True

    def test_parse_no_stock(self):
        c = self._make_connector()
        data = {
            "SearchResults": {
                "Parts": [
                    {
                        "ManufacturerPartNumber": "OBS-PART",
                        "Manufacturer": "Obsolete Mfr",
                        "MouserPartNumber": "999-OBS",
                        "Availability": "None",
                        "PriceBreaks": [],
                        "ProductDetailUrl": "https://mouser.com/p/999-OBS",
                        "Description": "Obsolete",
                    }
                ]
            }
        }
        results = c._parse(data, "OBS-PART")
        assert len(results) == 1
        assert results[0]["qty_available"] is None or results[0]["qty_available"] == 0

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


# ── OEMSecrets ──────────────────────────────────────────────────────


class TestOEMSecretsConnector:
    """Tests for OEMSecretsConnector parse logic."""

    def _make_connector(self):
        from app.connectors.oemsecrets import OEMSecretsConnector
        return OEMSecretsConnector(api_key="test-oem-key")

    def test_parse_distributors(self):
        c = self._make_connector()
        data = {
            "stock": [
                {
                    "distributor": "Arrow",
                    "manufacturer": "TI",
                    "mpn": "LM317T",
                    "distributor_sku": "ARW-LM317",
                    "stock": "10000",
                    "currency": "USD",
                    "moq": "1",
                    "buy_url": "https://arrow.com/buy/LM317T",
                    "datasheet_url": "https://arrow.com/ds/LM317T.pdf",
                    "prices": [
                        {"quantity": 1, "unit_price": "0.80"},
                    ],
                    "authorized_distributor": True,
                },
                {
                    "distributor": "Mouser",
                    "manufacturer": "TI",
                    "mpn": "LM317T",
                    "distributor_sku": "595-LM317T",
                    "stock": "5000",
                    "currency": "USD",
                    "buy_url": "https://mouser.com/LM317T",
                    "prices": [
                        {"quantity": 1, "unit_price": "0.89"},
                    ],
                    "authorized_distributor": True,
                },
            ]
        }
        results = c._parse(data, "LM317T")
        assert len(results) == 2
        assert results[0]["vendor_name"] == "Arrow"
        assert results[0]["source_type"] == "oemsecrets"
        assert results[1]["vendor_name"] == "Mouser"

    def test_parse_empty(self):
        c = self._make_connector()
        assert c._parse({"stock": []}, "XYZ") == []

    @pytest.mark.asyncio
    async def test_empty_api_key(self):
        from app.connectors.oemsecrets import OEMSecretsConnector
        c = OEMSecretsConnector(api_key="")
        results = await c._do_search("LM317T")
        assert results == []


# ── Sourcengine ─────────────────────────────────────────────────────


class TestSourcengineConnector:
    """Tests for SourcengineConnector parse logic."""

    def _make_connector(self):
        from app.connectors.sourcengine import SourcengineConnector
        return SourcengineConnector(api_key="test-src-key")

    def test_parse_results(self):
        c = self._make_connector()
        data = {
            "results": [
                {
                    "supplier": "Future Electronics",
                    "manufacturer": "Texas Instruments",
                    "mpn": "LM317T",
                    "sku": "FUT-LM317T",
                    "stock": 3000,
                    "unit_price": 0.65,
                    "currency": "USD",
                    "buy_url": "https://sourcengine.com/buy/LM317T",
                    "authorized": True,
                    "moq": 10,
                },
            ]
        }
        results = c._parse(data, "LM317T")
        assert len(results) == 1
        r = results[0]
        assert r["vendor_name"] == "Future Electronics"
        assert r["source_type"] == "sourcengine"

    def test_parse_empty(self):
        c = self._make_connector()
        assert c._parse({"results": []}, "XYZ") == []

    @pytest.mark.asyncio
    async def test_empty_api_key(self):
        from app.connectors.sourcengine import SourcengineConnector
        c = SourcengineConnector(api_key="")
        results = await c._do_search("LM317T")
        assert results == []


# ── BrokerBin ───────────────────────────────────────────────────────


class TestBrokerBinConnector:
    """Tests for BrokerBinConnector parse and confidence logic."""

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
        """Mock a successful BrokerBin response."""
        c = self._make_connector()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
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
            mock_client.get = AsyncMock(return_value=mock_response)

            results = await c._do_search("LM317T")

        assert len(results) == 2
        assert results[0]["vendor_name"] == "Chip Warehouse"
        assert results[0]["confidence"] == 5  # qty + price
        assert results[0]["source_type"] == "brokerbin"
        assert results[1]["confidence"] == 4  # qty but no price


# ── Nexar/Octopart ──────────────────────────────────────────────────


class TestNexarConnector:
    """Tests for NexarConnector parse_full logic."""

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
                    "manufacturer": {"name": "Texas Instruments"},
                    "sellers": [
                        {
                            "company": {"name": "Arrow", "homepageUrl": "https://arrow.com"},
                            "isAuthorized": True,
                            "offers": [
                                {
                                    "inventoryLevel": 5000,
                                    "prices": [
                                        {"price": 0.75, "currency": "USD", "quantity": 1}
                                    ],
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
        r = results[0]
        assert r["vendor_name"] == "Arrow"
        assert r["is_authorized"] is True
        assert r["confidence"] == 5  # authorized + qty
        assert r["source_type"] == "octopart"
        assert r["unit_price"] == 0.75

    def test_parse_full_no_sellers(self):
        c = self._make_connector()
        results_data = [
            {
                "part": {
                    "mpn": "RARE-PART",
                    "manufacturer": {"name": "Obscure MFR"},
                    "sellers": [],
                }
            }
        ]
        results = c._parse_full(results_data, "RARE-PART")
        assert len(results) == 1
        assert results[0]["vendor_name"] == "(no sellers listed)"
        assert results[0]["confidence"] == 2

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
        # Second offer with same SKU should be deduped
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_empty_client_id(self):
        from app.connectors.sources import NexarConnector
        c = NexarConnector(client_id="", client_secret="secret")
        results = await c._do_search("LM317T")
        assert results == []


# ── BaseConnector retry logic ───────────────────────────────────────


class TestBaseConnectorRetry:
    """Tests for BaseConnector retry and error handling."""

    @pytest.mark.asyncio
    async def test_retry_on_failure(self):
        from app.connectors.sources import BaseConnector

        class FailThenSucceed(BaseConnector):
            def __init__(self):
                super().__init__(timeout=1.0, max_retries=1)
                self.call_count = 0

            async def _do_search(self, part_number):
                self.call_count += 1
                if self.call_count == 1:
                    raise ConnectionError("Network error")
                return [{"vendor_name": "Test", "mpn_matched": part_number}]

        c = FailThenSucceed()
        results = await c.search("LM317T")
        assert len(results) == 1
        assert c.call_count == 2

    @pytest.mark.asyncio
    async def test_all_retries_exhausted(self):
        from app.connectors.sources import BaseConnector

        class AlwaysFails(BaseConnector):
            def __init__(self):
                super().__init__(timeout=1.0, max_retries=1)

            async def _do_search(self, part_number):
                raise ConnectionError("Always fails")

        c = AlwaysFails()
        results = await c.search("LM317T")
        assert results == []
