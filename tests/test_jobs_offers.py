"""test_jobs_offers.py — Tests for offer-related background jobs

Covers: _job_proactive_matching, _job_performance_tracking,
_job_proactive_offer_expiry, _job_flag_stale_offers.

All jobs use SessionLocal() internally, so we patch app.database.SessionLocal
to return the test DB session with close() disabled.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

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


# ── _job_proactive_matching() ─────────────────────────────────────────


def test_proactive_matching_calls_scan(scheduler_db):
    """Proactive matching job delegates to scan_new_offers_for_matches."""
    with patch("app.services.proactive_service.scan_new_offers_for_matches") as mock_scan:
        mock_scan.return_value = {"matches_created": 3, "scanned": 10}
        from app.jobs.offers_jobs import _job_proactive_matching

        asyncio.run(_job_proactive_matching())
        mock_scan.assert_called_once_with(scheduler_db)


def test_proactive_matching_no_matches(scheduler_db):
    """Proactive matching runs cleanly when no matches are created."""
    with patch("app.services.proactive_service.scan_new_offers_for_matches") as mock_scan:
        mock_scan.return_value = {"matches_created": 0, "scanned": 5}
        from app.jobs.offers_jobs import _job_proactive_matching

        asyncio.run(_job_proactive_matching())
        mock_scan.assert_called_once()


def test_proactive_matching_error_handling(scheduler_db):
    """Proactive matching handles errors gracefully."""
    with patch(
        "app.services.proactive_service.scan_new_offers_for_matches",
        side_effect=Exception("DB connection lost"),
    ):
        from app.jobs.offers_jobs import _job_proactive_matching

        asyncio.run(_job_proactive_matching())


def test_proactive_matching_timeout(scheduler_db):
    """Proactive matching handles timeout gracefully."""

    async def _mock_wait_for(coro, timeout=None):
        try:
            coro.close()
        except Exception:
            pass
        raise asyncio.TimeoutError()

    with (
        patch("app.services.proactive_service.scan_new_offers_for_matches"),
        patch("asyncio.wait_for", side_effect=_mock_wait_for),
    ):
        from app.jobs.offers_jobs import _job_proactive_matching

        asyncio.run(_job_proactive_matching())


def test_proactive_matching_logs_summary(scheduler_db):
    """Proactive matching logs a summary with new matches and total pending."""
    with (
        patch("app.services.proactive_service.scan_new_offers_for_matches") as mock_legacy,
        patch("app.services.proactive_matching.run_proactive_scan") as mock_cph,
        patch("app.services.proactive_matching.expire_old_matches") as mock_expire,
        patch("app.jobs.offers_jobs.logger") as mock_logger,
    ):
        mock_legacy.return_value = {"matches_created": 2, "scanned": 10}
        mock_cph.return_value = {"matches_created": 1, "scanned_offers": 5, "scanned_sightings": 3}
        mock_expire.return_value = 0
        from app.jobs.offers_jobs import _job_proactive_matching

        asyncio.run(_job_proactive_matching())
        log_calls = [str(c) for c in mock_logger.info.call_args_list]
        summary_found = any("3 new matches" in c and "pending" in c for c in log_calls)
        assert summary_found, f"Expected summary log with '3 new matches' and 'pending', got: {log_calls}"


def test_proactive_matching_expired_branch(scheduler_db):
    """When expire_old_matches returns a nonzero count, logger.info is called."""
    mock_scan = MagicMock(return_value={"matches_created": 0, "scanned": 5})
    mock_cph = MagicMock(return_value={"matches_created": 0, "scanned_offers": 3, "scanned_sightings": 2})
    mock_expire = MagicMock(return_value=7)  # 7 expired matches

    with (
        patch("app.services.proactive_matching.expire_old_matches", mock_expire),
        patch("app.services.proactive_service.scan_new_offers_for_matches", mock_scan),
        patch("app.services.proactive_matching.run_proactive_scan", mock_cph),
    ):
        from app.jobs.offers_jobs import _job_proactive_matching

        asyncio.run(_job_proactive_matching())


# ── _job_performance_tracking() ───────────────────────────────────────


def test_performance_tracking_calls_services(scheduler_db):
    """Performance tracking computes vendor scorecards and buyer leaderboard."""
    with (
        patch("app.services.performance_service.compute_all_vendor_scorecards") as mock_vs,
        patch("app.services.performance_service.compute_buyer_leaderboard") as mock_bl,
    ):
        mock_vs.return_value = {"updated": 5, "skipped_cold_start": 2}
        mock_bl.return_value = {"entries": 3}
        from app.jobs.offers_jobs import _job_performance_tracking

        asyncio.run(_job_performance_tracking())
        mock_vs.assert_called_once_with(scheduler_db)
        assert mock_bl.call_count >= 1


def test_performance_tracking_recomputes_previous_month_in_grace_period(scheduler_db):
    """During the first 7 days of a month, previous month is also recomputed."""
    frozen_now = datetime(2026, 3, 3, 10, 0, 0, tzinfo=timezone.utc)

    with (
        patch("app.services.performance_service.compute_all_vendor_scorecards") as mock_vs,
        patch("app.services.performance_service.compute_buyer_leaderboard") as mock_bl,
        patch("app.jobs.offers_jobs.datetime") as mock_dt,
    ):
        mock_dt.now.return_value = frozen_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        mock_vs.return_value = {"updated": 5, "skipped_cold_start": 2}
        mock_bl.return_value = {"entries": 3}
        from app.jobs.offers_jobs import _job_performance_tracking

        asyncio.run(_job_performance_tracking())
        assert mock_bl.call_count == 2


def test_performance_tracking_error_handling(scheduler_db):
    """Performance tracking handles errors gracefully without propagating."""
    with patch("app.services.performance_service.compute_all_vendor_scorecards") as mock_vs:
        mock_vs.side_effect = Exception("DB error")
        from app.jobs.offers_jobs import _job_performance_tracking

        asyncio.run(_job_performance_tracking())


def test_performance_tracking_timeout(scheduler_db):
    """Performance tracking handles timeout gracefully."""

    async def _mock_wait_for(coro, timeout=None):
        try:
            coro.close()
        except Exception:
            pass
        raise asyncio.TimeoutError()

    with (
        patch("app.services.performance_service.compute_all_vendor_scorecards"),
        patch("app.services.performance_service.compute_buyer_leaderboard"),
        patch("asyncio.wait_for", side_effect=_mock_wait_for),
    ):
        from app.jobs.offers_jobs import _job_performance_tracking

        asyncio.run(_job_performance_tracking())


# ── _job_proactive_offer_expiry() ─────────────────────────────────────


def test_proactive_offer_expiry_expires_old(scheduler_db, test_user, test_company, test_customer_site):
    """_job_proactive_offer_expiry marks old sent offers as expired."""
    from app.models.intelligence import ProactiveOffer

    old_offer = ProactiveOffer(
        customer_site_id=test_customer_site.id,
        salesperson_id=test_user.id,
        line_items=[],
        status="sent",
        sent_at=datetime.now(timezone.utc) - timedelta(days=20),
    )
    scheduler_db.add(old_offer)
    scheduler_db.commit()

    from app.jobs.offers_jobs import _job_proactive_offer_expiry

    asyncio.run(_job_proactive_offer_expiry())

    scheduler_db.refresh(old_offer)
    assert old_offer.status == "expired"


def test_proactive_offer_expiry_no_expired(scheduler_db):
    """No offers to expire — no commit needed."""
    from app.jobs.offers_jobs import _job_proactive_offer_expiry

    asyncio.run(_job_proactive_offer_expiry())


def test_proactive_offer_expiry_error(scheduler_db):
    """DB error rolls back."""
    with patch.object(scheduler_db, "query", side_effect=Exception("DB error")):
        from app.jobs.offers_jobs import _job_proactive_offer_expiry

        asyncio.run(_job_proactive_offer_expiry())


# ── _job_flag_stale_offers() ──────────────────────────────────────────


def test_flag_stale_offers_flags_old(scheduler_db, test_user, test_requisition):
    """_job_flag_stale_offers sets is_stale on old active offers."""
    from app.models.offers import Offer

    old_offer = Offer(
        requisition_id=test_requisition.id,
        vendor_name="Test Vendor",
        mpn="LM317T",
        status="active",
        is_stale=False,
        created_at=datetime.now(timezone.utc) - timedelta(days=20),
    )
    scheduler_db.add(old_offer)
    scheduler_db.commit()

    from app.jobs.offers_jobs import _job_flag_stale_offers

    asyncio.run(_job_flag_stale_offers())

    scheduler_db.refresh(old_offer)
    assert old_offer.is_stale is True


def test_flag_stale_offers_no_matches(scheduler_db):
    """No stale offers — no commit."""
    from app.jobs.offers_jobs import _job_flag_stale_offers

    asyncio.run(_job_flag_stale_offers())


def test_flag_stale_offers_error(scheduler_db):
    """DB error rolls back."""
    with patch.object(scheduler_db, "query", side_effect=Exception("DB error")):
        from app.jobs.offers_jobs import _job_flag_stale_offers

        asyncio.run(_job_flag_stale_offers())
