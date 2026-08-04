"""Tests for the worker liveness watchdog (app/jobs/worker_liveness_jobs.py).

Alerts (debounced) when a worker that should be running has a stale heartbeat
(hung/crashed) or an open circuit breaker; stays quiet otherwise. The monitored worker
list is computed once (registration-time credential gate) — the job iterates the stored
list.
"""

import asyncio
import os
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.jobs.worker_liveness_jobs as liveness
from app.jobs.worker_liveness_jobs import (
    _job_monitor_worker_heartbeats,
    _monitored_checks,
    heartbeat_is_stale,
    register_worker_liveness_jobs,
    should_alert_stale_heartbeat,
)
from app.models import IcsWorkerStatus

NOW = datetime(2026, 6, 28, 12, 0, tzinfo=UTC)

# The watchdog only monitors workers whose credentials are configured (keys-off
# honesty — a stack without ICS creds runs no ICS worker, so its restored
# is_running singleton must not alert). Tests below simulate a CONFIGURED
# deployment unless they test the skip itself.
_ICS_CONFIGURED = {"ICS_USERNAME": "test-user", "ICS_PASSWORD": "test-pass"}

_ALL_CRED_VARS = (
    "ICS_USERNAME",
    "ICS_PASSWORD",
    "NC_ACCOUNT_NUMBER",
    "NC_USERNAME",
    "NC_PASSWORD",
    "TBF_USERNAME",
    "TBF_PASSWORD",
)


@pytest.fixture(autouse=True)
def _reset_checks():
    """Restore the module-level monitored list after each test."""
    saved = list(liveness._checks)
    yield
    liveness._checks[:] = saved


def _run(db_session, env=_ICS_CONFIGURED):
    """Recompute the monitored list under ``env``, run the watchdog job against the test
    DB with no debounce; return the Teams mock."""
    teams = AsyncMock()
    with (
        patch.dict(os.environ, env, clear=False),
        patch("app.database.SessionLocal", return_value=db_session),
        patch("app.cache.intel_cache.get_cached", return_value=None),
        patch("app.cache.intel_cache.set_cached"),
        patch("app.services.teams_notifications.post_teams_channel", teams),
    ):
        liveness._checks[:] = _monitored_checks()
        asyncio.run(_job_monitor_worker_heartbeats())
    return teams


def _ics(db_session, **kwargs):
    row = IcsWorkerStatus(id=1, **kwargs)
    db_session.add(row)
    db_session.commit()
    return row


def _delenv_all_creds(monkeypatch):
    for var in _ALL_CRED_VARS:
        monkeypatch.delenv(var, raising=False)


def test_stale_running_worker_alerts(db_session):
    _ics(db_session, is_running=True, last_heartbeat=datetime.now(UTC) - timedelta(minutes=20))
    teams = _run(db_session)
    teams.assert_awaited_once()
    assert "ICS" in teams.await_args.args[0]


def test_fresh_worker_no_alert(db_session):
    _ics(db_session, is_running=True, last_heartbeat=datetime.now(UTC))
    teams = _run(db_session)
    teams.assert_not_awaited()


def test_not_running_no_alert(db_session):
    _ics(db_session, is_running=False, last_heartbeat=datetime.now(UTC) - timedelta(hours=5))
    teams = _run(db_session)
    teams.assert_not_awaited()


def test_breaker_open_alerts(db_session):
    _ics(
        db_session,
        is_running=True,
        last_heartbeat=datetime.now(UTC),
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
    _ics(db_session, is_running=True, last_heartbeat=datetime.now(UTC) - timedelta(minutes=20))
    teams = AsyncMock()
    with (
        patch.dict(os.environ, _ICS_CONFIGURED, clear=False),
        patch("app.database.SessionLocal", return_value=db_session),
        patch("app.cache.intel_cache.get_cached", return_value={"alerted": 1}),  # already alerted
        patch("app.cache.intel_cache.set_cached"),
        patch("app.services.teams_notifications.post_teams_channel", teams),
    ):
        liveness._checks[:] = _monitored_checks()
        asyncio.run(_job_monitor_worker_heartbeats())
    teams.assert_not_awaited()


# ── Registration-time credential gate (W1.16 48h-gate) ───────────────────


def test_unconfigured_worker_not_monitored(db_session, monkeypatch):
    # W1.16 (48h-gate): a restored prod copy says ICS is_running=true, but this
    # deployment has no ICS credentials — the watchdog must skip, not alert.
    _delenv_all_creds(monkeypatch)
    _ics(db_session, is_running=True, last_heartbeat=datetime.now(UTC) - timedelta(hours=5))
    teams = _run(db_session, env={})
    teams.assert_not_awaited()


def test_registration_stores_monitored_list_once(db_session, monkeypatch):
    # No scrape-worker credentials: registration stores Enrichment only, and a
    # second job run against a stale unconfigured ICS row still stays quiet
    # (the gate lives in the stored list, not per-run env reads).
    _delenv_all_creds(monkeypatch)
    _ics(db_session, is_running=True, last_heartbeat=None)

    scheduler = MagicMock()
    register_worker_liveness_jobs(scheduler, MagicMock(worker_liveness_check_minutes=5))
    scheduler.add_job.assert_called_once()
    assert [label for label, _ in liveness._checks] == ["Enrichment"]

    for _ in range(2):
        teams = AsyncMock()
        with (
            patch("app.database.SessionLocal", return_value=db_session),
            patch("app.cache.intel_cache.get_cached", return_value=None),
            patch("app.cache.intel_cache.set_cached"),
            patch("app.services.teams_notifications.post_teams_channel", teams),
        ):
            asyncio.run(_job_monitor_worker_heartbeats())
        teams.assert_not_awaited()


def test_partial_credentials_not_monitored(monkeypatch):
    # Username without password does NOT satisfy the session manager's
    # is_configured semantics — the worker cannot log in, so it is not monitored.
    _delenv_all_creds(monkeypatch)
    monkeypatch.setenv("ICS_USERNAME", "test-user")
    assert [label for label, _ in _monitored_checks()] == ["Enrichment"]


def test_full_credentials_monitored(monkeypatch):
    _delenv_all_creds(monkeypatch)
    monkeypatch.setenv("ICS_USERNAME", "test-user")
    monkeypatch.setenv("ICS_PASSWORD", "test-pass")
    assert [label for label, _ in _monitored_checks()] == ["ICS", "Enrichment"]


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
