"""test_connector_status.py — Tests for app/connector_status.py.

Covers uncovered line 35: when all connectors are disabled.
"""

from unittest.mock import patch

# Every credential attribute log_connector_status() inspects, defaulted to empty
# (disabled). Individual tests override only the credentials they want set.
_ALL_CREDENTIALS = (
    "nexar_client_id",
    "nexar_client_secret",
    "brokerbin_api_key",
    "brokerbin_api_secret",
    "ebay_client_id",
    "ebay_client_secret",
    "digikey_client_id",
    "digikey_client_secret",
    "mouser_api_key",
    "oemsecrets_api_key",
    "sourcengine_api_key",
    "element14_api_key",
    "anthropic_api_key",
    "azure_client_id",
    "azure_client_secret",
    "azure_tenant_id",
)


def _configure_settings(mock_settings, **overrides):
    """Set all credentials to empty (disabled), then apply the given overrides."""
    for name in _ALL_CREDENTIALS:
        setattr(mock_settings, name, overrides.get(name, ""))


class TestLogConnectorStatus:
    def test_all_disabled_returns_dict(self):
        """When no credentials are set, all connectors are disabled (line 35)."""
        from app.connector_status import log_connector_status

        with patch("app.connector_status.settings") as mock_settings:
            _configure_settings(mock_settings)

            result = log_connector_status()

        assert isinstance(result, dict)
        assert all(v is False for v in result.values())

    def test_some_enabled(self):
        """When some credentials are set, those connectors are enabled."""
        from app.connector_status import log_connector_status

        with patch("app.connector_status.settings") as mock_settings:
            _configure_settings(
                mock_settings,
                nexar_client_id="id",
                nexar_client_secret="secret",
                mouser_api_key="key",
            )

            result = log_connector_status()

        assert result["Nexar (Octopart)"] is True
        assert result["Mouser"] is True
        assert result["BrokerBin"] is False
