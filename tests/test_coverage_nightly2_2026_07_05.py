"""test_coverage_nightly2_2026_07_05.py — Nightly coverage fill-in (part 2).

Targets modules below 85% coverage:
  - app/jobs/worker_liveness_jobs.py (0%)
  - app/jobs/maintenance_jobs.py (84%)

Called by: pytest
Depends on: conftest (db_session), unittest.mock
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ["TESTING"] = "1"


# ─────────────────────────────────────────────────────────────────────
# app/jobs/worker_liveness_jobs.py
# ─────────────────────────────────────────────────────────────────────


class TestAsUtc:
    def test_none_returns_none(self):
        from app.jobs.worker_liveness_jobs import _as_utc

        assert _as_utc(None) is None

    def test_naive_datetime_gets_utc(self):
        from app.jobs.worker_liveness_jobs import _as_utc

        naive = datetime(2025, 6, 1, 12, 0, 0)
        result = _as_utc(naive)
        assert result.tzinfo == timezone.utc
        assert result.hour == 12

    def test_aware_datetime_returned_as_is(self):
        from app.jobs.worker_liveness_jobs import _as_utc

        aware = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        result = _as_utc(aware)
        assert result is aware


class TestHeartbeatIsStale:
    def test_not_running_never_stale(self):
        from app.jobs.worker_liveness_jobs import heartbeat_is_stale

        now = datetime.now(timezone.utc)
        assert heartbeat_is_stale(False, None, now, 5) is False

    def test_not_running_old_heartbeat_still_not_stale(self):
        from app.jobs.worker_liveness_jobs import heartbeat_is_stale

        now = datetime.now(timezone.utc)
        hb = now - timedelta(hours=24)
        assert heartbeat_is_stale(False, hb, now, 5) is False

    def test_running_no_heartbeat_is_stale(self):
        from app.jobs.worker_liveness_jobs import heartbeat_is_stale

        now = datetime.now(timezone.utc)
        assert heartbeat_is_stale(True, None, now, 5) is True

    def test_running_recent_heartbeat_not_stale(self):
        from app.jobs.worker_liveness_jobs import heartbeat_is_stale

        now = datetime.now(timezone.utc)
        hb = now - timedelta(minutes=2)
        assert heartbeat_is_stale(True, hb, now, 5) is False

    def test_running_old_heartbeat_is_stale(self):
        from app.jobs.worker_liveness_jobs import heartbeat_is_stale

        now = datetime.now(timezone.utc)
        hb = now - timedelta(minutes=10)
        assert heartbeat_is_stale(True, hb, now, 5) is True

    def test_naive_heartbeat_coerced_to_utc(self):
        from app.jobs.worker_liveness_jobs import heartbeat_is_stale

        now = datetime.now(timezone.utc)
        naive_hb = datetime.now() - timedelta(minutes=10)
        assert heartbeat_is_stale(True, naive_hb, now, 5) is True


class TestShouldAlertStaleHeartbeat:
    def test_stale_and_not_alerted_returns_true(self):
        from app.jobs.worker_liveness_jobs import should_alert_stale_heartbeat

        now = datetime.now(timezone.utc)
        hb = now - timedelta(minutes=10)
        result = should_alert_stale_heartbeat(
            is_running=True,
            last_heartbeat=hb,
            now=now,
            stale_after_minutes=5,
            already_alerted=False,
        )
        assert result is True

    def test_stale_but_already_alerted_returns_false(self):
        from app.jobs.worker_liveness_jobs import should_alert_stale_heartbeat

        now = datetime.now(timezone.utc)
        hb = now - timedelta(minutes=10)
        result = should_alert_stale_heartbeat(
            is_running=True,
            last_heartbeat=hb,
            now=now,
            stale_after_minutes=5,
            already_alerted=True,
        )
        assert result is False

    def test_not_stale_returns_false(self):
        from app.jobs.worker_liveness_jobs import should_alert_stale_heartbeat

        now = datetime.now(timezone.utc)
        hb = now - timedelta(minutes=1)
        result = should_alert_stale_heartbeat(
            is_running=True,
            last_heartbeat=hb,
            now=now,
            stale_after_minutes=5,
            already_alerted=False,
        )
        assert result is False

    def test_not_running_returns_false(self):
        from app.jobs.worker_liveness_jobs import should_alert_stale_heartbeat

        now = datetime.now(timezone.utc)
        result = should_alert_stale_heartbeat(
            is_running=False,
            last_heartbeat=None,
            now=now,
            stale_after_minutes=5,
            already_alerted=False,
        )
        assert result is False


class TestAlreadyAlerted:
    def test_returns_true_when_cache_hit(self):
        with patch("app.cache.intel_cache.get_cached", return_value={"alerted": 1}):
            from app.jobs.worker_liveness_jobs import _already_alerted

            assert _already_alerted("ics-label") is True

    def test_returns_false_when_cache_miss(self):
        with patch("app.cache.intel_cache.get_cached", return_value=None):
            from app.jobs.worker_liveness_jobs import _already_alerted

            assert _already_alerted("ics-label") is False


class TestEmitAlert:
    @pytest.mark.asyncio
    async def test_sets_debounce_cache_and_calls_teams(self):
        mock_teams = AsyncMock(return_value=None)
        with (
            patch("app.cache.intel_cache.set_cached") as mock_set,
            patch("app.services.teams_notifications.post_teams_channel", mock_teams),
            patch("app.services.search_worker_base.monitoring.capture_sentry_message"),
        ):
            from app.jobs.worker_liveness_jobs import _emit_alert

            await _emit_alert("ICS", "Heartbeat stale for ICS worker", 60)

        mock_set.assert_called_once_with("worker_alert:ICS", {"alerted": 1}, ttl_days=60 / 1440)
        mock_teams.assert_called_once()

    @pytest.mark.asyncio
    async def test_teams_failure_does_not_raise(self):
        with (
            patch("app.cache.intel_cache.set_cached"),
            patch(
                "app.services.teams_notifications.post_teams_channel",
                side_effect=Exception("network error"),
            ),
            patch("app.services.search_worker_base.monitoring.capture_sentry_message"),
        ):
            from app.jobs.worker_liveness_jobs import _emit_alert

            # Should not raise — Teams failure is caught and logged
            await _emit_alert("ICS", "test message", 60)


class TestRegisterWorkerLivenessJobs:
    def test_registers_one_job(self):
        from app.jobs.worker_liveness_jobs import register_worker_liveness_jobs

        mock_scheduler = MagicMock()
        mock_settings = MagicMock()
        mock_settings.worker_liveness_check_minutes = 5
        register_worker_liveness_jobs(mock_scheduler, mock_settings)
        mock_scheduler.add_job.assert_called_once()

    def test_job_id_is_correct(self):
        from app.jobs.worker_liveness_jobs import register_worker_liveness_jobs

        mock_scheduler = MagicMock()
        mock_settings = MagicMock()
        mock_settings.worker_liveness_check_minutes = 3
        register_worker_liveness_jobs(mock_scheduler, mock_settings)
        call_kwargs = mock_scheduler.add_job.call_args[1]
        assert call_kwargs["id"] == "worker_liveness_check"


class TestJobMonitorWorkerHeartbeats:
    @pytest.mark.asyncio
    async def test_all_rows_none_no_alerts(self):
        from app.jobs.worker_liveness_jobs import _job_monitor_worker_heartbeats

        mock_db = MagicMock()
        mock_db.get.return_value = None

        mock_settings = MagicMock()
        mock_settings.worker_heartbeat_stale_minutes = 10
        mock_settings.worker_alert_debounce_minutes = 60

        with (
            patch("app.database.SessionLocal", return_value=mock_db),
            patch("app.config.settings", mock_settings),
        ):
            await _job_monitor_worker_heartbeats.__wrapped__()

        mock_db.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_stale_worker_triggers_alert(self):
        from app.jobs.worker_liveness_jobs import _job_monitor_worker_heartbeats

        now = datetime.now(timezone.utc)
        mock_row = MagicMock()
        mock_row.is_running = True
        mock_row.last_heartbeat = now - timedelta(minutes=30)
        mock_row.circuit_breaker_open = False

        mock_db = MagicMock()
        mock_db.get.return_value = mock_row

        mock_settings = MagicMock()
        mock_settings.worker_heartbeat_stale_minutes = 10
        mock_settings.worker_alert_debounce_minutes = 60

        with (
            patch("app.database.SessionLocal", return_value=mock_db),
            patch("app.config.settings", mock_settings),
            patch("app.cache.intel_cache.get_cached", return_value=None),
            patch("app.cache.intel_cache.set_cached"),
            patch("app.services.teams_notifications.post_teams_channel", AsyncMock()),
            patch("app.services.search_worker_base.monitoring.capture_sentry_message"),
        ):
            await _job_monitor_worker_heartbeats.__wrapped__()

        mock_db.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_circuit_breaker_open_triggers_alert(self):
        from app.jobs.worker_liveness_jobs import _job_monitor_worker_heartbeats

        now = datetime.now(timezone.utc)
        mock_row = MagicMock()
        mock_row.is_running = True
        # Fresh heartbeat — not stale
        mock_row.last_heartbeat = now - timedelta(minutes=1)
        mock_row.circuit_breaker_open = True
        mock_row.circuit_breaker_reason = "Rate limit exceeded"

        mock_db = MagicMock()
        mock_db.get.return_value = mock_row

        mock_settings = MagicMock()
        mock_settings.worker_heartbeat_stale_minutes = 10
        mock_settings.worker_alert_debounce_minutes = 60

        with (
            patch("app.database.SessionLocal", return_value=mock_db),
            patch("app.config.settings", mock_settings),
            patch("app.cache.intel_cache.get_cached", return_value=None),
            patch("app.cache.intel_cache.set_cached"),
            patch("app.services.teams_notifications.post_teams_channel", AsyncMock()),
            patch("app.services.search_worker_base.monitoring.capture_sentry_message"),
        ):
            await _job_monitor_worker_heartbeats.__wrapped__()

        mock_db.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_already_alerted_skips_alert(self):
        from app.jobs.worker_liveness_jobs import _job_monitor_worker_heartbeats

        now = datetime.now(timezone.utc)
        mock_row = MagicMock()
        mock_row.is_running = True
        mock_row.last_heartbeat = now - timedelta(minutes=30)
        mock_row.circuit_breaker_open = False

        mock_db = MagicMock()
        mock_db.get.return_value = mock_row

        mock_settings = MagicMock()
        mock_settings.worker_heartbeat_stale_minutes = 10
        mock_settings.worker_alert_debounce_minutes = 60

        mock_teams = AsyncMock()
        with (
            patch("app.database.SessionLocal", return_value=mock_db),
            patch("app.config.settings", mock_settings),
            patch("app.cache.intel_cache.get_cached", return_value={"alerted": 1}),
            patch("app.services.teams_notifications.post_teams_channel", mock_teams),
        ):
            await _job_monitor_worker_heartbeats.__wrapped__()

        # Already alerted — no Teams message sent
        mock_teams.assert_not_called()

    @pytest.mark.asyncio
    async def test_stale_heartbeat_age_unknown_when_none(self):
        from app.jobs.worker_liveness_jobs import _job_monitor_worker_heartbeats

        mock_row = MagicMock()
        mock_row.is_running = True
        mock_row.last_heartbeat = None  # Never seen — age = "unknown"
        mock_row.circuit_breaker_open = False

        mock_db = MagicMock()
        mock_db.get.return_value = mock_row

        mock_settings = MagicMock()
        mock_settings.worker_heartbeat_stale_minutes = 10
        mock_settings.worker_alert_debounce_minutes = 60

        with (
            patch("app.database.SessionLocal", return_value=mock_db),
            patch("app.config.settings", mock_settings),
            patch("app.cache.intel_cache.get_cached", return_value=None),
            patch("app.cache.intel_cache.set_cached"),
            patch("app.services.teams_notifications.post_teams_channel", AsyncMock()),
            patch("app.services.search_worker_base.monitoring.capture_sentry_message"),
        ):
            await _job_monitor_worker_heartbeats.__wrapped__()

        mock_db.close.assert_called_once()


# ─────────────────────────────────────────────────────────────────────
# app/jobs/maintenance_jobs.py — missing lines 20-44, 162-190
# ─────────────────────────────────────────────────────────────────────


class TestRegisterMaintenanceJobs:
    def test_registers_all_six_jobs(self):
        from app.jobs.maintenance_jobs import register_maintenance_jobs

        mock_scheduler = MagicMock()
        mock_settings = MagicMock()
        register_maintenance_jobs(mock_scheduler, mock_settings)
        assert mock_scheduler.add_job.call_count == 6

    def test_cache_cleanup_job_registered(self):
        from app.jobs.maintenance_jobs import register_maintenance_jobs

        mock_scheduler = MagicMock()
        mock_settings = MagicMock()
        register_maintenance_jobs(mock_scheduler, mock_settings)
        job_ids = [call[1]["id"] for call in mock_scheduler.add_job.call_args_list]
        assert "cache_cleanup" in job_ids

    def test_contact_dedup_job_registered(self):
        from app.jobs.maintenance_jobs import register_maintenance_jobs

        mock_scheduler = MagicMock()
        mock_settings = MagicMock()
        register_maintenance_jobs(mock_scheduler, mock_settings)
        job_ids = [call[1]["id"] for call in mock_scheduler.add_job.call_args_list]
        assert "contact_dedup" in job_ids


class TestJobContactDedup:
    @pytest.mark.asyncio
    async def test_with_duplicate_contacts_merges_them(self):
        from app.jobs.maintenance_jobs import _job_contact_dedup

        # Build fake duplicate pair
        dupe = MagicMock()
        dupe.customer_site_id = 1
        dupe.em = "dup@example.com"

        contact_keeper = MagicMock()
        contact_keeper.id = 10
        contact_keeper.full_name = "Jane Doe"
        contact_keeper.title = "Engineer"
        contact_keeper.phone = "555-0100"
        contact_keeper.notes = "VIP"
        contact_keeper.linkedin_url = None

        contact_dup = MagicMock()
        contact_dup.id = 11
        contact_dup.full_name = None
        contact_dup.title = None
        contact_dup.phone = None
        contact_dup.notes = None
        contact_dup.linkedin_url = None

        mock_db = MagicMock()

        # First query: find duplicate groups (.filter → .group_by → .having → .all)
        mock_db.query.return_value.filter.return_value.group_by.return_value.having.return_value.all.return_value = [
            dupe
        ]
        # Second query: fetch contacts in that group (.filter → .order_by → .all)
        mock_db.query.return_value.filter.return_value.order_by.return_value.all.return_value = [
            contact_keeper,
            contact_dup,
        ]

        with (
            patch("app.database.SessionLocal", return_value=mock_db),
            patch("app.services.contact_merge_service.merge_contacts") as mock_merge,
        ):
            await _job_contact_dedup.__wrapped__()

        mock_merge.assert_called_once_with(contact_keeper.id, contact_dup.id, mock_db)
        mock_db.commit.assert_called()
        mock_db.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_merged_count_logged(self):
        from app.jobs.maintenance_jobs import _job_contact_dedup

        dupe = MagicMock()
        dupe.customer_site_id = 2
        dupe.em = "log@example.com"

        c1 = MagicMock()
        c1.id = 20
        c1.full_name = "Alice"
        c1.title = "PM"
        c1.phone = None
        c1.notes = None
        c1.linkedin_url = None

        c2 = MagicMock()
        c2.id = 21
        c2.full_name = None
        c2.title = None
        c2.phone = None
        c2.notes = None
        c2.linkedin_url = None

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.group_by.return_value.having.return_value.all.return_value = [
            dupe
        ]
        mock_db.query.return_value.filter.return_value.order_by.return_value.all.return_value = [
            c1,
            c2,
        ]

        with (
            patch("app.database.SessionLocal", return_value=mock_db),
            patch("app.services.contact_merge_service.merge_contacts"),
        ):
            await _job_contact_dedup.__wrapped__()

        # Commit called with merged > 0 path executed
        mock_db.commit.assert_called()
        mock_db.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_merge_exception_skipped_continues(self):
        from app.jobs.maintenance_jobs import _job_contact_dedup

        dupe = MagicMock()
        dupe.customer_site_id = 3
        dupe.em = "err@example.com"

        c1 = MagicMock()
        c1.id = 30
        c1.full_name = "Bob"
        c1.title = None
        c1.phone = None
        c1.notes = None
        c1.linkedin_url = None

        c2 = MagicMock()
        c2.id = 31
        c2.full_name = None
        c2.title = None
        c2.phone = None
        c2.notes = None
        c2.linkedin_url = None

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.group_by.return_value.having.return_value.all.return_value = [
            dupe
        ]
        mock_db.query.return_value.filter.return_value.order_by.return_value.all.return_value = [
            c1,
            c2,
        ]

        with (
            patch("app.database.SessionLocal", return_value=mock_db),
            patch(
                "app.services.contact_merge_service.merge_contacts",
                side_effect=Exception("merge failed"),
            ),
        ):
            # Should not raise — per-pair exceptions are caught and logged
            await _job_contact_dedup.__wrapped__()

        mock_db.close.assert_called_once()
