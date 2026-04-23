"""test_sourcengine_connector.py — Tests for app/connectors/sourcengine.py.

Covers empty api_key, 429, 401, and _parse edge cases.

Called by: pytest
Depends on: app/connectors/sourcengine.py
"""

import os

os.environ["TESTING"] = "1"

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestSourcengineDoSearch:
    """Tests for SourcengineConnector._do_search."""

    @pytest.mark.asyncio
    async def test_empty_api_key_returns_empty(self):
        """No API key → return [] immediately."""
        from app.connectors.sourcengine import SourcengineConnector

        connector = SourcengineConnector(api_key="")
        result = await connector._do_search("LM317T")
        assert result == []

    @pytest.mark.asyncio
    async def test_status_429_returns_empty(self):
        """HTTP 429 → log warning and return []."""
        from app.connectors.sourcengine import SourcengineConnector

        connector = SourcengineConnector(api_key="test-key")

        mock_response = MagicMock()
        mock_response.status_code = 429

        with patch("app.connectors.sourcengine.http.get", new_callable=AsyncMock, return_value=mock_response):
            result = await connector._do_search("LM317T")

        assert result == []

    @pytest.mark.asyncio
    async def test_status_401_returns_empty(self):
        """HTTP 401 → log error and return []."""
        from app.connectors.sourcengine import SourcengineConnector

        connector = SourcengineConnector(api_key="bad-key")

        mock_response = MagicMock()
        mock_response.status_code = 401

        with patch("app.connectors.sourcengine.http.get", new_callable=AsyncMock, return_value=mock_response):
            result = await connector._do_search("LM317T")

        assert result == []

    @pytest.mark.asyncio
    async def test_successful_search_returns_results(self):
        """200 response with valid data → returns parsed results."""
        from app.connectors.sourcengine import SourcengineConnector

        connector = SourcengineConnector(api_key="valid-key")

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "offers": [
                {
                    "supplier": {"name": "Arrow Electronics"},
                    "mpn": "LM317T",
                    "quantity": 1000,
                    "unit_price": 0.75,
                    "currency": "USD",
                }
            ]
        }

        with patch("app.connectors.sourcengine.http.get", new_callable=AsyncMock, return_value=mock_response):
            result = await connector._do_search("LM317T")

        assert len(result) == 1
        assert result[0]["vendor_name"] == "Arrow Electronics"
        assert result[0]["unit_price"] == 0.75


class TestSourcengineParseEdgeCases:
    """Tests for SourcengineConnector._parse edge cases."""

    def _make_connector(self):
        from app.connectors.sourcengine import SourcengineConnector

        return SourcengineConnector(api_key="test-key")

    def test_non_list_offers_returns_empty(self):
        """When offers field is not a list → treat as empty."""
        connector = self._make_connector()
        data = {"offers": "not-a-list"}
        result = connector._parse(data, "LM317T")
        assert result == []

    def test_non_dict_offer_items_skipped(self):
        """Non-dict items in offers list are skipped."""
        connector = self._make_connector()
        data = {
            "offers": [
                "string-not-dict",
                42,
                None,
                {
                    "supplier": {"name": "Arrow"},
                    "mpn": "LM317T",
                    "quantity": 500,
                    "unit_price": 0.80,
                },
            ]
        }
        result = connector._parse(data, "LM317T")
        assert len(result) == 1
        assert result[0]["vendor_name"] == "Arrow"

    def test_supplier_as_string_not_dict(self):
        """supplier field is a string → use str() directly."""
        connector = self._make_connector()
        data = {
            "offers": [
                {
                    "supplier": "Mouser Electronics",
                    "mpn": "LM317T",
                    "quantity": 200,
                    "unit_price": 0.90,
                }
            ]
        }
        result = connector._parse(data, "LM317T")
        assert len(result) == 1
        assert result[0]["vendor_name"] == "Mouser Electronics"

    def test_empty_supplier_name_skipped(self):
        """Offer with no resolvable supplier name → skipped."""
        connector = self._make_connector()
        data = {
            "offers": [
                {
                    "supplier": {},  # dict but name is empty
                    "mpn": "LM317T",
                    "quantity": 100,
                    "unit_price": 1.00,
                    # No supplier_name or company fallbacks
                }
            ]
        }
        result = connector._parse(data, "LM317T")
        assert result == []

    def test_supplier_name_from_fallback_fields(self):
        """No supplier dict name → falls back to supplier_name or company."""
        connector = self._make_connector()
        data = {
            "offers": [
                {
                    "supplier": {},
                    "supplier_name": "DigiKey",
                    "mpn": "LM317T",
                    "quantity": 300,
                    "unit_price": 0.65,
                }
            ]
        }
        result = connector._parse(data, "LM317T")
        assert len(result) == 1
        assert result[0]["vendor_name"] == "DigiKey"

    def test_deduplication_by_vendor_mpn_sku(self):
        """Duplicate vendor+mpn+sku combinations are deduplicated."""
        connector = self._make_connector()
        data = {
            "offers": [
                {
                    "supplier": {"name": "Arrow"},
                    "mpn": "LM317T",
                    "sku": "SKU-001",
                    "quantity": 500,
                    "unit_price": 0.75,
                },
                {
                    "supplier": {"name": "Arrow"},
                    "mpn": "LM317T",
                    "sku": "SKU-001",
                    "quantity": 500,
                    "unit_price": 0.75,
                },
            ]
        }
        result = connector._parse(data, "LM317T")
        assert len(result) == 1

    def test_results_key_fallback(self):
        """Uses 'results' key when 'offers' is absent."""
        connector = self._make_connector()
        data = {
            "results": [
                {
                    "supplier": {"name": "Nexar"},
                    "mpn": "LM317T",
                    "quantity": 1000,
                    "unit_price": 0.55,
                }
            ]
        }
        result = connector._parse(data, "LM317T")
        assert len(result) == 1
        assert result[0]["vendor_name"] == "Nexar"

    def test_manufacturer_as_dict(self):
        """Manufacturer field as dict → extracts name key."""
        connector = self._make_connector()
        data = {
            "offers": [
                {
                    "supplier": {"name": "Arrow"},
                    "mpn": "LM317T",
                    "manufacturer": {"name": "Texas Instruments"},
                    "quantity": 500,
                    "unit_price": 0.75,
                }
            ]
        }
        result = connector._parse(data, "LM317T")
        assert len(result) == 1
        assert result[0]["manufacturer"] == "Texas Instruments"
