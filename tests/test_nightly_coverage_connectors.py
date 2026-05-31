"""test_nightly_coverage_connectors.py — Coverage for ebay, mouser, digikey, oemsecrets connectors.

Targets:
- app/connectors/ebay.py: lines 56-156
- app/connectors/mouser.py: lines 33, 80-144
- app/connectors/digikey.py: lines 73, 92-147
- app/connectors/oemsecrets.py: lines 33, 59-133

Called by: pytest
Depends on: tests/conftest.py, unittest.mock
"""

import os

os.environ["TESTING"] = "1"

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.conftest import engine  # noqa: F401

# ══════════════════════════════════════════════════════════════════════════════
# eBay connector
# ══════════════════════════════════════════════════════════════════════════════


def _make_ebay():
    from app.connectors.ebay import EbayConnector

    return EbayConnector(client_id="test-id", client_secret="test-secret")


def _mock_token_response():
    m = MagicMock()
    m.json.return_value = {"access_token": "tok-abc", "expires_in": 7200}
    m.raise_for_status = MagicMock()
    return m


def _mock_search_response(items):
    m = MagicMock()
    m.status_code = 200
    m.json.return_value = {"itemSummaries": items}
    m.raise_for_status = MagicMock()
    return m


class TestEbayConnector:
    @pytest.mark.asyncio
    @patch("app.connectors.ebay.http")
    async def test_do_search_returns_empty_when_no_client_id(self, mock_http):
        from app.connectors.ebay import EbayConnector

        connector = EbayConnector(client_id="", client_secret="secret")
        result = await connector._do_search("LM317T")
        assert result == []
        mock_http.post.assert_not_called()

    @pytest.mark.asyncio
    @patch("app.connectors.ebay.http")
    async def test_do_search_happy_path(self, mock_http):
        connector = _make_ebay()
        token_resp = _mock_token_response()
        items = [
            {
                "seller": {"username": "seller1"},
                "title": "LM317T lot",
                "price": {"value": "1.50", "currency": "USD"},
                "condition": "New",
                "itemWebUrl": "https://ebay.com/item/1",
                "itemId": "item-001",
                "image": {"imageUrl": "https://img.ebay.com/1.jpg"},
                "estimatedAvailabilities": [{"estimatedAvailableQuantity": "100"}],
            }
        ]
        search_resp = _mock_search_response(items)
        mock_http.post = AsyncMock(return_value=token_resp)
        mock_http.get = AsyncMock(return_value=search_resp)

        results = await connector._do_search("LM317T")
        assert len(results) == 1
        assert results[0]["vendor_name"] == "seller1"
        assert results[0]["qty_available"] == 100
        assert results[0]["unit_price"] == 1.50

    @pytest.mark.asyncio
    @patch("app.connectors.ebay.http")
    async def test_do_search_401_refreshes_token(self, mock_http):
        connector = _make_ebay()
        token_resp = _mock_token_response()
        unauth_resp = MagicMock(status_code=401, raise_for_status=MagicMock())
        ok_resp = _mock_search_response([])

        mock_http.post = AsyncMock(return_value=token_resp)
        mock_http.get = AsyncMock(side_effect=[unauth_resp, ok_resp])

        results = await connector._do_search("LM317T")
        assert results == []
        assert mock_http.get.call_count == 2

    @pytest.mark.asyncio
    @patch("app.connectors.ebay.http")
    async def test_do_search_404_returns_empty(self, mock_http):
        connector = _make_ebay()
        token_resp = _mock_token_response()
        not_found = MagicMock(status_code=404)

        mock_http.post = AsyncMock(return_value=token_resp)
        mock_http.get = AsyncMock(return_value=not_found)

        results = await connector._do_search("LM317T")
        assert results == []

    @pytest.mark.asyncio
    @patch("app.connectors.ebay.http")
    async def test_get_token_uses_cache(self, mock_http):
        import time

        connector = _make_ebay()
        connector._token = "cached-tok"
        connector._token_expires_at = time.monotonic() + 3600  # far future

        token = await connector._get_token()
        assert token == "cached-tok"
        mock_http.post.assert_not_called()

    def test_parse_skips_items_without_seller(self):
        connector = _make_ebay()
        data = {"itemSummaries": [{"title": "no seller", "price": {}}]}
        result = connector._parse(data, "LM317T")
        assert result == []

    def test_parse_deduplicates_by_seller_item_id(self):
        connector = _make_ebay()
        item = {
            "seller": {"username": "sel1"},
            "title": "T",
            "price": {"value": "1.00", "currency": "USD"},
            "condition": "New",
            "itemWebUrl": "https://ebay.com/1",
            "itemId": "x1",
        }
        data = {"itemSummaries": [item, item]}
        result = connector._parse(data, "LM317T")
        assert len(result) == 1

    def test_parse_no_qty_sets_confidence_2(self):
        connector = _make_ebay()
        item = {
            "seller": {"username": "sel1"},
            "title": "T",
            "price": {"value": "0.99", "currency": "USD"},
            "condition": "Used",
            "itemWebUrl": "",
            "itemId": "y1",
        }
        result = connector._parse({"itemSummaries": [item]}, "ABC")
        assert result[0]["confidence"] == 2
        assert result[0]["qty_available"] is None

    def test_parse_with_qty_sets_confidence_3(self):
        connector = _make_ebay()
        item = {
            "seller": {"username": "sel2"},
            "title": "T",
            "price": {"value": "0.50", "currency": "USD"},
            "condition": "New",
            "itemWebUrl": "",
            "itemId": "z1",
            "estimatedAvailabilities": [{"estimatedAvailableQuantity": "50"}],
        }
        result = connector._parse({"itemSummaries": [item]}, "ABC")
        assert result[0]["confidence"] == 3
        assert result[0]["qty_available"] == 50

    def test_parse_empty_item_summaries(self):
        connector = _make_ebay()
        assert connector._parse({}, "TEST") == []
        assert connector._parse({"itemSummaries": []}, "TEST") == []


# ══════════════════════════════════════════════════════════════════════════════
# Mouser connector
# ══════════════════════════════════════════════════════════════════════════════


def _make_mouser():
    from app.connectors.mouser import MouserConnector

    return MouserConnector(api_key="test-key")


class TestMouserConnector:
    @pytest.mark.asyncio
    @patch("app.connectors.mouser.http")
    async def test_do_search_empty_key_returns_empty(self, mock_http):
        from app.connectors.mouser import MouserConnector

        connector = MouserConnector(api_key="")
        result = await connector._do_search("LM317T")
        assert result == []

    @pytest.mark.asyncio
    @patch("app.connectors.mouser.http")
    async def test_do_search_403_raises_auth_error(self, mock_http):
        from app.connectors.errors import ConnectorAuthError

        connector = _make_mouser()
        mock_http.post = AsyncMock(return_value=MagicMock(status_code=403, text="Forbidden"))
        with pytest.raises(ConnectorAuthError):
            await connector._do_search("LM317T")

    @pytest.mark.asyncio
    @patch("app.connectors.mouser.http")
    async def test_do_search_429_raises_rate_limit(self, mock_http):
        from app.connectors.errors import ConnectorRateLimitError

        connector = _make_mouser()
        mock_http.post = AsyncMock(return_value=MagicMock(status_code=429, text="Too many"))
        with pytest.raises(ConnectorRateLimitError):
            await connector._do_search("LM317T")

    @pytest.mark.asyncio
    @patch("app.connectors.mouser.http")
    async def test_do_search_body_rate_limit_error(self, mock_http):
        from app.connectors.errors import ConnectorRateLimitError

        connector = _make_mouser()
        resp = MagicMock(status_code=200, raise_for_status=MagicMock())
        resp.json.return_value = {"Errors": [{"Message": "Too many requests per second"}]}
        mock_http.post = AsyncMock(return_value=resp)
        with pytest.raises(ConnectorRateLimitError):
            await connector._do_search("LM317T")

    @pytest.mark.asyncio
    @patch("app.connectors.mouser.http")
    async def test_do_search_body_auth_error(self, mock_http):
        from app.connectors.errors import ConnectorAuthError

        connector = _make_mouser()
        resp = MagicMock(status_code=200, raise_for_status=MagicMock())
        resp.json.return_value = {"Errors": [{"Message": "Invalid API Key identifier"}]}
        mock_http.post = AsyncMock(return_value=resp)
        with pytest.raises(ConnectorAuthError):
            await connector._do_search("LM317T")

    @pytest.mark.asyncio
    @patch("app.connectors.mouser.http")
    async def test_do_search_body_generic_error_raises_runtime(self, mock_http):
        connector = _make_mouser()
        resp = MagicMock(status_code=200, raise_for_status=MagicMock())
        resp.json.return_value = {"Errors": [{"Message": "Invalid part number"}]}
        mock_http.post = AsyncMock(return_value=resp)
        with pytest.raises(RuntimeError, match="Mouser API"):
            await connector._do_search("BADPART")

    @pytest.mark.asyncio
    @patch("app.connectors.mouser.http")
    async def test_do_search_success_returns_parsed(self, mock_http):
        connector = _make_mouser()
        resp = MagicMock(status_code=200, raise_for_status=MagicMock())
        resp.json.return_value = {
            "Errors": [],
            "SearchResults": {
                "Parts": [
                    {
                        "ManufacturerPartNumber": "LM317T",
                        "Manufacturer": "Texas Instruments",
                        "MouserPartNumber": "511-LM317T",
                        "Description": "Voltage Regulator",
                        "ProductDetailUrl": "https://mouser.com/p/lm317t",
                        "Availability": "3,500 In Stock",
                        "PriceBreaks": [
                            {"Quantity": 1, "Price": "$0.45"},
                            {"Quantity": 100, "Price": "$0.30"},
                        ],
                    }
                ]
            },
        }
        mock_http.post = AsyncMock(return_value=resp)
        results = await connector._do_search("LM317T")
        assert len(results) == 1
        assert results[0]["qty_available"] == 3500
        assert results[0]["unit_price"] == 0.45
        assert results[0]["is_authorized"] is True

    def test_parse_in_stock_no_count(self):
        connector = _make_mouser()
        data = {
            "SearchResults": {
                "Parts": [
                    {
                        "ManufacturerPartNumber": "TMP",
                        "Manufacturer": "MFR",
                        "Availability": "In Stock",
                        "PriceBreaks": [],
                    }
                ]
            }
        }
        results = connector._parse(data, "TMP")
        assert results[0]["qty_available"] == 1

    def test_parse_no_availability(self):
        connector = _make_mouser()
        data = {"SearchResults": {"Parts": [{"ManufacturerPartNumber": "X", "Availability": ""}]}}
        results = connector._parse(data, "X")
        assert results[0]["qty_available"] is None
        assert results[0]["confidence"] == 3

    def test_parse_empty_results(self):
        connector = _make_mouser()
        assert connector._parse({}, "X") == []
        assert connector._parse({"SearchResults": {}}, "X") == []


# ══════════════════════════════════════════════════════════════════════════════
# DigiKey connector
# ══════════════════════════════════════════════════════════════════════════════


def _make_digikey():
    from app.connectors.digikey import DigiKeyConnector

    return DigiKeyConnector(client_id="test-id", client_secret="test-secret")


def _mock_dk_token():
    m = MagicMock()
    m.json.return_value = {"access_token": "dk-tok", "expires_in": 590}
    m.raise_for_status = MagicMock()
    return m


class TestDigiKeyConnector:
    @pytest.mark.asyncio
    @patch("app.connectors.digikey.http")
    async def test_do_search_empty_client_id_returns_empty(self, mock_http):
        from app.connectors.digikey import DigiKeyConnector

        connector = DigiKeyConnector(client_id="", client_secret="secret")
        result = await connector._do_search("LM317T")
        assert result == []

    @pytest.mark.asyncio
    @patch("app.connectors.digikey.http")
    async def test_do_search_401_refreshes_token(self, mock_http):
        connector = _make_digikey()
        token_resp = _mock_dk_token()
        unauth = MagicMock(status_code=401)
        ok_resp = MagicMock(status_code=200, raise_for_status=MagicMock())
        ok_resp.json.return_value = {"Products": []}

        mock_http.post = AsyncMock(side_effect=[token_resp, token_resp, ok_resp])
        # First call: token; second call: 401 on search; third call: token refresh; fourth: search ok
        mock_http.post = AsyncMock(side_effect=[token_resp, unauth, token_resp, ok_resp])
        results = await connector._do_search("LM317T")
        assert results == []

    @pytest.mark.asyncio
    @patch("app.connectors.digikey.asyncio")
    @patch("app.connectors.digikey.http")
    async def test_do_search_429_waits_and_retries(self, mock_http, mock_asyncio):
        from app.connectors.errors import ConnectorRateLimitError

        connector = _make_digikey()
        token_resp = _mock_dk_token()
        rate_limited = MagicMock(status_code=429, headers={})
        second_rate = MagicMock(status_code=429, headers={})

        mock_http.post = AsyncMock(side_effect=[token_resp, rate_limited, second_rate])
        mock_asyncio.sleep = AsyncMock()

        with pytest.raises(ConnectorRateLimitError):
            await connector._do_search("LM317T")

    @pytest.mark.asyncio
    @patch("app.connectors.digikey.http")
    async def test_do_search_happy_path(self, mock_http):
        connector = _make_digikey()
        token_resp = _mock_dk_token()
        search_resp = MagicMock(status_code=200, raise_for_status=MagicMock())
        search_resp.json.return_value = {
            "Products": [
                {
                    "ManufacturerPartNumber": "LM317T",
                    "Manufacturer": {"Name": "Texas Instruments"},
                    "DigiKeyPartNumber": "LM317T-ND",
                    "QuantityAvailable": 5000,
                    "Description": {"DetailedDescription": "Voltage Reg"},
                    "ProductUrl": "https://www.digikey.com/product/lm317t",
                    "StandardPricing": [
                        {"BreakQuantity": 1, "UnitPrice": 0.65},
                        {"BreakQuantity": 100, "UnitPrice": 0.50},
                    ],
                }
            ]
        }
        mock_http.post = AsyncMock(side_effect=[token_resp, search_resp])

        results = await connector._do_search("LM317T")
        assert len(results) == 1
        assert results[0]["qty_available"] == 5000
        assert results[0]["unit_price"] == 0.65
        assert results[0]["is_authorized"] is True

    def test_parse_lowercase_keys(self):
        connector = _make_digikey()
        data = {
            "products": [
                {
                    "manufacturerPartNumber": "ABC",
                    "manufacturer": {"Name": "MFR"},
                    "digiKeyPartNumber": "ABC-ND",
                    "quantityAvailable": 100,
                    "description": {"DetailedDescription": "desc"},
                    "productUrl": "/product/abc",
                    "standardPricing": [{"breakQuantity": 1, "unitPrice": 1.00}],
                }
            ]
        }
        results = connector._parse(data, "ABC")
        assert len(results) == 1
        assert results[0]["click_url"].startswith("https://www.digikey.com")

    def test_parse_no_pricing_falls_back_to_unit_price(self):
        connector = _make_digikey()
        data = {
            "Products": [
                {
                    "ManufacturerPartNumber": "XYZ",
                    "Manufacturer": {"Name": ""},
                    "QuantityAvailable": 0,
                    "UnitPrice": 2.50,
                    "ProductUrl": "https://www.digikey.com/xyz",
                }
            ]
        }
        results = connector._parse(data, "XYZ")
        assert results[0]["unit_price"] == 2.50

    def test_parse_empty_returns_empty(self):
        connector = _make_digikey()
        assert connector._parse({}, "X") == []


# ══════════════════════════════════════════════════════════════════════════════
# OEMSecrets connector
# ══════════════════════════════════════════════════════════════════════════════


def _make_oems():
    from app.connectors.oemsecrets import OEMSecretsConnector

    return OEMSecretsConnector(api_key="test-key")


class TestOEMSecretsConnector:
    @pytest.mark.asyncio
    @patch("app.connectors.oemsecrets.http")
    async def test_do_search_empty_key_returns_empty(self, mock_http):
        from app.connectors.oemsecrets import OEMSecretsConnector

        connector = OEMSecretsConnector(api_key="")
        result = await connector._do_search("LM317T")
        assert result == []

    @pytest.mark.asyncio
    @patch("app.connectors.oemsecrets.http")
    async def test_do_search_401_raises_auth_error(self, mock_http):
        from app.connectors.errors import ConnectorAuthError

        connector = _make_oems()
        mock_http.get = AsyncMock(return_value=MagicMock(status_code=401, text="Unauthorized"))
        with pytest.raises(ConnectorAuthError, match="OEMSecrets auth"):
            await connector._do_search("LM317T")

    @pytest.mark.asyncio
    @patch("app.connectors.oemsecrets.http")
    async def test_do_search_429_raises_rate_limit(self, mock_http):
        from app.connectors.errors import ConnectorRateLimitError

        connector = _make_oems()
        mock_http.get = AsyncMock(return_value=MagicMock(status_code=429, text="Rate limited"))
        with pytest.raises(ConnectorRateLimitError):
            await connector._do_search("LM317T")

    @pytest.mark.asyncio
    @patch("app.connectors.oemsecrets.http")
    async def test_do_search_non_200_raises_for_status(self, mock_http):
        import httpx

        connector = _make_oems()
        mock_resp = MagicMock(status_code=503, text="Service Unavailable")
        mock_resp.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError("503", request=MagicMock(), response=MagicMock())
        )
        mock_http.get = AsyncMock(return_value=mock_resp)
        with pytest.raises(httpx.HTTPStatusError):
            await connector._do_search("LM317T")

    @pytest.mark.asyncio
    @patch("app.connectors.oemsecrets.http")
    async def test_do_search_non_json_returns_empty(self, mock_http):
        connector = _make_oems()
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.side_effect = ValueError("not json")
        mock_http.get = AsyncMock(return_value=mock_resp)
        result = await connector._do_search("LM317T")
        assert result == []

    @pytest.mark.asyncio
    @patch("app.connectors.oemsecrets.http")
    async def test_do_search_happy_path(self, mock_http):
        connector = _make_oems()
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = [
            {
                "distributor": {"distributor_name": "DigiKey"},
                "source_part_number": "LM317T",
                "manufacturer": "TI",
                "quantity_in_stock": 5000,
                "prices": {"USD": [{"unit_break": 1, "unit_price": "0.60"}]},
                "buy_now_url": "https://digikey.com/lm317t",
                "currency": "USD",
            }
        ]
        mock_http.get = AsyncMock(return_value=mock_resp)
        results = await connector._do_search("LM317T")
        assert len(results) == 1
        assert results[0]["vendor_name"] == "DigiKey"
        assert results[0]["qty_available"] == 5000

    def test_parse_list_input(self):
        connector = _make_oems()
        data = [
            {
                "distributor": {"distributor_name": "Arrow"},
                "source_part_number": "ABC",
                "manufacturer": "MFR",
                "quantity_in_stock": 100,
                "prices": {"USD": [{"unit_price": "1.00"}]},
                "buy_now_url": "https://arrow.com",
                "currency": "USD",
            }
        ]
        results = connector._parse(data, "ABC")
        assert len(results) == 1

    def test_parse_flat_distributor_string(self):
        connector = _make_oems()
        data = [
            {
                "distributor": "Mouser",
                "source_part_number": "XYZ",
                "manufacturer": "",
                "quantity_in_stock": 200,
            }
        ]
        results = connector._parse(data, "XYZ")
        assert results[0]["vendor_name"] == "Mouser"

    def test_parse_skips_item_without_distributor(self):
        connector = _make_oems()
        data = [{"source_part_number": "NONAME", "quantity_in_stock": 10}]
        results = connector._parse(data, "NONAME")
        assert results == []

    def test_parse_deduplicates_by_dist_mpn_sku(self):
        connector = _make_oems()
        item = {
            "distributor": {"distributor_name": "Arrow"},
            "source_part_number": "DUP",
            "quantity_in_stock": 10,
        }
        results = connector._parse([item, item], "DUP")
        assert len(results) == 1

    def test_parse_falls_back_to_first_currency_if_no_usd(self):
        connector = _make_oems()
        data = [
            {
                "distributor": {"distributor_name": "TME"},
                "source_part_number": "EUR-PART",
                "prices": {"EUR": [{"unit_price": "2.50"}]},
                "quantity_in_stock": 50,
            }
        ]
        results = connector._parse(data, "EUR-PART")
        assert results[0]["unit_price"] == 2.50

    def test_parse_authorised_status(self):
        connector = _make_oems()
        data = [
            {
                "distributor": {"distributor_name": "RS"},
                "source_part_number": "AUTH",
                "distributor_authorisation_status": "authorised",
                "quantity_in_stock": 5,
            }
        ]
        results = connector._parse(data, "AUTH")
        assert results[0]["is_authorized"] is True

    def test_parse_non_authorised_status(self):
        connector = _make_oems()
        data = [
            {
                "distributor": {"distributor_name": "XYZ-DIST"},
                "source_part_number": "NONAUTH",
                "distributor_authorisation_status": "unauthorised",
                "quantity_in_stock": 5,
            }
        ]
        results = connector._parse(data, "NONAUTH")
        assert results[0]["is_authorized"] is False

    def test_parse_skips_non_dict_items(self):
        connector = _make_oems()
        data = ["not-a-dict", None, 123]
        results = connector._parse(data, "X")
        assert results == []

    def test_parse_stock_key_as_qty(self):
        connector = _make_oems()
        data = [
            {
                "distributor": {"distributor_name": "Farnell"},
                "source_part_number": "P1",
                "stock": 999,
                "prices": {"USD": [{"unit_price": "0.99"}]},
            }
        ]
        results = connector._parse(data, "P1")
        assert results[0]["qty_available"] == 999
