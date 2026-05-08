"""Tests for app/connectors/errors.py — connector exception hierarchy."""

import pytest

from app.connectors.errors import (
    ConnectorAuthError,
    ConnectorError,
    ConnectorQuotaError,
    ConnectorRateLimitError,
)


class TestConnectorErrorHierarchy:
    """All connector hard-error types must inherit from ConnectorError, which in turn
    inherits from RuntimeError so existing catches still work."""

    def test_connector_error_is_runtime_error(self):
        assert issubclass(ConnectorError, RuntimeError)

    def test_auth_error_is_connector_error(self):
        assert issubclass(ConnectorAuthError, ConnectorError)

    def test_rate_limit_error_is_connector_error(self):
        assert issubclass(ConnectorRateLimitError, ConnectorError)

    def test_quota_error_is_connector_error(self):
        assert issubclass(ConnectorQuotaError, ConnectorError)

    def test_specific_types_are_distinct(self):
        """Operator code branches on the specific type to produce distinct messages —
        auth (rotate creds) vs rate-limit (wait) vs quota (upgrade plan).

        The types must not overlap.
        """
        assert not issubclass(ConnectorAuthError, ConnectorRateLimitError)
        assert not issubclass(ConnectorRateLimitError, ConnectorAuthError)
        assert not issubclass(ConnectorQuotaError, ConnectorAuthError)
        assert not issubclass(ConnectorQuotaError, ConnectorRateLimitError)

    def test_message_propagates(self):
        with pytest.raises(ConnectorAuthError, match="DigiKey auth"):
            raise ConnectorAuthError("DigiKey auth error: HTTP 401 unauthorized")

    def test_runtime_error_catch_still_catches(self):
        """Backward-compat: existing `except RuntimeError` and `except Exception`
        catches should continue to catch the new types."""
        with pytest.raises(RuntimeError):
            raise ConnectorAuthError("test")
        with pytest.raises(Exception):
            raise ConnectorRateLimitError("test")
