"""
test_scheduler.py — Tests for APScheduler background jobs

Covers: _utc helper, configure_scheduler registration, and individual job
functions (_job_auto_archive, _job_cache_cleanup, _job_token_refresh,
_job_batch_results, _job_engagement_scoring, _job_proactive_matching,
_job_performance_tracking).

All jobs use SessionLocal() internally, so we patch app.database.SessionLocal
to return the test DB session with close() disabled.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models import Requisition, VendorCard
from app.scheduler import _utc, configure_scheduler, scheduler

# ── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture()
def scheduler_db(db_session: Session):
    """Patch SessionLocal so scheduler jobs use the test DB.

    Jobs do `from .database import SessionLocal; db = SessionLocal()` inside
    each function, so we patch at app.database.SessionLocal.
    """
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


# ── _utc() ─────────────────────────────────────────────────────────────


def test_utc_naive_becomes_utc():
    naive = datetime(2026, 1, 15, 12, 0, 0)
    result = _utc(naive)
    assert result.tzinfo == timezone.utc
    assert result.year == 2026


def test_utc_aware_passthrough():
    tz5 = timezone(timedelta(hours=5))
    aware = datetime(2026, 1, 15, 12, 0, 0, tzinfo=tz5)
    result = _utc(aware)
    assert result.tzinfo == tz5  # unchanged


def test_utc_none_returns_none():
    assert _utc(None) is None


# ── configure_scheduler() ──────────────────────────────────────────────
# settings is imported inside configure_scheduler via `from .config import settings`


def _mock_settings(**overrides):
    """Build a mock settings object with defaults for scheduler tests."""
    defaults = dict(
        inbox_scan_interval_min=30,
        contacts_sync_enabled=False,
        activity_tracking_enabled=False,
        proactive_matching_enabled=False,
        deep_email_mining_enabled=False,
        deep_enrichment_enabled=False,
        po_verify_interval_min=30,
        buyplan_auto_complete_hour=18,
        buyplan_auto_complete_tz="America/New_York",
    )
    defaults.update(overrides)
    from unittest.mock import MagicMock
    mock = MagicMock()
    for k, v in defaults.items():
        setattr(mock, k, v)
    return mock


def test_configure_scheduler_registers_core_jobs():
    """Core jobs (auto_archive, token_refresh, etc.) always registered."""
    with patch("app.config.settings", _mock_settings()):
        configure_scheduler()

    job_ids = {j.id for j in scheduler.get_jobs()}
    for core_id in ("auto_archive", "token_refresh", "inbox_scan",
                     "batch_results", "engagement_scoring"):
        assert core_id in job_ids, f"Missing core job: {core_id}"


def test_configure_scheduler_conditional_flags_off():
    """When conditional flags are off, optional jobs are not registered."""
    with patch("app.config.settings", _mock_settings()):
        configure_scheduler()

    job_ids = {j.id for j in scheduler.get_jobs()}
    assert "contacts_sync" not in job_ids
    assert "proactive_matching" not in job_ids
    assert "deep_email_mining" not in job_ids
    assert "deep_enrichment" not in job_ids


def test_configure_scheduler_conditional_flags_on():
    """When conditional flags are on, optional jobs are registered."""
    with patch("app.config.settings", _mock_settings(
        contacts_sync_enabled=True,
        activity_tracking_enabled=True,
        proactive_matching_enabled=True,
        deep_email_mining_enabled=True,
        deep_enrichment_enabled=True,
    )):
        configure_scheduler()

    job_ids = {j.id for j in scheduler.get_jobs()}
    assert "contacts_sync" in job_ids
    assert "proactive_matching" in job_ids
    assert "deep_email_mining" in job_ids
    assert "deep_enrichment" in job_ids


# ── _job_auto_archive() ───────────────────────────────────────────────


def test_auto_archive_archives_stale(scheduler_db, test_user):
    """Requisitions last searched >30 days ago get archived."""
    old = Requisition(
        name="OLD-001",
        status="active",
        created_by=test_user.id,
        last_searched_at=datetime.now(timezone.utc) - timedelta(days=45),
    )
    scheduler_db.add(old)
    scheduler_db.commit()

    from app.scheduler import _job_auto_archive
    asyncio.get_event_loop().run_until_complete(_job_auto_archive())

    scheduler_db.refresh(old)
    assert old.status == "archived"


def test_auto_archive_skips_recent(scheduler_db, test_user):
    """Requisitions searched recently are not archived."""
    recent = Requisition(
        name="RECENT-001",
        status="active",
        created_by=test_user.id,
        last_searched_at=datetime.now(timezone.utc) - timedelta(days=5),
    )
    scheduler_db.add(recent)
    scheduler_db.commit()

    from app.scheduler import _job_auto_archive
    asyncio.get_event_loop().run_until_complete(_job_auto_archive())

    scheduler_db.refresh(recent)
    assert recent.status == "active"


def test_auto_archive_skips_never_searched(scheduler_db, test_user):
    """Requisitions that have never been searched are not archived."""
    unsearched = Requisition(
        name="UNSEARCHED-001",
        status="active",
        created_by=test_user.id,
        last_searched_at=None,
    )
    scheduler_db.add(unsearched)
    scheduler_db.commit()

    from app.scheduler import _job_auto_archive
    asyncio.get_event_loop().run_until_complete(_job_auto_archive())

    scheduler_db.refresh(unsearched)
    assert unsearched.status == "active"


# ── _job_cache_cleanup() ──────────────────────────────────────────────


def test_cache_cleanup_calls_cleanup_expired():
    """Cache cleanup job delegates to intel_cache.cleanup_expired."""
    with patch("app.cache.intel_cache.cleanup_expired") as mock_cleanup:
        from app.scheduler import _job_cache_cleanup
        asyncio.get_event_loop().run_until_complete(_job_cache_cleanup())
        mock_cleanup.assert_called_once()


# ── _job_token_refresh() ──────────────────────────────────────────────


def test_token_refresh_refreshes_expired(scheduler_db, test_user):
    """Users with expired tokens get refreshed."""
    test_user.refresh_token = "rt_test_123"
    test_user.token_expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
    test_user.access_token = "old_token"
    scheduler_db.commit()

    with patch("app.scheduler.refresh_user_token", new_callable=AsyncMock) as mock_refresh:
        mock_refresh.return_value = "new_token"
        from app.scheduler import _job_token_refresh
        asyncio.get_event_loop().run_until_complete(_job_token_refresh())
        mock_refresh.assert_called_once()


def test_token_refresh_skips_valid(scheduler_db, test_user):
    """Users with valid tokens are not refreshed."""
    test_user.refresh_token = "rt_test_123"
    test_user.token_expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    test_user.access_token = "still_valid"
    scheduler_db.commit()

    with patch("app.scheduler.refresh_user_token", new_callable=AsyncMock) as mock_refresh:
        from app.scheduler import _job_token_refresh
        asyncio.get_event_loop().run_until_complete(_job_token_refresh())
        mock_refresh.assert_not_called()


# ── _job_batch_results() ─────────────────────────────────────────────


def test_batch_results_calls_process(scheduler_db):
    """Batch results job delegates to email_service.process_batch_results."""
    with patch("app.email_service.process_batch_results", new_callable=AsyncMock) as mock_pbr:
        mock_pbr.return_value = 5
        from app.scheduler import _job_batch_results
        asyncio.get_event_loop().run_until_complete(_job_batch_results())
        mock_pbr.assert_called_once_with(scheduler_db)


# ── _job_engagement_scoring() ─────────────────────────────────────────


def test_engagement_scoring_runs_when_stale(scheduler_db):
    """Engagement scoring runs when no recent computation exists."""
    with patch(
        "app.scheduler._compute_engagement_scores_job", new_callable=AsyncMock
    ) as mock_compute:
        from app.scheduler import _job_engagement_scoring
        asyncio.get_event_loop().run_until_complete(_job_engagement_scoring())
        mock_compute.assert_called_once()


def test_engagement_scoring_skips_when_recent(scheduler_db):
    """Engagement scoring skips when computed recently."""
    card = VendorCard(
        normalized_name="test vendor",
        display_name="Test Vendor",
        emails=[],
        phones=[],
        engagement_computed_at=datetime.now(timezone.utc) - timedelta(hours=2),
    )
    scheduler_db.add(card)
    scheduler_db.commit()

    with patch(
        "app.scheduler._compute_engagement_scores_job", new_callable=AsyncMock
    ) as mock_compute:
        from app.scheduler import _job_engagement_scoring
        asyncio.get_event_loop().run_until_complete(_job_engagement_scoring())
        mock_compute.assert_not_called()


# ── _job_proactive_matching() ─────────────────────────────────────────


def test_proactive_matching_calls_scan(scheduler_db):
    """Proactive matching job delegates to scan_new_offers_for_matches."""
    with patch(
        "app.services.proactive_service.scan_new_offers_for_matches"
    ) as mock_scan:
        mock_scan.return_value = {"matches_created": 3, "scanned": 10}
        from app.scheduler import _job_proactive_matching
        asyncio.get_event_loop().run_until_complete(_job_proactive_matching())
        mock_scan.assert_called_once_with(scheduler_db)


# ── _job_performance_tracking() ───────────────────────────────────────


def test_performance_tracking_calls_services(scheduler_db):
    """Performance tracking computes vendor scorecards and buyer leaderboard."""
    with patch(
        "app.services.performance_service.compute_all_vendor_scorecards"
    ) as mock_vs, patch(
        "app.services.performance_service.compute_buyer_leaderboard"
    ) as mock_bl:
        mock_vs.return_value = {"updated": 5, "skipped_cold_start": 2}
        mock_bl.return_value = {"entries": 3}
        from app.scheduler import _job_performance_tracking
        asyncio.get_event_loop().run_until_complete(_job_performance_tracking())
        mock_vs.assert_called_once_with(scheduler_db)
        assert mock_bl.call_count >= 1  # At least current month
