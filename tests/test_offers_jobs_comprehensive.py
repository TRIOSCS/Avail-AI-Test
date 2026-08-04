"""test_offers_jobs_comprehensive.py — Comprehensive tests for app/jobs/offers_jobs.py.

Covers: register_offers_jobs (only the flag-gated proactive Teams push remains
registrable) and _job_performance_tracking (parked implementation, kept).

Called by: pytest
Depends on: app.jobs.offers_jobs, conftest fixtures
"""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.scheduler import scheduler

# ── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture()
def scheduler_db(db_session: Session):
    """Patch SessionLocal so scheduler jobs use the test DB."""
    original_close = db_session.close
    db_session.close = lambda: None
    with patch("app.database.SessionLocal", return_value=db_session):
        yield db_session
    db_session.close = original_close


@pytest.fixture(autouse=True)
def _clear_scheduler_jobs():
    """Remove all jobs before/after each test to prevent leakage."""
    for job in scheduler.get_jobs():
        job.remove()
    yield
    for job in scheduler.get_jobs():
        job.remove()


# ── register_offers_jobs() ─────────────────────────────────────────────


class TestRegisterOffersJobs:
    """Tests for register_offers_jobs after the Wave-1 park/delete pass."""

    def test_parked_and_deleted_jobs_never_registered(self):
        """Even with proactive matching flagged on, only the Teams push is
        registrable."""
        mock_scheduler = MagicMock()
        mock_settings = MagicMock()
        mock_settings.proactive_matching_enabled = True
        mock_settings.proactive_teams_push_enabled = False
        mock_settings.proactive_scan_interval_hours = 4

        from app.jobs.offers_jobs import register_offers_jobs

        register_offers_jobs(mock_scheduler, mock_settings)

        assert mock_scheduler.add_job.call_count == 0

    def test_only_teams_push_registers_on_explicit_true(self):
        """proactive_teams_push is the sole remaining registration, flag-gated."""
        mock_scheduler = MagicMock()
        mock_settings = MagicMock()
        mock_settings.proactive_teams_push_enabled = True
        mock_settings.proactive_scan_interval_hours = 4

        from app.jobs.offers_jobs import register_offers_jobs

        register_offers_jobs(mock_scheduler, mock_settings)

        job_ids = [c.kwargs.get("id") for c in mock_scheduler.add_job.call_args_list]
        assert job_ids == ["proactive_teams_push"]

    def test_push_interval_minimum_1_hour(self):
        """Teams push interval has a floor of 1 hour."""
        mock_scheduler = MagicMock()
        mock_settings = MagicMock()
        mock_settings.proactive_teams_push_enabled = True
        mock_settings.proactive_scan_interval_hours = 0  # Below minimum

        from app.jobs.offers_jobs import register_offers_jobs

        register_offers_jobs(mock_scheduler, mock_settings)

        first_call = mock_scheduler.add_job.call_args_list[0]
        trigger = first_call[0][1] if len(first_call[0]) > 1 else first_call[1].get("trigger")
        # IntervalTrigger with hours=1 (minimum)
        assert trigger.interval == timedelta(hours=1)


# ── _job_performance_tracking() ────────────────────────────────────────


class TestPerformanceTracking:
    """Tests for _job_performance_tracking."""

    def test_performance_tracking_happy_path(self, scheduler_db):
        """Performance tracking calls all scoring services."""
        # Fix datetime to day > 7 to avoid grace-period double-call
        fixed_now = datetime(2026, 4, 15, 12, 0, 0, tzinfo=UTC)
        with (
            patch("app.jobs.offers_jobs.datetime") as mock_dt,
            patch("app.services.vendor_scorecard.compute_all_vendor_scorecards") as mock_vs,
            patch("app.services.buyer_leaderboard.compute_buyer_leaderboard") as mock_bl,
            patch("app.services.avail_score_service.compute_all_avail_scores") as mock_as,
            patch("app.services.multiplier_score_service.compute_all_multiplier_scores") as mock_ms,
            patch("app.services.unified_score_service.compute_all_unified_scores") as mock_us,
        ):
            mock_dt.now.return_value = fixed_now
            mock_dt.side_effect = lambda *args, **kw: datetime(*args, **kw)
            mock_vs.return_value = {"updated": 10, "skipped_cold_start": 2}
            mock_bl.return_value = {"entries": 5}
            mock_as.return_value = {"buyers": 3, "sales": 2, "saved": 5}
            mock_ms.return_value = {"buyers": 3, "sales": 2, "saved": 5}
            mock_us.return_value = {"computed": 5, "saved": 5}

            from app.jobs.offers_jobs import _job_performance_tracking

            asyncio.run(_job_performance_tracking())

            mock_vs.assert_called_once()
            mock_bl.assert_called_once()
            mock_as.assert_called_once()
            mock_ms.assert_called_once()
            mock_us.assert_called_once()

    def test_performance_tracking_timeout(self, scheduler_db):
        """Performance tracking handles TimeoutError gracefully."""

        async def _mock_wait_for(coro, timeout=None):
            try:
                coro.close()
            except Exception:
                pass
            raise TimeoutError()

        with (
            patch("app.services.vendor_scorecard.compute_all_vendor_scorecards"),
            patch("asyncio.wait_for", side_effect=_mock_wait_for),
        ):
            from app.jobs.offers_jobs import _job_performance_tracking

            # Should not raise — timeout is caught internally
            asyncio.run(_job_performance_tracking())

    def test_performance_tracking_generic_error(self, scheduler_db):
        """Performance tracking handles generic errors gracefully."""
        with patch(
            "app.services.vendor_scorecard.compute_all_vendor_scorecards",
            side_effect=Exception("scoring error"),
        ):
            from app.jobs.offers_jobs import _job_performance_tracking

            # Should not raise — error is caught internally
            asyncio.run(_job_performance_tracking())

    def test_performance_tracking_grace_period_recompute(self, scheduler_db):
        """During first 7 days of month, previous month is also recomputed."""
        # Force now.day <= 7
        fixed_now = datetime(2026, 3, 3, 12, 0, 0, tzinfo=UTC)

        with (
            patch("app.services.vendor_scorecard.compute_all_vendor_scorecards") as mock_vs,
            patch("app.services.buyer_leaderboard.compute_buyer_leaderboard") as mock_bl,
            patch("app.services.avail_score_service.compute_all_avail_scores") as mock_as,
            patch("app.services.multiplier_score_service.compute_all_multiplier_scores") as mock_ms,
            patch("app.services.unified_score_service.compute_all_unified_scores") as mock_us,
            patch("app.jobs.offers_jobs.datetime") as mock_dt,
        ):
            mock_dt.now.return_value = fixed_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            mock_vs.return_value = {"updated": 10, "skipped_cold_start": 2}
            mock_bl.return_value = {"entries": 5}
            mock_as.return_value = {"buyers": 3, "sales": 2, "saved": 5}
            mock_ms.return_value = {"buyers": 3, "sales": 2, "saved": 5}
            mock_us.return_value = {"computed": 5, "saved": 5}

            from app.jobs.offers_jobs import _job_performance_tracking

            asyncio.run(_job_performance_tracking())

            # buyer_leaderboard called twice: current + previous month
            assert mock_bl.call_count == 2
            assert mock_as.call_count == 2
            assert mock_ms.call_count == 2
            assert mock_us.call_count == 2
