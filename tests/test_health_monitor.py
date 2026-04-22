"""Tests for app/services/health_monitor.py — API health check service.

Called by: pytest
Depends on: conftest fixtures, unittest.mock
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.orm import Session

from app.models.config import ApiSource, ApiUsageLog
from app.services.health_monitor import (
    _check_quota_threshold,
    _check_status_transition,
    _redact_api_keys,
    deep_test_source,
    ping_source,
    run_health_checks,
)

# ── Helpers ──────────────────────────────────────────────────────────


def _make_source(db: Session, **overrides) -> ApiSource:
    """Create an ApiSource with sensible defaults."""
    defaults = {
        "name": "test_source",
        "display_name": "Test Source",
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


# ── _redact_api_keys tests ───────────────────────────────────────────


class TestRedactApiKeys:
    def test_none_input(self):
        assert _redact_api_keys(None) is None

    def test_empty_string(self):
        assert _redact_api_keys("") == ""

    def test_no_keys_present(self):
        text = "Connection timed out after 30s"
        assert _redact_api_keys(text) == text

    def test_named_key_pattern(self):
        text = "api_key=ABCDEFGHIJKLMNOP"
        result = _redact_api_keys(text)
        assert "ABCDEFGHIJKLMNOP" not in result
        assert "***" in result

    def test_token_pattern(self):
        text = 'token="sk_live_1234567890abcdef"'
        result = _redact_api_keys(text)
        assert "1234567890abcdef" not in result
        assert "***" in result

    def test_short_key_not_redacted(self):
        # Keys <= 4 chars should not be masked
        text = "api_key=ABCD"
        result = _redact_api_keys(text)
        assert result == text

    def test_bare_key_in_url(self):
        text = "https://api.example.com/search?key=ABCDEFGHIJKLMNOPQRSTUVWXYZ&q=test"
        result = _redact_api_keys(text)
        assert "ABCDEFGHIJKLMNOPQRSTUVWXYZ" not in result

    def test_no_bare_key_without_query_string(self):
        text = "Plain text with no URL query string"
        result = _redact_api_keys(text)
        assert result == text

    def test_very_long_bare_token_not_masked(self):
        # Tokens > 100 chars are skipped by bare key masking
        long_key = "A" * 110
        text = f"https://api.com?data={long_key}"
        result = _redact_api_keys(text)
        assert long_key in result


# ── _check_status_transition tests ───────────────────────────────────


class TestCheckStatusTransition:
    def test_live_to_error_notifies(self, db_session):
        source = _make_source(db_session, status="live")
        with patch("app.services.health_monitor._notify_admins") as mock_notify:
            _check_status_transition(source, "live", "error", db_session, "timeout")
            mock_notify.assert_called_once()
            call_kwargs = mock_notify.call_args
            assert call_kwargs[1]["event_type"] == "api_source_down"

    def test_error_to_error_no_notification(self, db_session):
        source = _make_source(db_session, status="error")
        with patch("app.services.health_monitor._notify_admins") as mock_notify:
            _check_status_transition(source, "error", "error", db_session, "timeout")
            mock_notify.assert_not_called()

    def test_live_to_live_no_notification(self, db_session):
        source = _make_source(db_session)
        with patch("app.services.health_monitor._notify_admins") as mock_notify:
            _check_status_transition(source, "live", "live", db_session)
            mock_notify.assert_not_called()

    def test_error_msg_redacted(self, db_session):
        source = _make_source(db_session, status="live")
        with patch("app.services.health_monitor._notify_admins") as mock_notify:
            _check_status_transition(source, "live", "error", db_session, "api_key=SECRET12345678")
            mock_notify.assert_called_once()
            body = mock_notify.call_args[1]["body"]
            assert "SECRET12345678" not in body

    def test_none_error_msg(self, db_session):
        source = _make_source(db_session, status="live")
        with patch("app.services.health_monitor._notify_admins") as mock_notify:
            _check_status_transition(source, "live", "error", db_session, None)
            mock_notify.assert_called_once()
            assert "unknown" in mock_notify.call_args[1]["body"]


# ── _check_quota_threshold tests ─────────────────────────────────────


class TestCheckQuotaThreshold:
    def test_no_monthly_quota(self, db_session):
        source = _make_source(db_session, monthly_quota=None)
        with patch("app.services.health_monitor._notify_admins") as mock_notify:
            _check_quota_threshold(source, db_session)
            mock_notify.assert_not_called()

    def test_zero_quota(self, db_session):
        source = _make_source(db_session, monthly_quota=0)
        with patch("app.services.health_monitor._notify_admins") as mock_notify:
            _check_quota_threshold(source, db_session)
            mock_notify.assert_not_called()

    def test_below_warning_threshold(self, db_session):
        source = _make_source(db_session, monthly_quota=100, calls_this_month=50)
        with patch("app.services.health_monitor._notify_admins") as mock_notify:
            _check_quota_threshold(source, db_session)
            mock_notify.assert_not_called()

    def test_warning_threshold(self, db_session):
        source = _make_source(db_session, monthly_quota=100, calls_this_month=85)
        with patch("app.services.health_monitor._notify_admins") as mock_notify:
            _check_quota_threshold(source, db_session)
            mock_notify.assert_called_once()
            assert mock_notify.call_args[1]["event_type"] == "api_quota_warning"

    def test_critical_threshold(self, db_session):
        source = _make_source(db_session, monthly_quota=100, calls_this_month=96)
        with patch("app.services.health_monitor._notify_admins") as mock_notify:
            _check_quota_threshold(source, db_session)
            mock_notify.assert_called_once()
            assert mock_notify.call_args[1]["event_type"] == "api_quota_critical"

    def test_exactly_at_warning(self, db_session):
        source = _make_source(db_session, monthly_quota=100, calls_this_month=80)
        with patch("app.services.health_monitor._notify_admins") as mock_notify:
            _check_quota_threshold(source, db_session)
            mock_notify.assert_called_once()
            assert mock_notify.call_args[1]["event_type"] == "api_quota_warning"


# ── ping_source tests ────────────────────────────────────────────────


class TestPingSource:
    def test_no_connector_sets_disabled(self, db_session):
        source = _make_source(db_session, status="live")
        with patch("app.services.health_monitor._get_connector", return_value=None):
            result = asyncio.get_event_loop().run_until_complete(ping_source(source, db_session))
        assert result["success"] is False
        assert result["error"] == "No connector available"
        assert source.status == "disabled"

    def test_successful_ping(self, db_session):
        source = _make_source(db_session, status="pending", calls_this_month=0)
        mock_connector = MagicMock()
        mock_connector.search = AsyncMock(return_value=[{"mpn": "LM317"}])

        with patch("app.services.health_monitor._get_connector", return_value=mock_connector):
            result = asyncio.get_event_loop().run_until_complete(ping_source(source, db_session))

        assert result["success"] is True
        assert result["error"] is None
        assert result["elapsed_ms"] >= 0
        assert source.status == "live"
        assert source.last_error is None
        assert source.calls_this_month == 1

        # Verify usage log was created
        logs = db_session.query(ApiUsageLog).filter_by(source_id=source.id).all()
        assert len(logs) == 1
        assert logs[0].check_type == "ping"
        assert logs[0].success is True

    def test_failed_ping(self, db_session):
        source = _make_source(db_session, status="live", error_count_24h=0)
        mock_connector = MagicMock()
        mock_connector.search = AsyncMock(side_effect=ConnectionError("timeout"))

        with patch("app.services.health_monitor._get_connector", return_value=mock_connector):
            with patch("app.services.health_monitor._check_status_transition"):
                result = asyncio.get_event_loop().run_until_complete(ping_source(source, db_session))

        assert result["success"] is False
        assert "timeout" in result["error"]
        assert source.status == "error"
        assert source.error_count_24h == 1

        # Verify failed usage log
        logs = db_session.query(ApiUsageLog).filter_by(source_id=source.id).all()
        assert len(logs) == 1
        assert logs[0].success is False
        assert "timeout" in logs[0].error_message

    def test_ping_increments_calls_this_month(self, db_session):
        source = _make_source(db_session, calls_this_month=10)
        mock_connector = MagicMock()
        mock_connector.search = AsyncMock(return_value=[])

        with patch("app.services.health_monitor._get_connector", return_value=mock_connector):
            asyncio.get_event_loop().run_until_complete(ping_source(source, db_session))

        assert source.calls_this_month == 11

    def test_ping_checks_quota(self, db_session):
        source = _make_source(db_session, monthly_quota=100, calls_this_month=94)
        mock_connector = MagicMock()
        mock_connector.search = AsyncMock(return_value=[])

        with patch("app.services.health_monitor._get_connector", return_value=mock_connector):
            with patch("app.services.health_monitor._check_quota_threshold") as mock_quota:
                asyncio.get_event_loop().run_until_complete(ping_source(source, db_session))
                mock_quota.assert_called_once()

    def test_ping_truncates_long_error(self, db_session):
        source = _make_source(db_session, status="live")
        long_error = "x" * 1000
        mock_connector = MagicMock()
        mock_connector.search = AsyncMock(side_effect=Exception(long_error))

        with patch("app.services.health_monitor._get_connector", return_value=mock_connector):
            with patch("app.services.health_monitor._check_status_transition"):
                result = asyncio.get_event_loop().run_until_complete(ping_source(source, db_session))

        assert len(result["error"]) <= 500


# ── deep_test_source tests ───────────────────────────────────────────


class TestDeepTestSource:
    def test_no_connector(self, db_session):
        source = _make_source(db_session, status="live")
        with patch("app.services.health_monitor._get_connector", return_value=None):
            result = asyncio.get_event_loop().run_until_complete(deep_test_source(source, db_session))

        assert result["success"] is False
        assert result["error"] == "No connector"
        assert result["results_count"] == 0
        assert source.status == "disabled"

        # Verify log entry for no-connector case
        logs = db_session.query(ApiUsageLog).filter_by(source_id=source.id).all()
        assert len(logs) == 1
        assert logs[0].check_type == "deep"

    def test_successful_deep_test(self, db_session):
        source = _make_source(db_session, status="pending", calls_this_month=5)
        mock_connector = MagicMock()
        mock_connector.search = AsyncMock(return_value=[{"mpn": "LM317"}, {"mpn": "LM317T"}])

        with patch("app.services.health_monitor._get_connector", return_value=mock_connector):
            result = asyncio.get_event_loop().run_until_complete(deep_test_source(source, db_session))

        assert result["success"] is True
        assert result["results_count"] == 2
        assert result["error"] is None
        assert source.status == "live"
        assert source.calls_this_month == 6

        logs = db_session.query(ApiUsageLog).filter_by(source_id=source.id).all()
        assert len(logs) == 1
        assert logs[0].endpoint == "deep_test"
        assert logs[0].success is True

    def test_failed_deep_test(self, db_session):
        source = _make_source(db_session, status="live", error_count_24h=2)
        mock_connector = MagicMock()
        mock_connector.search = AsyncMock(side_effect=RuntimeError("API rate limited"))

        with patch("app.services.health_monitor._get_connector", return_value=mock_connector):
            with patch("app.services.health_monitor._check_status_transition"):
                result = asyncio.get_event_loop().run_until_complete(deep_test_source(source, db_session))

        assert result["success"] is False
        assert result["results_count"] == 0
        assert "rate limited" in result["error"]
        assert source.status == "error"
        assert source.error_count_24h == 3

        logs = db_session.query(ApiUsageLog).filter_by(source_id=source.id).all()
        assert len(logs) == 1
        assert logs[0].success is False

    def test_deep_test_checks_quota(self, db_session):
        source = _make_source(db_session, monthly_quota=100, calls_this_month=94)
        mock_connector = MagicMock()
        mock_connector.search = AsyncMock(return_value=[])

        with patch("app.services.health_monitor._get_connector", return_value=mock_connector):
            with patch("app.services.health_monitor._check_quota_threshold") as mock_q:
                asyncio.get_event_loop().run_until_complete(deep_test_source(source, db_session))
                mock_q.assert_called_once()


# ── run_health_checks tests ──────────────────────────────────────────


class TestRunHealthChecks:
    def test_ping_check_type(self, db_session):
        source = _make_source(db_session, name="source_a", is_active=True)
        mock_connector = MagicMock()
        mock_connector.search = AsyncMock(return_value=[])

        mock_session = MagicMock(spec=Session)
        mock_session.query.return_value.filter.return_value.all.return_value = [source]
        mock_session.commit = MagicMock()
        mock_session.rollback = MagicMock()
        mock_session.close = MagicMock()

        with patch("app.database.SessionLocal", return_value=mock_session):
            with patch("app.services.health_monitor.ping_source", new_callable=AsyncMock) as mock_ping:
                mock_ping.return_value = {"success": True, "elapsed_ms": 50, "error": None}
                result = asyncio.get_event_loop().run_until_complete(run_health_checks("ping"))

        assert result["total"] == 1
        assert result["passed"] == 1
        assert result["failed"] == 0
        mock_ping.assert_called_once()

    def test_deep_check_type(self, db_session):
        source = _make_source(db_session, name="source_b", is_active=True)

        mock_session = MagicMock(spec=Session)
        mock_session.query.return_value.filter.return_value.all.return_value = [source]
        mock_session.commit = MagicMock()
        mock_session.close = MagicMock()

        with patch("app.database.SessionLocal", return_value=mock_session):
            with patch("app.services.health_monitor.deep_test_source", new_callable=AsyncMock) as mock_deep:
                mock_deep.return_value = {"success": False, "results_count": 0, "elapsed_ms": 0, "error": "fail"}
                result = asyncio.get_event_loop().run_until_complete(run_health_checks("deep"))

        assert result["failed"] == 1
        mock_deep.assert_called_once()

    def test_no_active_sources(self):
        mock_session = MagicMock(spec=Session)
        mock_session.query.return_value.filter.return_value.all.return_value = []
        mock_session.commit = MagicMock()
        mock_session.close = MagicMock()

        with patch("app.database.SessionLocal", return_value=mock_session):
            result = asyncio.get_event_loop().run_until_complete(run_health_checks("ping"))

        assert result["total"] == 0
        assert result["passed"] == 0
        assert result["failed"] == 0

    def test_source_check_crash_counted_as_failure(self):
        source = MagicMock()
        source.name = "broken_source"

        mock_session = MagicMock(spec=Session)
        mock_session.query.return_value.filter.return_value.all.return_value = [source]
        mock_session.commit = MagicMock()
        mock_session.close = MagicMock()

        with patch("app.database.SessionLocal", return_value=mock_session):
            with patch("app.services.health_monitor.ping_source", new_callable=AsyncMock) as mock_ping:
                mock_ping.side_effect = Exception("unexpected crash")
                result = asyncio.get_event_loop().run_until_complete(run_health_checks("ping"))

        assert result["failed"] == 1
        assert result["passed"] == 0

    def test_db_error_rolls_back(self):
        mock_session = MagicMock(spec=Session)
        mock_session.query.side_effect = Exception("DB connection lost")
        mock_session.rollback = MagicMock()
        mock_session.close = MagicMock()

        with patch("app.database.SessionLocal", return_value=mock_session):
            result = asyncio.get_event_loop().run_until_complete(run_health_checks("ping"))

        mock_session.rollback.assert_called_once()
        mock_session.close.assert_called_once()

    def test_commits_per_source_not_once_at_end(self):
        """Each source commits independently so api_sources row locks release between
        iterations — otherwise the search path hits LockNotAvailable (see Phase 3 root-
        cause analysis)."""
        sources = []
        for i in range(1, 4):
            s = MagicMock()
            s.name = f"source_{i}"
            sources.append(s)

        mock_session = MagicMock(spec=Session)
        mock_session.query.return_value.filter.return_value.all.return_value = sources
        mock_session.close = MagicMock()

        commit_count = 0

        def _count_commits():
            nonlocal commit_count
            commit_count += 1

        mock_session.commit.side_effect = _count_commits

        with patch("app.database.SessionLocal", return_value=mock_session):
            with patch("app.services.health_monitor.ping_source", new_callable=AsyncMock) as mock_ping:
                mock_ping.return_value = {"success": True, "elapsed_ms": 50, "error": None}
                asyncio.get_event_loop().run_until_complete(run_health_checks("ping"))

        assert commit_count == 3, (
            f"expected one commit per source (3), got {commit_count} — locks on api_sources "
            f"would be held for the whole run instead of released between iterations"
        )

    def test_source_failure_does_not_rollback_siblings(self):
        """When one source's check raises, earlier-committed sources persist and later
        sources still run.

        Under the old single-transaction design, a mid-loop exception rolled back every
        source's changes.
        """
        s1 = MagicMock()
        s1.name = "source_1"
        s2 = MagicMock()
        s2.name = "source_2"
        s3 = MagicMock()
        s3.name = "source_3"

        mock_session = MagicMock(spec=Session)
        mock_session.query.return_value.filter.return_value.all.return_value = [s1, s2, s3]
        mock_session.close = MagicMock()

        commit_count = 0
        rollback_count = 0

        def _count_commits():
            nonlocal commit_count
            commit_count += 1

        def _count_rollbacks():
            nonlocal rollback_count
            rollback_count += 1

        mock_session.commit.side_effect = _count_commits
        mock_session.rollback.side_effect = _count_rollbacks

        with patch("app.database.SessionLocal", return_value=mock_session):
            with patch("app.services.health_monitor.ping_source", new_callable=AsyncMock) as mock_ping:
                mock_ping.side_effect = [
                    {"success": True, "elapsed_ms": 50, "error": None},
                    Exception("connector blew up"),
                    {"success": True, "elapsed_ms": 60, "error": None},
                ]
                result = asyncio.get_event_loop().run_until_complete(run_health_checks("ping"))

        assert result["passed"] == 2
        assert result["failed"] == 1
        assert commit_count == 2, f"sources 1 and 3 should have committed independently; got {commit_count} commits"
        assert rollback_count == 1, f"source 2's failure should trigger exactly one rollback; got {rollback_count}"


# ── _get_connector tests ─────────────────────────────────────────────


class TestGetConnector:
    def test_connector_returned(self, db_session):
        from app.services.health_monitor import _get_connector

        source = _make_source(db_session)
        mock_conn = MagicMock()

        with patch(
            "app.routers.sources._get_connector_for_source",
            return_value=mock_conn,
        ):
            result = _get_connector(source, db_session)
        assert result is mock_conn

    def test_connector_exception_returns_none(self, db_session):
        from app.services.health_monitor import _get_connector

        source = _make_source(db_session)

        with patch(
            "app.routers.sources._get_connector_for_source",
            side_effect=ValueError("no config"),
        ):
            result = _get_connector(source, db_session)
        assert result is None


# ── _notify_admins tests ─────────────────────────────────────────────


class TestNotifyAdmins:
    def test_logs_warning_with_body(self, db_session):
        from app.services.health_monitor import _notify_admins

        # Should not raise; just logs
        _notify_admins(db_session, "test_event", "Test title", "Test body")

    def test_logs_warning_without_body(self, db_session):
        from app.services.health_monitor import _notify_admins

        _notify_admins(db_session, "test_event", "Test title")
