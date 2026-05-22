"""test_health_monitor_nightly_coverage.py — Extra coverage for health_monitor.py.

Covers: deep_test_source typed-error branches (auth, rate-limit, quota),
and _redact_api_keys closure edge cases.

Called by: pytest
Depends on: tests/test_health_monitor.py (existing coverage), conftest.py
"""

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

os.environ["TESTING"] = "1"

from app.connectors.errors import ConnectorAuthError, ConnectorQuotaError, ConnectorRateLimitError
from app.models.config import ApiSource, ApiUsageLog
from app.services.health_monitor import _redact_api_keys, deep_test_source


def _make_source(db, **overrides):
    defaults = {
        "name": "test_hm_nightly",
        "display_name": "Test HM Nightly",
        "category": "distributor",
        "source_type": "api",
        "status": "live",
        "is_active": True,
        "calls_this_month": 0,
        "monthly_quota": None,
        "error_count_24h": 0,
    }
    defaults.update(overrides)
    source = ApiSource(**defaults)
    db.add(source)
    db.flush()
    return source


class TestDeepTestSourceTypedErrors:
    """deep_test_source produces distinct error messages for each ConnectorError subtype
    (lines 341-407 in health_monitor.py).
    """

    def test_auth_error_message(self, db_session):
        """ConnectorAuthError in deep_test → error mentions 'rotate credentials'."""
        source = _make_source(db_session, status="live", error_count_24h=0)
        mock_connector = MagicMock()
        mock_connector.search = AsyncMock(side_effect=ConnectorAuthError("bad creds"))

        with patch("app.services.health_monitor._get_connector", return_value=mock_connector):
            with patch("app.services.health_monitor._check_status_transition"):
                result = asyncio.get_event_loop().run_until_complete(deep_test_source(source, db_session))

        assert result["success"] is False
        assert result["results_count"] == 0
        assert source.status == "error"
        msg = (source.last_error or "").lower()
        assert "rotate credentials" in msg or "auth error" in msg

        logs = db_session.query(ApiUsageLog).filter_by(source_id=source.id, endpoint="deep_test").all()
        assert len(logs) == 1
        assert logs[0].success is False

    def test_rate_limit_error_message(self, db_session):
        """ConnectorRateLimitError in deep_test → error mentions 'rate limited' or 'auto-recover'."""
        source = _make_source(db_session, status="live", error_count_24h=0)
        mock_connector = MagicMock()
        mock_connector.search = AsyncMock(side_effect=ConnectorRateLimitError("hit window"))

        with patch("app.services.health_monitor._get_connector", return_value=mock_connector):
            with patch("app.services.health_monitor._check_status_transition"):
                result = asyncio.get_event_loop().run_until_complete(deep_test_source(source, db_session))

        assert result["success"] is False
        assert source.status == "error"
        msg = (source.last_error or "").lower()
        assert "rate limited" in msg or "auto-recover" in msg or "auto recovers" in msg

        logs = db_session.query(ApiUsageLog).filter_by(source_id=source.id, endpoint="deep_test").all()
        assert logs[0].success is False

    def test_quota_error_message(self, db_session):
        """ConnectorQuotaError in deep_test → error mentions 'quota' or 'upgrade plan'."""
        source = _make_source(db_session, status="live", error_count_24h=0)
        mock_connector = MagicMock()
        mock_connector.search = AsyncMock(side_effect=ConnectorQuotaError("plan limit"))

        with patch("app.services.health_monitor._get_connector", return_value=mock_connector):
            with patch("app.services.health_monitor._check_status_transition"):
                result = asyncio.get_event_loop().run_until_complete(deep_test_source(source, db_session))

        assert result["success"] is False
        assert source.status == "error"
        msg = (source.last_error or "").lower()
        assert "quota" in msg or "upgrade" in msg

        logs = db_session.query(ApiUsageLog).filter_by(source_id=source.id, endpoint="deep_test").all()
        assert logs[0].success is False

    def test_typed_error_increments_error_count(self, db_session):
        """Each typed error increments error_count_24h."""
        source = _make_source(db_session, status="live", error_count_24h=5)
        mock_connector = MagicMock()
        mock_connector.search = AsyncMock(side_effect=ConnectorAuthError("expired"))

        with patch("app.services.health_monitor._get_connector", return_value=mock_connector):
            with patch("app.services.health_monitor._check_status_transition"):
                asyncio.get_event_loop().run_until_complete(deep_test_source(source, db_session))

        assert source.error_count_24h == 6


class TestRedactApiKeysClosureBranches:
    """Tests to cover the closure branches in _redact_api_keys."""

    def test_named_key_eight_char_minimum_is_masked(self):
        """The regex requires 8+ chars; this key has exactly 8 and gets masked."""
        text = "api_key=ABCDEFGH"
        result = _redact_api_keys(text)
        assert "ABCDEFGH" not in result
        assert "***" in result

    def test_bare_key_over_100_chars_not_masked_in_url(self):
        """Keys >100 chars in query strings with non-named param are left unmasked (line 73).

        Uses 'data=' prefix which is NOT in _API_KEY_RE's prefix list, so the bare-key
        masking path applies. The key is 105 A's (>100), triggering the early return.
        """
        long_key = "A" * 105
        text = f"https://api.com/search?data={long_key}&other=val"
        result = _redact_api_keys(text)
        # Long bare key should NOT be masked (>100 char guard, line 73)
        assert long_key in result

    def test_bare_key_20_to_100_chars_is_masked_in_url(self):
        """Keys 20-100 chars in URL query strings ARE masked."""
        medium_key = "B" * 30
        text = f"https://api.example.com/v1?apikey={medium_key}"
        result = _redact_api_keys(text)
        assert medium_key not in result or "***" in result

    def test_ampersand_triggers_url_masking(self):
        """URLs with & (no ?) also trigger bare key masking."""
        key = "C" * 25
        text = f"search?q=hello&key={key}"
        result = _redact_api_keys(text)
        # The bare key should be masked since it's in a query string
        assert key not in result
