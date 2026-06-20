"""Tests for the worker liveness watchdog (app/jobs/worker_liveness_jobs.py).

Alerts (debounced) when a worker that should be running has a stale heartbeat
(hung/crashed) or an open circuit breaker; stays quiet otherwise.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from app.jobs.worker_liveness_jobs import _job_monitor_worker_heartbeats
from app.models import IcsWorkerStatus


def _run(db_session):
    """Run the watchdog job against the test DB with no debounce; return the Teams mock."""
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
    _ics(db_session, is_running=True,
         last_heartbeat=datetime.now(timezone.utc) - timedelta(minutes=20))
    teams = _run(db_session)
    teams.assert_awaited_once()
    assert "ICS" in teams.await_args.args[0]


def test_fresh_worker_no_alert(db_session):
    _ics(db_session, is_running=True, last_heartbeat=datetime.now(timezone.utc))
    teams = _run(db_session)
    teams.assert_not_awaited()


def test_not_running_no_alert(db_session):
    _ics(db_session, is_running=False,
         last_heartbeat=datetime.now(timezone.utc) - timedelta(hours=5))
    teams = _run(db_session)
    teams.assert_not_awaited()


def test_breaker_open_alerts(db_session):
    _ics(db_session, is_running=True, last_heartbeat=datetime.now(timezone.utc),
         circuit_breaker_open=True, circuit_breaker_reason="Captcha detected")
    teams = _run(db_session)
    teams.assert_awaited_once()
    assert "circuit breaker is OPEN" in teams.await_args.args[0]


def test_missing_rows_no_error(db_session):
    teams = _run(db_session)  # no singletons seeded
    teams.assert_not_awaited()


def test_debounce_suppresses_repeat(db_session):
    _ics(db_session, is_running=True,
         last_heartbeat=datetime.now(timezone.utc) - timedelta(minutes=20))
    teams = AsyncMock()
    with (
        patch("app.database.SessionLocal", return_value=db_session),
        patch("app.cache.intel_cache.get_cached", return_value={"alerted": 1}),  # already alerted
        patch("app.cache.intel_cache.set_cached"),
        patch("app.services.teams_notifications.post_teams_channel", teams),
    ):
        asyncio.run(_job_monitor_worker_heartbeats())
    teams.assert_not_awaited()
