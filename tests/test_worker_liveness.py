"""Tests for the worker liveness watchdog (app/jobs/worker_liveness_jobs.py).

Alerts (debounced) when a worker that should be running has a stale heartbeat
(hung/crashed) or an open circuit breaker; stays quiet otherwise.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from app.jobs.worker_liveness_jobs import (
    _job_monitor_worker_heartbeats,
    heartbeat_is_stale,
    should_alert_stale_heartbeat,
)
from app.models import IcsWorkerStatus

NOW = datetime(2026, 6, 28, 12, 0, tzinfo=timezone.utc)


def _run(db_session):
    """Run the watchdog job against the test DB with no debounce; return the Teams
    mock."""
    teams = AsyncMock()
    with (
        patch("app.database.SessionLocal", return_value=db_session),
        patch("app.cache.intel_cache.get_cached", return_value=None),
        patch("app.cache.intel_cache.set_cached"),
        patch("app.services.teams_notifications.post_teams_channel", teams),
    ):
        asyncio.run(_job_monitor_worker_heartbeats())
    return teams


def _ics(db_session, **kwargs):
    row = IcsWorkerStatus(id=1, **kwargs)
    db_session.add(row)
    db_session.commit()
    return row


def test_stale_running_worker_alerts(db_session):
    _ics(db_session, is_running=True, last_heartbeat=datetime.now(timezone.utc) - timedelta(minutes=20))
    teams = _run(db_session)
    teams.assert_awaited_once()
    assert "ICS" in teams.await_args.args[0]


def test_fresh_worker_no_alert(db_session):
    _ics(db_session, is_running=True, last_heartbeat=datetime.now(timezone.utc))
    teams = _run(db_session)
    teams.assert_not_awaited()


def test_not_running_no_alert(db_session):
    _ics(db_session, is_running=False, last_heartbeat=datetime.now(timezone.utc) - timedelta(hours=5))
    teams = _run(db_session)
    teams.assert_not_awaited()


def test_breaker_open_alerts(db_session):
    _ics(
        db_session,
        is_running=True,
        last_heartbeat=datetime.now(timezone.utc),
        circuit_breaker_open=True,
        circuit_breaker_reason="Captcha detected",
    )
    teams = _run(db_session)
    teams.assert_awaited_once()
    assert "circuit breaker is OPEN" in teams.await_args.args[0]


def test_missing_rows_no_error(db_session):
    teams = _run(db_session)  # no singletons seeded
    teams.assert_not_awaited()


def test_debounce_suppresses_repeat(db_session):
    _ics(db_session, is_running=True, last_heartbeat=datetime.now(timezone.utc) - timedelta(minutes=20))
    teams = AsyncMock()
    with (
        patch("app.database.SessionLocal", return_value=db_session),
        patch("app.cache.intel_cache.get_cached", return_value={"alerted": 1}),  # already alerted
        patch("app.cache.intel_cache.set_cached"),
        patch("app.services.teams_notifications.post_teams_channel", teams),
    ):
        asyncio.run(_job_monitor_worker_heartbeats())
    teams.assert_not_awaited()


def test_null_heartbeat_treated_as_stale(db_session):
    # A running worker that never wrote a heartbeat (NULL) is stale → alert.
    _ics(db_session, is_running=True, last_heartbeat=None)
    teams = _run(db_session)
    teams.assert_awaited_once()
    assert "ICS" in teams.await_args.args[0]


# ── Pure decision function — no DB / scheduler / IO ──────────────────────


class TestHeartbeatIsStale:
    """heartbeat_is_stale() — pure staleness predicate (the branch selector)."""

    def test_fresh_running_worker_not_stale(self):
        assert heartbeat_is_stale(True, NOW - timedelta(minutes=2), NOW, 15) is False

    def test_old_running_worker_is_stale(self):
        assert heartbeat_is_stale(True, NOW - timedelta(minutes=20), NOW, 15) is True

    def test_null_heartbeat_is_stale(self):
        assert heartbeat_is_stale(True, None, NOW, 15) is True

    def test_stopped_worker_never_stale(self):
        # Clean shutdown sets is_running=False — silence is expected, not a fault.
        assert heartbeat_is_stale(False, None, NOW, 15) is False
        assert heartbeat_is_stale(False, NOW - timedelta(hours=5), NOW, 15) is False

    def test_naive_heartbeat_coerced_to_utc(self):
        # ICS stores naive timestamps; they must compare correctly against UTC now.
        naive = (NOW - timedelta(minutes=20)).replace(tzinfo=None)
        assert heartbeat_is_stale(True, naive, NOW, 15) is True


class TestShouldAlertStaleHeartbeat:
    """should_alert_stale_heartbeat() — pure staleness + debounce emit gate."""

    def _decide(self, **overrides):
        kwargs = {
            "is_running": True,
            "last_heartbeat": NOW - timedelta(minutes=20),
            "now": NOW,
            "stale_after_minutes": 15,
            "already_alerted": False,
        }
        kwargs.update(overrides)
        return should_alert_stale_heartbeat(**kwargs)

    def test_stale_and_not_yet_alerted_emits(self):
        assert self._decide() is True

    def test_null_heartbeat_emits(self):
        assert self._decide(last_heartbeat=None) is True

    def test_fresh_does_not_emit(self):
        assert self._decide(last_heartbeat=NOW - timedelta(minutes=2)) is False

    def test_debounce_suppresses_repeat(self):
        # Stale, but already alerted within the window → stay quiet.
        assert self._decide(already_alerted=True) is False

    def test_stopped_worker_does_not_emit(self):
        assert self._decide(is_running=False) is False
