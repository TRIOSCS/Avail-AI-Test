"""test_sourcengine_connector.py — Tests for app/connectors/sourcengine.py.

Covers empty api_key, 429, 401, and _parse edge cases.

Called by: pytest
Depends on: app/connectors/sourcengine.py
"""

import os

os.environ["TESTING"] = "1"

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.connectors.errors import ConnectorAuthError, ConnectorRateLimitError


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
    async def test_status_429_raises_for_health_monitor(self):
        """Sourcengine 429 raises RuntimeError so health_monitor flips
        api_sources.status to 'error'; search_service excludes the source from user
        searches; auto-recovers on next successful ping.

        Replaces the prior silent-empty contract per connector convention. See
        docs/APP_MAP_INTERACTIONS.md § Connector Failure Contract.
        """
        from app.connectors.sourcengine import SourcengineConnector

        connector = SourcengineConnector(api_key="test-key")

        mock_response = MagicMock()
        mock_response.status_code = 429
        mock_response.text = "Too Many Requests"

        with patch("app.connectors.sourcengine.http.get", new_callable=AsyncMock, return_value=mock_response):
            with pytest.raises(ConnectorRateLimitError, match="Sourcengine rate limited"):
                await connector._do_search("LM317T")

    @pytest.mark.asyncio
    async def test_status_401_raises_for_health_monitor(self):
        """Sourcengine 401 (auth) raises RuntimeError — same contract as 429."""
        from app.connectors.sourcengine import SourcengineConnector

        connector = SourcengineConnector(api_key="bad-key")

        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.text = "Unauthorized"

        with patch("app.connectors.sourcengine.http.get", new_callable=AsyncMock, return_value=mock_response):
            with pytest.raises(ConnectorAuthError, match="Sourcengine auth error"):
                await connector._do_search("LM317T")

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

    @pytest.mark.parametrize(
        ("data", "expected_vendor_name"),
        [
            pytest.param({"offers": "not-a-list"}, None, id="non_list_offers_returns_empty"),
            pytest.param(
                {
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
                },
                "Arrow",
                id="non_dict_offer_items_skipped",
            ),
            pytest.param(
                {
                    "offers": [
                        {
                            "supplier": "Mouser Electronics",
                            "mpn": "LM317T",
                            "quantity": 200,
                            "unit_price": 0.90,
                        }
                    ]
                },
                "Mouser Electronics",
                id="supplier_as_string_not_dict",
            ),
            pytest.param(
                {
                    "offers": [
                        {
                            "supplier": {},  # dict but name is empty
                            "mpn": "LM317T",
                            "quantity": 100,
                            "unit_price": 1.00,
                            # No supplier_name or company fallbacks
                        }
                    ]
                },
                None,
                id="empty_supplier_name_skipped",
            ),
            pytest.param(
                {
                    "offers": [
                        {
                            "supplier": {},
                            "supplier_name": "DigiKey",
                            "mpn": "LM317T",
                            "quantity": 300,
                            "unit_price": 0.65,
                        }
                    ]
                },
                "DigiKey",
                id="supplier_name_from_fallback_fields",
            ),
            pytest.param(
                {
                    "results": [
                        {
                            "supplier": {"name": "Nexar"},
                            "mpn": "LM317T",
                            "quantity": 1000,
                            "unit_price": 0.55,
                        }
                    ]
                },
                "Nexar",
                id="results_key_fallback",
            ),
        ],
    )
    def test_parse_supplier_name_resolution(self, data, expected_vendor_name):
        """_parse resolves (or rejects) the vendor name across supplier
        shapes/fallbacks."""
        connector = self._make_connector()
        result = connector._parse(data, "LM317T")
        if expected_vendor_name is None:
            assert result == []
        else:
            assert len(result) == 1
            assert result[0]["vendor_name"] == expected_vendor_name

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

    def test_parse_non_dict_response_returns_empty(self):
        """A top-level non-object 200 body (e.g. a JSON array) returns [] instead of
        crashing on ``data.get`` — a shape-drift guard (Phase-4 audit)."""
        connector = self._make_connector()
        assert connector._parse([{"supplier": {"name": "Arrow"}}], "LM317T") == []
        assert connector._parse("unexpected", "LM317T") == []

    def test_parse_unrecognized_envelope_warns(self):
        """A 200 object with none of the recognized offer keys logs a drift WARNING and
        returns [] (never masquerades as a silent 'no matches')."""
        import app.connectors.sourcengine as sg_mod

        connector = self._make_connector()
        with patch.object(sg_mod.logger, "warning") as mock_warn:
            result = connector._parse({"unexpected_key": [1, 2, 3]}, "LM317T")
        assert result == []
        mock_warn.assert_called_once()
        assert "may have drifted" in mock_warn.call_args.args[0]

    def test_parse_recognized_empty_envelope_no_warn(self):
        """A recognized-but-empty envelope (``{"offers": []}``) is a legitimate empty
        result and must NOT trigger the drift warning."""
        import app.connectors.sourcengine as sg_mod

        connector = self._make_connector()
        with patch.object(sg_mod.logger, "warning") as mock_warn:
            result = connector._parse({"offers": []}, "LM317T")
        assert result == []
        mock_warn.assert_not_called()

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
