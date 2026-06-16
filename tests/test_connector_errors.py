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

    @pytest.mark.parametrize(
        ("subclass", "base"),
        [
            (ConnectorError, RuntimeError),
            (ConnectorAuthError, ConnectorError),
            (ConnectorRateLimitError, ConnectorError),
            (ConnectorQuotaError, ConnectorError),
        ],
        ids=[
            "connector_error_is_runtime_error",
            "auth_error_is_connector_error",
            "rate_limit_error_is_connector_error",
            "quota_error_is_connector_error",
        ],
    )
    def test_inheritance(self, subclass, base):
        assert issubclass(subclass, base)

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
