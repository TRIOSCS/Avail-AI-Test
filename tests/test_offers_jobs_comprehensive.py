"""test_offers_jobs_comprehensive.py — Comprehensive tests for app/jobs/offers_jobs.py.

Covers: register_offers_jobs, _job_proactive_matching, _job_performance_tracking,
_job_proactive_offer_expiry, _job_flag_stale_offers, _job_expire_strategic_vendors,
_job_warn_strategic_expiring.

Called by: pytest
Depends on: app.jobs.offers_jobs, conftest fixtures
"""

import asyncio
from datetime import datetime, timedelta, timezone
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
    """Tests for register_offers_jobs configuration."""

    def test_registers_all_jobs_proactive_enabled(self):
        """When proactive_matching_enabled=True, all 6 jobs are registered."""
        mock_scheduler = MagicMock()
        mock_settings = MagicMock()
        mock_settings.proactive_matching_enabled = True
        mock_settings.proactive_scan_interval_hours = 4

        from app.jobs.offers_jobs import register_offers_jobs

        register_offers_jobs(mock_scheduler, mock_settings)

        job_ids = [c.kwargs.get("id") for c in mock_scheduler.add_job.call_args_list]
        assert "proactive_matching" in job_ids
        assert "performance_tracking" in job_ids
        assert "proactive_offer_expiry" in job_ids
        assert "flag_stale_offers" in job_ids
        assert "expire_strategic_vendors" in job_ids
        assert "warn_strategic_expiring" in job_ids
        assert mock_scheduler.add_job.call_count == 6

    def test_registers_without_proactive_matching(self):
        """When proactive_matching_enabled=False, proactive_matching job is skipped."""
        mock_scheduler = MagicMock()
        mock_settings = MagicMock()
        mock_settings.proactive_matching_enabled = False

        from app.jobs.offers_jobs import register_offers_jobs

        register_offers_jobs(mock_scheduler, mock_settings)

        job_ids = [c.kwargs.get("id") for c in mock_scheduler.add_job.call_args_list]
        assert "proactive_matching" not in job_ids
        assert mock_scheduler.add_job.call_count == 5

    def test_proactive_interval_minimum_1_hour(self):
        """Proactive scan interval has a floor of 1 hour."""
        mock_scheduler = MagicMock()
        mock_settings = MagicMock()
        mock_settings.proactive_matching_enabled = True
        mock_settings.proactive_scan_interval_hours = 0  # Below minimum

        from app.jobs.offers_jobs import register_offers_jobs

        register_offers_jobs(mock_scheduler, mock_settings)

        # The first add_job call is for proactive_matching
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
        fixed_now = datetime(2026, 4, 15, 12, 0, 0, tzinfo=timezone.utc)
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
            raise asyncio.TimeoutError()

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
        fixed_now = datetime(2026, 3, 3, 12, 0, 0, tzinfo=timezone.utc)

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


# ── _job_expire_strategic_vendors() ───────────────────────────────────


class TestExpireStrategicVendors:
    """Tests for _job_expire_strategic_vendors."""

    def test_expire_strategic_vendors_happy_path(self, scheduler_db):
        """Expire strategic vendors delegates to service."""
        with patch("app.services.strategic_vendor_service.expire_stale", return_value=3) as mock_expire:
            from app.jobs.offers_jobs import _job_expire_strategic_vendors

            asyncio.run(_job_expire_strategic_vendors())
            mock_expire.assert_called_once_with(scheduler_db)

    def test_expire_strategic_vendors_none_expired(self, scheduler_db):
        """No vendors to expire — runs cleanly."""
        with patch("app.services.strategic_vendor_service.expire_stale", return_value=0):
            from app.jobs.offers_jobs import _job_expire_strategic_vendors

            asyncio.run(_job_expire_strategic_vendors())

    def test_expire_strategic_vendors_error(self, scheduler_db):
        """Error in expire_stale is caught and rolled back."""
        with patch(
            "app.services.strategic_vendor_service.expire_stale",
            side_effect=Exception("DB error"),
        ):
            from app.jobs.offers_jobs import _job_expire_strategic_vendors

            # Should not raise — error is caught internally
            asyncio.run(_job_expire_strategic_vendors())


# ── _job_warn_strategic_expiring() ────────────────────────────────────


class TestWarnStrategicExpiring:
    """Tests for _job_warn_strategic_expiring."""

    def test_warn_no_expiring_vendors(self, scheduler_db):
        """No vendors expiring — runs cleanly."""
        with patch("app.services.strategic_vendor_service.get_expiring_soon", return_value=[]):
            from app.jobs.offers_jobs import _job_warn_strategic_expiring

            asyncio.run(_job_warn_strategic_expiring())

    def test_warn_creates_activity_log(self, scheduler_db, test_user, test_vendor_card):
        """Creates ActivityLog entries for expiring strategic vendors."""
        from app.models import ActivityLog

        mock_sv = MagicMock()
        mock_sv.id = 99
        mock_sv.user_id = test_user.id
        mock_sv.expires_at = datetime.now(timezone.utc) + timedelta(days=3)
        mock_sv.vendor_card = test_vendor_card

        with patch("app.services.strategic_vendor_service.get_expiring_soon", return_value=[mock_sv]):
            from app.jobs.offers_jobs import _job_warn_strategic_expiring

            asyncio.run(_job_warn_strategic_expiring())

        log = scheduler_db.query(ActivityLog).filter(ActivityLog.activity_type == "strategic_vendor_expiring").first()
        assert log is not None
        assert "Arrow Electronics" in log.subject
        assert "expires in" in log.subject

    def test_warn_deduplicates_alerts(self, scheduler_db, test_user, test_vendor_card):
        """Does not create duplicate alerts for the same strategic vendor."""
        from app.models import ActivityLog

        # Create existing alert
        existing = ActivityLog(
            user_id=test_user.id,
            activity_type="strategic_vendor_expiring",
            channel="system",
            external_id="99",
        )
        scheduler_db.add(existing)
        scheduler_db.commit()

        mock_sv = MagicMock()
        mock_sv.id = 99
        mock_sv.user_id = test_user.id
        mock_sv.expires_at = datetime.now(timezone.utc) + timedelta(days=3)
        mock_sv.vendor_card = test_vendor_card

        with patch("app.services.strategic_vendor_service.get_expiring_soon", return_value=[mock_sv]):
            from app.jobs.offers_jobs import _job_warn_strategic_expiring

            asyncio.run(_job_warn_strategic_expiring())

        count = scheduler_db.query(ActivityLog).filter(ActivityLog.activity_type == "strategic_vendor_expiring").count()
        assert count == 1  # No duplicate created

    def test_warn_naive_timezone_handling(self, scheduler_db, test_user, test_vendor_card):
        """Handles naive datetime (no tzinfo) on expires_at."""
        mock_sv = MagicMock()
        mock_sv.id = 100
        mock_sv.user_id = test_user.id
        mock_sv.expires_at = datetime(2026, 4, 1, 12, 0, 0)  # Naive datetime
        mock_sv.vendor_card = test_vendor_card

        with patch("app.services.strategic_vendor_service.get_expiring_soon", return_value=[mock_sv]):
            from app.jobs.offers_jobs import _job_warn_strategic_expiring

            asyncio.run(_job_warn_strategic_expiring())

    def test_warn_vendor_card_none(self, scheduler_db, test_user):
        """Handles missing vendor_card gracefully (shows 'Unknown')."""
        from app.models import ActivityLog

        mock_sv = MagicMock()
        mock_sv.id = 101
        mock_sv.user_id = test_user.id
        mock_sv.expires_at = datetime.now(timezone.utc) + timedelta(days=2)
        mock_sv.vendor_card = None

        with patch("app.services.strategic_vendor_service.get_expiring_soon", return_value=[mock_sv]):
            from app.jobs.offers_jobs import _job_warn_strategic_expiring

            asyncio.run(_job_warn_strategic_expiring())

        log = scheduler_db.query(ActivityLog).filter(ActivityLog.activity_type == "strategic_vendor_expiring").first()
        assert log is not None
        assert "Unknown" in log.subject

    def test_warn_error_handling(self, scheduler_db):
        """Error in get_expiring_soon is caught and rolled back."""
        with patch(
            "app.services.strategic_vendor_service.get_expiring_soon",
            side_effect=Exception("DB error"),
        ):
            from app.jobs.offers_jobs import _job_warn_strategic_expiring

            # Should not raise — error caught internally
            asyncio.run(_job_warn_strategic_expiring())


# ── _job_proactive_offer_expiry() — additional tests ──────────────────


class TestProactiveOfferExpiryAdditional:
    """Additional coverage for _job_proactive_offer_expiry."""

    def test_recent_sent_offer_not_expired(self, scheduler_db, test_user, test_customer_site):
        """Sent offers younger than 14 days are NOT expired."""
        from app.models.intelligence import ProactiveOffer

        recent_offer = ProactiveOffer(
            customer_site_id=test_customer_site.id,
            salesperson_id=test_user.id,
            line_items=[],
            status="sent",
            sent_at=datetime.now(timezone.utc) - timedelta(days=5),
        )
        scheduler_db.add(recent_offer)
        scheduler_db.commit()

        from app.jobs.offers_jobs import _job_proactive_offer_expiry

        asyncio.run(_job_proactive_offer_expiry())

        scheduler_db.refresh(recent_offer)
        assert recent_offer.status == "sent"  # Not expired


# ── _job_flag_stale_offers() — additional tests ───────────────────────


class TestFlagStaleOffersAdditional:
    """Additional coverage for _job_flag_stale_offers."""

    def test_recent_offer_not_flagged(self, scheduler_db, test_user, test_requisition):
        """Active offers younger than 14 days are NOT flagged stale."""
        from app.models.offers import Offer

        recent = Offer(
            requisition_id=test_requisition.id,
            vendor_name="Test Vendor",
            mpn="LM317T",
            status="active",
            is_stale=False,
            created_at=datetime.now(timezone.utc) - timedelta(days=5),
        )
        scheduler_db.add(recent)
        scheduler_db.commit()

        from app.jobs.offers_jobs import _job_flag_stale_offers

        asyncio.run(_job_flag_stale_offers())

        scheduler_db.refresh(recent)
        assert recent.is_stale is False

    def test_already_stale_not_reflagged(self, scheduler_db, test_user, test_requisition):
        """Offers already flagged as stale are not processed again."""
        from app.models.offers import Offer

        already_stale = Offer(
            requisition_id=test_requisition.id,
            vendor_name="Test Vendor",
            mpn="LM317T",
            status="active",
            is_stale=True,
            created_at=datetime.now(timezone.utc) - timedelta(days=20),
        )
        scheduler_db.add(already_stale)
        scheduler_db.commit()

        from app.jobs.offers_jobs import _job_flag_stale_offers

        asyncio.run(_job_flag_stale_offers())

        # No error, still stale
        scheduler_db.refresh(already_stale)
        assert already_stale.is_stale is True
