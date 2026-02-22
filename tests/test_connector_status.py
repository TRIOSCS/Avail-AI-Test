"""
test_connector_status.py — Tests for app/connector_status.py

Covers uncovered line 35: when all connectors are disabled.
"""

from unittest.mock import patch


class TestLogConnectorStatus:
    def test_all_disabled_returns_dict(self):
        """When no credentials are set, all connectors are disabled (line 35)."""
        from app.connector_status import log_connector_status

        with patch("app.connector_status.settings") as mock_settings:
            # Set all credentials to empty strings
            mock_settings.nexar_client_id = ""
            mock_settings.nexar_client_secret = ""
            mock_settings.brokerbin_api_key = ""
            mock_settings.brokerbin_api_secret = ""
            mock_settings.ebay_client_id = ""
            mock_settings.ebay_client_secret = ""
            mock_settings.digikey_client_id = ""
            mock_settings.digikey_client_secret = ""
            mock_settings.mouser_api_key = ""
            mock_settings.oemsecrets_api_key = ""
            mock_settings.sourcengine_api_key = ""
            mock_settings.element14_api_key = ""
            mock_settings.tme_api_token = ""
            mock_settings.tme_api_secret = ""
            mock_settings.anthropic_api_key = ""
            mock_settings.azure_client_id = ""
            mock_settings.azure_client_secret = ""
            mock_settings.azure_tenant_id = ""

            result = log_connector_status()

        assert isinstance(result, dict)
        assert all(v is False for v in result.values())

    def test_some_enabled(self):
        """When some credentials are set, those connectors are enabled."""
        from app.connector_status import log_connector_status

        with patch("app.connector_status.settings") as mock_settings:
            mock_settings.nexar_client_id = "id"
            mock_settings.nexar_client_secret = "secret"
            mock_settings.brokerbin_api_key = ""
            mock_settings.brokerbin_api_secret = ""
            mock_settings.ebay_client_id = ""
            mock_settings.ebay_client_secret = ""
            mock_settings.digikey_client_id = ""
            mock_settings.digikey_client_secret = ""
            mock_settings.mouser_api_key = "key"
            mock_settings.oemsecrets_api_key = ""
            mock_settings.sourcengine_api_key = ""
            mock_settings.element14_api_key = ""
            mock_settings.tme_api_token = ""
            mock_settings.tme_api_secret = ""
            mock_settings.anthropic_api_key = ""
            mock_settings.azure_client_id = ""
            mock_settings.azure_client_secret = ""
            mock_settings.azure_tenant_id = ""

            result = log_connector_status()

        assert result["Nexar (Octopart)"] is True
        assert result["Mouser"] is True
        assert result["BrokerBin"] is False
