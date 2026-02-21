"""
test_scheduler.py — Tests for APScheduler background jobs

Covers: _utc helper, configure_scheduler registration, and individual job
functions (_job_auto_archive, _job_cache_cleanup, _job_token_refresh,
_job_batch_results, _job_engagement_scoring, _job_proactive_matching,
_job_performance_tracking, _job_inbox_scan, _job_contacts_sync,
_job_webhook_subscriptions, _job_ownership_sweep,
_job_po_verification, _job_stock_autocomplete, _job_deep_email_mining,
_job_deep_enrichment), plus get_valid_token, refresh_user_token,
_refresh_access_token, and _compute_engagement_scores_job.

All jobs use SessionLocal() internally, so we patch app.database.SessionLocal
to return the test DB session with close() disabled.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models import BuyPlan, Company, Quote, Requisition, User, VendorCard
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
    """Vendor scoring runs when no recent computation exists."""
    with patch(
        "app.scheduler._compute_vendor_scores_job", new_callable=AsyncMock
    ) as mock_compute:
        from app.scheduler import _job_engagement_scoring
        asyncio.get_event_loop().run_until_complete(_job_engagement_scoring())
        mock_compute.assert_called_once()


def test_engagement_scoring_skips_when_recent(scheduler_db):
    """Vendor scoring skips when computed recently."""
    card = VendorCard(
        normalized_name="test vendor",
        display_name="Test Vendor",
        emails=[],
        phones=[],
        vendor_score_computed_at=datetime.now(timezone.utc) - timedelta(hours=2),
    )
    scheduler_db.add(card)
    scheduler_db.commit()

    with patch(
        "app.scheduler._compute_vendor_scores_job", new_callable=AsyncMock
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


def test_performance_tracking_recomputes_previous_month_in_grace_period(scheduler_db):
    """During the first 7 days of a month, previous month is also recomputed."""
    # Freeze time to day 3 of a month to trigger grace period logic
    frozen_now = datetime(2026, 3, 3, 10, 0, 0, tzinfo=timezone.utc)

    with patch(
        "app.services.performance_service.compute_all_vendor_scorecards"
    ) as mock_vs, patch(
        "app.services.performance_service.compute_buyer_leaderboard"
    ) as mock_bl, patch(
        "app.scheduler.datetime"
    ) as mock_dt:
        mock_dt.now.return_value = frozen_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        mock_vs.return_value = {"updated": 5, "skipped_cold_start": 2}
        mock_bl.return_value = {"entries": 3}
        from app.scheduler import _job_performance_tracking
        asyncio.get_event_loop().run_until_complete(_job_performance_tracking())
        # Should be called twice: current month + previous month
        assert mock_bl.call_count == 2


def test_performance_tracking_error_handling(scheduler_db):
    """Performance tracking handles errors gracefully without propagating."""
    with patch(
        "app.services.performance_service.compute_all_vendor_scorecards"
    ) as mock_vs:
        mock_vs.side_effect = Exception("DB error")
        from app.scheduler import _job_performance_tracking
        # Should not raise
        asyncio.get_event_loop().run_until_complete(_job_performance_tracking())


# ── get_valid_token() ──────────────────────────────────────────────────


def test_get_valid_token_returns_current_when_valid(db_session, test_user):
    """Returns existing access_token when it has not expired."""
    test_user.access_token = "valid_token_abc"
    test_user.token_expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    db_session.commit()

    from app.scheduler import get_valid_token
    token = asyncio.get_event_loop().run_until_complete(
        get_valid_token(test_user, db_session)
    )
    assert token == "valid_token_abc"


def test_get_valid_token_refreshes_when_near_expiry(db_session, test_user):
    """Refreshes token when it expires within 5 minutes."""
    test_user.access_token = "about_to_expire"
    test_user.token_expires_at = datetime.now(timezone.utc) + timedelta(minutes=3)
    test_user.refresh_token = "rt_123"
    db_session.commit()

    with patch("app.scheduler.refresh_user_token", new_callable=AsyncMock) as mock_refresh:
        mock_refresh.return_value = "fresh_token_xyz"
        from app.scheduler import get_valid_token
        token = asyncio.get_event_loop().run_until_complete(
            get_valid_token(test_user, db_session)
        )
        mock_refresh.assert_called_once_with(test_user, db_session)
        assert token == "fresh_token_xyz"
        assert test_user.m365_last_healthy is not None
        assert test_user.m365_error_reason is None


def test_get_valid_token_refreshes_when_expired(db_session, test_user):
    """Refreshes token when it has already expired."""
    test_user.access_token = "expired_token"
    test_user.token_expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
    test_user.refresh_token = "rt_456"
    db_session.commit()

    with patch("app.scheduler.refresh_user_token", new_callable=AsyncMock) as mock_refresh:
        mock_refresh.return_value = "new_token"
        from app.scheduler import get_valid_token
        token = asyncio.get_event_loop().run_until_complete(
            get_valid_token(test_user, db_session)
        )
        assert token == "new_token"


def test_get_valid_token_sets_error_when_refresh_fails(db_session, test_user):
    """Sets m365_error_reason when token refresh fails."""
    test_user.access_token = "expired_token"
    test_user.token_expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
    test_user.refresh_token = "rt_bad"
    db_session.commit()

    with patch("app.scheduler.refresh_user_token", new_callable=AsyncMock) as mock_refresh:
        mock_refresh.return_value = None
        from app.scheduler import get_valid_token
        token = asyncio.get_event_loop().run_until_complete(
            get_valid_token(test_user, db_session)
        )
        assert token is None
        assert test_user.m365_error_reason == "Token refresh failed"


def test_get_valid_token_no_token_no_expiry(db_session, test_user):
    """Refreshes when there is no access_token at all."""
    test_user.access_token = None
    test_user.token_expires_at = None
    test_user.refresh_token = "rt_789"
    db_session.commit()

    with patch("app.scheduler.refresh_user_token", new_callable=AsyncMock) as mock_refresh:
        mock_refresh.return_value = "brand_new_token"
        from app.scheduler import get_valid_token
        token = asyncio.get_event_loop().run_until_complete(
            get_valid_token(test_user, db_session)
        )
        assert token == "brand_new_token"


# ── refresh_user_token() ──────────────────────────────────────────────


def test_refresh_user_token_success(db_session, test_user):
    """Successful refresh updates user fields."""
    test_user.refresh_token = "rt_old"
    test_user.access_token = "old_at"
    test_user.m365_connected = True
    db_session.commit()

    with patch("app.scheduler._refresh_access_token", new_callable=AsyncMock) as mock_aat:
        mock_aat.return_value = ("new_access_token", "new_refresh_token")
        from app.scheduler import refresh_user_token
        result = asyncio.get_event_loop().run_until_complete(
            refresh_user_token(test_user, db_session)
        )
        assert result == "new_access_token"
        assert test_user.access_token == "new_access_token"
        assert test_user.refresh_token == "new_refresh_token"
        assert test_user.m365_connected is True
        assert test_user.token_expires_at is not None


def test_refresh_user_token_no_refresh_token(db_session, test_user):
    """Returns None when user has no refresh_token."""
    test_user.refresh_token = None
    db_session.commit()

    from app.scheduler import refresh_user_token
    result = asyncio.get_event_loop().run_until_complete(
        refresh_user_token(test_user, db_session)
    )
    assert result is None


def test_refresh_user_token_failure_disconnects_user(db_session, test_user):
    """Failed refresh sets m365_connected to False."""
    test_user.refresh_token = "rt_invalid"
    test_user.m365_connected = True
    db_session.commit()

    with patch("app.scheduler._refresh_access_token", new_callable=AsyncMock) as mock_aat:
        mock_aat.return_value = None
        from app.scheduler import refresh_user_token
        result = asyncio.get_event_loop().run_until_complete(
            refresh_user_token(test_user, db_session)
        )
        assert result is None
        assert test_user.m365_connected is False


def test_refresh_user_token_keeps_old_refresh_when_none_returned(db_session, test_user):
    """Keeps existing refresh_token when Azure returns no new one."""
    test_user.refresh_token = "rt_keep_me"
    db_session.commit()

    with patch("app.scheduler._refresh_access_token", new_callable=AsyncMock) as mock_aat:
        mock_aat.return_value = ("new_at", None)  # no new refresh token
        from app.scheduler import refresh_user_token
        result = asyncio.get_event_loop().run_until_complete(
            refresh_user_token(test_user, db_session)
        )
        assert result == "new_at"
        assert test_user.refresh_token == "rt_keep_me"


# ── _refresh_access_token() ──────────────────────────────────────────


def test_refresh_access_token_success():
    """Successful HTTP refresh returns (access_token, refresh_token)."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "access_token": "at_new",
        "refresh_token": "rt_new",
    }

    with patch("app.scheduler.http") as mock_http:
        mock_http.post = AsyncMock(return_value=mock_response)
        from app.scheduler import _refresh_access_token
        result = asyncio.get_event_loop().run_until_complete(
            _refresh_access_token("rt_old", "client_id", "client_secret", "tenant_id")
        )
        assert result == ("at_new", "rt_new")


def test_refresh_access_token_failure_returns_none():
    """Non-200 response from Azure AD returns None."""
    mock_response = MagicMock()
    mock_response.status_code = 400
    mock_response.text = "invalid_grant: The refresh token has expired"

    with patch("app.scheduler.http") as mock_http:
        mock_http.post = AsyncMock(return_value=mock_response)
        from app.scheduler import _refresh_access_token
        result = asyncio.get_event_loop().run_until_complete(
            _refresh_access_token("rt_bad", "cid", "cs", "tid")
        )
        assert result is None


def test_refresh_access_token_exception_returns_none():
    """Network error during refresh returns None."""
    with patch("app.scheduler.http") as mock_http:
        mock_http.post = AsyncMock(side_effect=Exception("Connection refused"))
        from app.scheduler import _refresh_access_token
        result = asyncio.get_event_loop().run_until_complete(
            _refresh_access_token("rt", "cid", "cs", "tid")
        )
        assert result is None


# ── _job_token_refresh() additional cases ─────────────────────────────


def test_token_refresh_refreshes_user_without_access_token(scheduler_db, test_user):
    """Users with a refresh token but no access token get refreshed."""
    test_user.refresh_token = "rt_test_789"
    test_user.access_token = None
    test_user.token_expires_at = None
    scheduler_db.commit()

    with patch("app.scheduler.refresh_user_token", new_callable=AsyncMock) as mock_refresh:
        mock_refresh.return_value = "new_token"
        from app.scheduler import _job_token_refresh
        asyncio.get_event_loop().run_until_complete(_job_token_refresh())
        mock_refresh.assert_called_once()


def test_token_refresh_handles_error_per_user(scheduler_db, test_user):
    """Errors during per-user refresh are caught and do not crash the job."""
    test_user.refresh_token = "rt_test_err"
    test_user.token_expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
    test_user.access_token = "old"
    scheduler_db.commit()

    with patch("app.scheduler.refresh_user_token", new_callable=AsyncMock) as mock_refresh:
        mock_refresh.side_effect = Exception("Unexpected error")
        from app.scheduler import _job_token_refresh
        # Should not raise
        asyncio.get_event_loop().run_until_complete(_job_token_refresh())


# ── _job_inbox_scan() ──────────────────────────────────────────────────


def test_inbox_scan_scans_connected_user(scheduler_db, test_user):
    """Connected users with stale last_inbox_scan are scanned."""
    test_user.refresh_token = "rt_inbox"
    test_user.access_token = "at_inbox"
    test_user.m365_connected = True
    test_user.last_inbox_scan = datetime.now(timezone.utc) - timedelta(hours=2)
    scheduler_db.commit()

    with patch("app.scheduler._scan_user_inbox", new_callable=AsyncMock) as mock_scan, \
         patch("app.config.settings") as mock_settings:
        mock_settings.inbox_scan_interval_min = 30
        from app.scheduler import _job_inbox_scan
        asyncio.get_event_loop().run_until_complete(_job_inbox_scan())
        mock_scan.assert_called_once()


def test_inbox_scan_skips_disconnected_user(scheduler_db, test_user):
    """Users without m365_connected=True are skipped."""
    test_user.refresh_token = "rt_inbox"
    test_user.access_token = "at_inbox"
    test_user.m365_connected = False
    test_user.last_inbox_scan = None
    scheduler_db.commit()

    with patch("app.scheduler._scan_user_inbox", new_callable=AsyncMock) as mock_scan, \
         patch("app.config.settings") as mock_settings:
        mock_settings.inbox_scan_interval_min = 30
        from app.scheduler import _job_inbox_scan
        asyncio.get_event_loop().run_until_complete(_job_inbox_scan())
        mock_scan.assert_not_called()


def test_inbox_scan_skips_user_without_access_token(scheduler_db, test_user):
    """Users without an access_token are skipped even if connected."""
    test_user.refresh_token = "rt_inbox"
    test_user.access_token = None
    test_user.m365_connected = True
    test_user.last_inbox_scan = None
    scheduler_db.commit()

    with patch("app.scheduler._scan_user_inbox", new_callable=AsyncMock) as mock_scan, \
         patch("app.config.settings") as mock_settings:
        mock_settings.inbox_scan_interval_min = 30
        from app.scheduler import _job_inbox_scan
        asyncio.get_event_loop().run_until_complete(_job_inbox_scan())
        mock_scan.assert_not_called()


def test_inbox_scan_scans_user_with_no_previous_scan(scheduler_db, test_user):
    """Users who have never been scanned (last_inbox_scan=None) are scanned."""
    test_user.refresh_token = "rt_inbox"
    test_user.access_token = "at_inbox"
    test_user.m365_connected = True
    test_user.last_inbox_scan = None
    scheduler_db.commit()

    with patch("app.scheduler._scan_user_inbox", new_callable=AsyncMock) as mock_scan, \
         patch("app.config.settings") as mock_settings:
        mock_settings.inbox_scan_interval_min = 30
        from app.scheduler import _job_inbox_scan
        asyncio.get_event_loop().run_until_complete(_job_inbox_scan())
        mock_scan.assert_called_once()


def test_inbox_scan_skips_recently_scanned_user(scheduler_db, test_user):
    """Users scanned within the interval are skipped."""
    test_user.refresh_token = "rt_inbox"
    test_user.access_token = "at_inbox"
    test_user.m365_connected = True
    test_user.last_inbox_scan = datetime.now(timezone.utc) - timedelta(minutes=5)
    scheduler_db.commit()

    with patch("app.scheduler._scan_user_inbox", new_callable=AsyncMock) as mock_scan, \
         patch("app.config.settings") as mock_settings:
        mock_settings.inbox_scan_interval_min = 30
        from app.scheduler import _job_inbox_scan
        asyncio.get_event_loop().run_until_complete(_job_inbox_scan())
        mock_scan.assert_not_called()


def test_inbox_scan_handles_timeout(scheduler_db, test_user):
    """Timeout during inbox scan sets m365_error_reason."""
    test_user.refresh_token = "rt_inbox"
    test_user.access_token = "at_inbox"
    test_user.m365_connected = True
    test_user.last_inbox_scan = None
    scheduler_db.commit()

    async def slow_scan(user, db):
        await asyncio.sleep(999)

    with patch("app.scheduler._scan_user_inbox", new_callable=AsyncMock, side_effect=slow_scan), \
         patch("app.config.settings") as mock_settings, \
         patch("asyncio.wait_for", new_callable=AsyncMock, side_effect=asyncio.TimeoutError()):
        mock_settings.inbox_scan_interval_min = 30
        from app.scheduler import _job_inbox_scan
        asyncio.get_event_loop().run_until_complete(_job_inbox_scan())
        # User should have an error set
        assert test_user.m365_error_reason == "Inbox scan timed out"


# ── _job_contacts_sync() ─────────────────────────────────────────────


def test_contacts_sync_syncs_eligible_user(scheduler_db, test_user):
    """Users with no prior sync get synced."""
    test_user.refresh_token = "rt_contacts"
    test_user.access_token = "at_contacts"
    test_user.m365_connected = True
    test_user.last_contacts_sync = None
    scheduler_db.commit()

    with patch("app.scheduler._sync_user_contacts", new_callable=AsyncMock) as mock_sync:
        from app.scheduler import _job_contacts_sync
        asyncio.get_event_loop().run_until_complete(_job_contacts_sync())
        mock_sync.assert_called_once()


def test_contacts_sync_skips_recently_synced(scheduler_db, test_user):
    """Users synced within 24 hours are skipped."""
    test_user.refresh_token = "rt_contacts"
    test_user.access_token = "at_contacts"
    test_user.m365_connected = True
    test_user.last_contacts_sync = datetime.now(timezone.utc) - timedelta(hours=12)
    scheduler_db.commit()

    with patch("app.scheduler._sync_user_contacts", new_callable=AsyncMock) as mock_sync:
        from app.scheduler import _job_contacts_sync
        asyncio.get_event_loop().run_until_complete(_job_contacts_sync())
        mock_sync.assert_not_called()


def test_contacts_sync_skips_disconnected_user(scheduler_db, test_user):
    """Disconnected users are skipped."""
    test_user.refresh_token = "rt_contacts"
    test_user.access_token = "at_contacts"
    test_user.m365_connected = False
    test_user.last_contacts_sync = None
    scheduler_db.commit()

    with patch("app.scheduler._sync_user_contacts", new_callable=AsyncMock) as mock_sync:
        from app.scheduler import _job_contacts_sync
        asyncio.get_event_loop().run_until_complete(_job_contacts_sync())
        mock_sync.assert_not_called()


def test_contacts_sync_handles_per_user_error(scheduler_db, test_user):
    """Errors during per-user sync do not crash the job."""
    test_user.refresh_token = "rt_contacts"
    test_user.access_token = "at_contacts"
    test_user.m365_connected = True
    test_user.last_contacts_sync = None
    scheduler_db.commit()

    with patch("app.scheduler._sync_user_contacts", new_callable=AsyncMock) as mock_sync:
        mock_sync.side_effect = Exception("Graph API down")
        from app.scheduler import _job_contacts_sync
        # Should not raise
        asyncio.get_event_loop().run_until_complete(_job_contacts_sync())


def test_contacts_sync_syncs_stale_user(scheduler_db, test_user):
    """Users last synced >24h ago get synced."""
    test_user.refresh_token = "rt_contacts"
    test_user.access_token = "at_contacts"
    test_user.m365_connected = True
    test_user.last_contacts_sync = datetime.now(timezone.utc) - timedelta(hours=30)
    scheduler_db.commit()

    with patch("app.scheduler._sync_user_contacts", new_callable=AsyncMock) as mock_sync:
        from app.scheduler import _job_contacts_sync
        asyncio.get_event_loop().run_until_complete(_job_contacts_sync())
        mock_sync.assert_called_once()


# ── _job_webhook_subscriptions() ──────────────────────────────────────


def test_webhook_subscriptions_delegates(scheduler_db):
    """Webhook job calls renew_expiring + ensure_all_users_subscribed."""
    with patch(
        "app.services.webhook_service.renew_expiring_subscriptions",
        new_callable=AsyncMock,
    ) as mock_renew, patch(
        "app.services.webhook_service.ensure_all_users_subscribed",
        new_callable=AsyncMock,
    ) as mock_ensure:
        from app.scheduler import _job_webhook_subscriptions
        asyncio.get_event_loop().run_until_complete(_job_webhook_subscriptions())
        mock_renew.assert_called_once_with(scheduler_db)
        mock_ensure.assert_called_once_with(scheduler_db)


def test_webhook_subscriptions_error_handling(scheduler_db):
    """Webhook job handles errors gracefully."""
    with patch(
        "app.services.webhook_service.renew_expiring_subscriptions",
        new_callable=AsyncMock,
        side_effect=Exception("Graph API error"),
    ):
        from app.scheduler import _job_webhook_subscriptions
        # Should not raise
        asyncio.get_event_loop().run_until_complete(_job_webhook_subscriptions())


# ── _job_ownership_sweep() ────────────────────────────────────────────


def test_ownership_sweep_delegates(scheduler_db):
    """Ownership sweep delegates to run_ownership_sweep."""
    with patch(
        "app.services.ownership_service.run_ownership_sweep",
        new_callable=AsyncMock,
    ) as mock_sweep:
        from app.scheduler import _job_ownership_sweep
        asyncio.get_event_loop().run_until_complete(_job_ownership_sweep())
        mock_sweep.assert_called_once_with(scheduler_db)


def test_ownership_sweep_error_handling(scheduler_db):
    """Ownership sweep handles errors gracefully."""
    with patch(
        "app.services.ownership_service.run_ownership_sweep",
        new_callable=AsyncMock,
        side_effect=Exception("Sweep failed"),
    ):
        from app.scheduler import _job_ownership_sweep
        # Should not raise
        asyncio.get_event_loop().run_until_complete(_job_ownership_sweep())


# ── _job_po_verification() ────────────────────────────────────────────


def test_po_verification_verifies_po_entered_plans(
    scheduler_db, test_user, test_requisition, test_company, test_customer_site, test_quote
):
    """PO verification scans buy plans in po_entered status."""
    plan = BuyPlan(
        requisition_id=test_requisition.id,
        quote_id=test_quote.id,
        status="po_entered",
        line_items=[],
        submitted_by_id=test_user.id,
    )
    scheduler_db.add(plan)
    scheduler_db.commit()

    with patch(
        "app.services.buyplan_service.verify_po_sent",
        new_callable=AsyncMock,
    ) as mock_verify:
        from app.scheduler import _job_po_verification
        asyncio.get_event_loop().run_until_complete(_job_po_verification())
        mock_verify.assert_called_once()
        call_args = mock_verify.call_args
        assert call_args[0][0].id == plan.id


def test_po_verification_skips_when_no_plans(scheduler_db):
    """No verification calls when there are no po_entered plans."""
    with patch(
        "app.services.buyplan_service.verify_po_sent",
        new_callable=AsyncMock,
    ) as mock_verify:
        from app.scheduler import _job_po_verification
        asyncio.get_event_loop().run_until_complete(_job_po_verification())
        mock_verify.assert_not_called()


def test_po_verification_handles_per_plan_error(
    scheduler_db, test_user, test_requisition, test_company, test_customer_site, test_quote
):
    """Errors during per-plan verification do not crash the job."""
    plan = BuyPlan(
        requisition_id=test_requisition.id,
        quote_id=test_quote.id,
        status="po_entered",
        line_items=[],
        submitted_by_id=test_user.id,
    )
    scheduler_db.add(plan)
    scheduler_db.commit()

    with patch(
        "app.services.buyplan_service.verify_po_sent",
        new_callable=AsyncMock,
        side_effect=Exception("Verification failed"),
    ):
        from app.scheduler import _job_po_verification
        # Should not raise
        asyncio.get_event_loop().run_until_complete(_job_po_verification())


# ── _job_stock_autocomplete() ─────────────────────────────────────────


def test_stock_autocomplete_delegates(scheduler_db):
    """Stock auto-complete delegates to auto_complete_stock_sales."""
    with patch(
        "app.services.buyplan_service.auto_complete_stock_sales"
    ) as mock_complete:
        mock_complete.return_value = 5
        from app.scheduler import _job_stock_autocomplete
        asyncio.get_event_loop().run_until_complete(_job_stock_autocomplete())
        mock_complete.assert_called_once_with(scheduler_db)


def test_stock_autocomplete_handles_zero(scheduler_db):
    """Job runs cleanly when no plans to complete."""
    with patch(
        "app.services.buyplan_service.auto_complete_stock_sales"
    ) as mock_complete:
        mock_complete.return_value = 0
        from app.scheduler import _job_stock_autocomplete
        asyncio.get_event_loop().run_until_complete(_job_stock_autocomplete())
        mock_complete.assert_called_once()


def test_stock_autocomplete_error_handling(scheduler_db):
    """Stock auto-complete handles errors gracefully."""
    with patch(
        "app.services.buyplan_service.auto_complete_stock_sales",
        side_effect=Exception("DB error"),
    ):
        from app.scheduler import _job_stock_autocomplete
        # Should not raise
        asyncio.get_event_loop().run_until_complete(_job_stock_autocomplete())


# ── _job_batch_results() additional cases ─────────────────────────────


def test_batch_results_handles_timeout(scheduler_db):
    """Batch results job handles asyncio.TimeoutError gracefully."""
    with patch(
        "app.email_service.process_batch_results",
        new_callable=AsyncMock,
    ) as mock_pbr, patch(
        "asyncio.wait_for",
        new_callable=AsyncMock,
        side_effect=asyncio.TimeoutError(),
    ):
        from app.scheduler import _job_batch_results
        # Should not raise
        asyncio.get_event_loop().run_until_complete(_job_batch_results())


def test_batch_results_handles_error(scheduler_db):
    """Batch results job handles general errors gracefully."""
    with patch(
        "app.email_service.process_batch_results",
        new_callable=AsyncMock,
        side_effect=Exception("AI service down"),
    ):
        from app.scheduler import _job_batch_results
        # Should not raise
        asyncio.get_event_loop().run_until_complete(_job_batch_results())


# ── _job_proactive_matching() additional cases ────────────────────────


def test_proactive_matching_no_matches(scheduler_db):
    """Proactive matching runs cleanly when no matches are created."""
    with patch(
        "app.services.proactive_service.scan_new_offers_for_matches"
    ) as mock_scan:
        mock_scan.return_value = {"matches_created": 0, "scanned": 5}
        from app.scheduler import _job_proactive_matching
        asyncio.get_event_loop().run_until_complete(_job_proactive_matching())
        mock_scan.assert_called_once()


def test_proactive_matching_error_handling(scheduler_db):
    """Proactive matching handles errors gracefully."""
    with patch(
        "app.services.proactive_service.scan_new_offers_for_matches",
        side_effect=Exception("DB connection lost"),
    ):
        from app.scheduler import _job_proactive_matching
        # Should not raise
        asyncio.get_event_loop().run_until_complete(_job_proactive_matching())


# ── _job_deep_email_mining() ──────────────────────────────────────────


def test_deep_email_mining_scans_eligible_user(scheduler_db, test_user):
    """Connected users without a recent deep scan are scanned."""
    test_user.refresh_token = "rt_deep"
    test_user.access_token = "at_deep"
    test_user.m365_connected = True
    test_user.last_deep_email_scan = None
    scheduler_db.commit()

    mock_miner_instance = MagicMock()
    mock_miner_instance.deep_scan_inbox = AsyncMock(return_value={
        "messages_scanned": 100,
        "contacts_found": 5,
        "per_domain": {},
    })

    with patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="token"), \
         patch("app.connectors.email_mining.EmailMiner", return_value=mock_miner_instance), \
         patch("app.services.deep_enrichment_service.link_contact_to_entities"):
        from app.scheduler import _job_deep_email_mining
        asyncio.get_event_loop().run_until_complete(_job_deep_email_mining())
        mock_miner_instance.deep_scan_inbox.assert_called_once()
        # User should have last_deep_email_scan updated
        assert test_user.last_deep_email_scan is not None


def test_deep_email_mining_skips_recently_scanned(scheduler_db, test_user):
    """Users scanned within 4 hours are skipped."""
    test_user.refresh_token = "rt_deep"
    test_user.access_token = "at_deep"
    test_user.m365_connected = True
    test_user.last_deep_email_scan = datetime.now(timezone.utc) - timedelta(hours=2)
    scheduler_db.commit()

    mock_miner_instance = MagicMock()
    mock_miner_instance.deep_scan_inbox = AsyncMock()

    with patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="token"), \
         patch("app.connectors.email_mining.EmailMiner", return_value=mock_miner_instance):
        from app.scheduler import _job_deep_email_mining
        asyncio.get_event_loop().run_until_complete(_job_deep_email_mining())
        mock_miner_instance.deep_scan_inbox.assert_not_called()


def test_deep_email_mining_skips_disconnected_user(scheduler_db, test_user):
    """Disconnected users are skipped."""
    test_user.refresh_token = "rt_deep"
    test_user.access_token = "at_deep"
    test_user.m365_connected = False
    test_user.last_deep_email_scan = None
    scheduler_db.commit()

    mock_miner_instance = MagicMock()
    mock_miner_instance.deep_scan_inbox = AsyncMock()

    with patch("app.scheduler.get_valid_token", new_callable=AsyncMock), \
         patch("app.connectors.email_mining.EmailMiner", return_value=mock_miner_instance):
        from app.scheduler import _job_deep_email_mining
        asyncio.get_event_loop().run_until_complete(_job_deep_email_mining())
        mock_miner_instance.deep_scan_inbox.assert_not_called()


def test_deep_email_mining_skips_when_no_valid_token(scheduler_db, test_user):
    """Users whose token refresh fails are skipped."""
    test_user.refresh_token = "rt_deep"
    test_user.access_token = "at_deep"
    test_user.m365_connected = True
    test_user.last_deep_email_scan = None
    scheduler_db.commit()

    mock_miner_instance = MagicMock()
    mock_miner_instance.deep_scan_inbox = AsyncMock()

    with patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value=None), \
         patch("app.connectors.email_mining.EmailMiner", return_value=mock_miner_instance):
        from app.scheduler import _job_deep_email_mining
        asyncio.get_event_loop().run_until_complete(_job_deep_email_mining())
        mock_miner_instance.deep_scan_inbox.assert_not_called()


def test_deep_email_mining_links_contacts(scheduler_db, test_user):
    """Contacts from per_domain results are linked via deep enrichment service."""
    test_user.refresh_token = "rt_deep"
    test_user.access_token = "at_deep"
    test_user.m365_connected = True
    test_user.last_deep_email_scan = None
    scheduler_db.commit()

    mock_miner_instance = MagicMock()
    mock_miner_instance.deep_scan_inbox = AsyncMock(return_value={
        "messages_scanned": 50,
        "contacts_found": 2,
        "per_domain": {
            "example.com": {
                "emails": ["alice@example.com", "bob@example.com"],
                "sender_names": ["Alice Smith"],
            }
        },
    })

    with patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="token"), \
         patch("app.connectors.email_mining.EmailMiner", return_value=mock_miner_instance), \
         patch("app.services.deep_enrichment_service.link_contact_to_entities") as mock_link:
        from app.scheduler import _job_deep_email_mining
        asyncio.get_event_loop().run_until_complete(_job_deep_email_mining())
        # Should be called for each email in per_domain
        assert mock_link.call_count == 2


# ── _job_deep_enrichment() ────────────────────────────────────────────


def test_deep_enrichment_enriches_stale_vendors(scheduler_db):
    """Stale vendor cards (no deep_enrichment_at) are enriched."""
    card = VendorCard(
        normalized_name="stale vendor",
        display_name="Stale Vendor",
        emails=[],
        phones=[],
        deep_enrichment_at=None,
        sighting_count=10,
        # Set created_at far in the past so it does not appear in "recent" query too
        created_at=datetime.now(timezone.utc) - timedelta(days=30),
    )
    scheduler_db.add(card)
    scheduler_db.commit()

    with patch("app.config.settings") as mock_settings, \
         patch(
             "app.services.deep_enrichment_service.deep_enrich_vendor",
             new_callable=AsyncMock,
         ) as mock_enrich_v, \
         patch(
             "app.services.deep_enrichment_service.deep_enrich_company",
             new_callable=AsyncMock,
         ):
        mock_settings.deep_enrichment_stale_days = 30
        from app.scheduler import _job_deep_enrichment
        asyncio.get_event_loop().run_until_complete(_job_deep_enrichment())
        mock_enrich_v.assert_called_once_with(card.id, scheduler_db)


def test_deep_enrichment_enriches_stale_companies(scheduler_db, test_company):
    """Stale companies (no deep_enrichment_at) are enriched."""
    test_company.deep_enrichment_at = None
    scheduler_db.commit()

    with patch("app.config.settings") as mock_settings, \
         patch(
             "app.services.deep_enrichment_service.deep_enrich_vendor",
             new_callable=AsyncMock,
         ), \
         patch(
             "app.services.deep_enrichment_service.deep_enrich_company",
             new_callable=AsyncMock,
         ) as mock_enrich_c:
        mock_settings.deep_enrichment_stale_days = 30
        from app.scheduler import _job_deep_enrichment
        asyncio.get_event_loop().run_until_complete(_job_deep_enrichment())
        mock_enrich_c.assert_called_once_with(test_company.id, scheduler_db)


def test_deep_enrichment_skips_recently_enriched(scheduler_db):
    """Recently enriched vendors are not re-enriched."""
    card = VendorCard(
        normalized_name="fresh vendor",
        display_name="Fresh Vendor",
        emails=[],
        phones=[],
        deep_enrichment_at=datetime.now(timezone.utc) - timedelta(days=1),
        sighting_count=10,
    )
    scheduler_db.add(card)
    scheduler_db.commit()

    with patch("app.config.settings") as mock_settings, \
         patch(
             "app.services.deep_enrichment_service.deep_enrich_vendor",
             new_callable=AsyncMock,
         ) as mock_enrich_v, \
         patch(
             "app.services.deep_enrichment_service.deep_enrich_company",
             new_callable=AsyncMock,
         ):
        mock_settings.deep_enrichment_stale_days = 30
        from app.scheduler import _job_deep_enrichment
        asyncio.get_event_loop().run_until_complete(_job_deep_enrichment())
        mock_enrich_v.assert_not_called()


def test_deep_enrichment_enriches_new_vendors(scheduler_db):
    """Recently created vendors without enrichment are enriched."""
    card = VendorCard(
        normalized_name="new vendor",
        display_name="New Vendor",
        emails=[],
        phones=[],
        deep_enrichment_at=None,
        sighting_count=1,
        created_at=datetime.now(timezone.utc) - timedelta(hours=2),
    )
    scheduler_db.add(card)
    scheduler_db.commit()

    with patch("app.config.settings") as mock_settings, \
         patch(
             "app.services.deep_enrichment_service.deep_enrich_vendor",
             new_callable=AsyncMock,
         ) as mock_enrich_v, \
         patch(
             "app.services.deep_enrichment_service.deep_enrich_company",
             new_callable=AsyncMock,
         ):
        mock_settings.deep_enrichment_stale_days = 30
        from app.scheduler import _job_deep_enrichment
        asyncio.get_event_loop().run_until_complete(_job_deep_enrichment())
        # Called at least once (may appear in stale OR recent query)
        assert mock_enrich_v.call_count >= 1


def test_deep_enrichment_error_handling(scheduler_db):
    """Deep enrichment handles errors gracefully."""
    card = VendorCard(
        normalized_name="error vendor",
        display_name="Error Vendor",
        emails=[],
        phones=[],
        deep_enrichment_at=None,
        sighting_count=5,
    )
    scheduler_db.add(card)
    scheduler_db.commit()

    with patch("app.config.settings") as mock_settings, \
         patch(
             "app.services.deep_enrichment_service.deep_enrich_vendor",
             new_callable=AsyncMock,
             side_effect=Exception("Enrichment API down"),
         ), \
         patch(
             "app.services.deep_enrichment_service.deep_enrich_company",
             new_callable=AsyncMock,
         ):
        mock_settings.deep_enrichment_stale_days = 30
        from app.scheduler import _job_deep_enrichment
        # Should not raise (errors are caught per-vendor)
        asyncio.get_event_loop().run_until_complete(_job_deep_enrichment())


# ── _compute_vendor_scores_job() ──────────────────────────────────


def test_compute_engagement_scores_job_delegates(db_session):
    """Vendor scores job delegates to compute_all_vendor_scores."""
    with patch(
        "app.services.vendor_score.compute_all_vendor_scores",
        new_callable=AsyncMock,
    ) as mock_compute:
        mock_compute.return_value = {"updated": 10, "skipped": 2}
        from app.scheduler import _compute_vendor_scores_job
        asyncio.get_event_loop().run_until_complete(
            _compute_vendor_scores_job(db_session)
        )
        mock_compute.assert_called_once_with(db_session)


def test_compute_engagement_scores_job_handles_error(db_session):
    """Vendor scores job handles errors without propagating."""
    with patch(
        "app.services.vendor_score.compute_all_vendor_scores",
        new_callable=AsyncMock,
        side_effect=Exception("Scorer crashed"),
    ):
        from app.scheduler import _compute_vendor_scores_job
        # Should not raise
        asyncio.get_event_loop().run_until_complete(
            _compute_vendor_scores_job(db_session)
        )


# ── _job_engagement_scoring() additional cases ────────────────────────


def test_engagement_scoring_error_handling(scheduler_db):
    """Vendor scoring handles errors gracefully."""
    with patch(
        "app.scheduler._compute_vendor_scores_job",
        new_callable=AsyncMock,
        side_effect=Exception("DB error"),
    ):
        from app.scheduler import _job_engagement_scoring
        # Should not raise
        asyncio.get_event_loop().run_until_complete(_job_engagement_scoring())


def test_engagement_scoring_runs_when_old_computation(scheduler_db):
    """Vendor scoring runs when the last computation is >12h old."""
    card = VendorCard(
        normalized_name="old score vendor",
        display_name="Old Score Vendor",
        emails=[],
        phones=[],
        vendor_score_computed_at=datetime.now(timezone.utc) - timedelta(hours=20),
    )
    scheduler_db.add(card)
    scheduler_db.commit()

    with patch(
        "app.scheduler._compute_vendor_scores_job", new_callable=AsyncMock
    ) as mock_compute:
        from app.scheduler import _job_engagement_scoring
        asyncio.get_event_loop().run_until_complete(_job_engagement_scoring())
        mock_compute.assert_called_once()


# ── _job_auto_archive() additional cases ──────────────────────────────


def test_auto_archive_only_archives_active_status(scheduler_db, test_user):
    """Only requisitions with status='active' are archived."""
    # Already-archived requisition should not be touched
    already_archived = Requisition(
        name="ALREADY-ARCHIVED",
        status="archived",
        created_by=test_user.id,
        last_searched_at=datetime.now(timezone.utc) - timedelta(days=60),
    )
    # "open" status req with old search should NOT be archived (only "active" is targeted)
    open_req = Requisition(
        name="OPEN-001",
        status="open",
        created_by=test_user.id,
        last_searched_at=datetime.now(timezone.utc) - timedelta(days=60),
    )
    scheduler_db.add_all([already_archived, open_req])
    scheduler_db.commit()

    from app.scheduler import _job_auto_archive
    asyncio.get_event_loop().run_until_complete(_job_auto_archive())

    scheduler_db.refresh(already_archived)
    scheduler_db.refresh(open_req)
    assert already_archived.status == "archived"  # unchanged
    assert open_req.status == "open"  # only "active" status is targeted


def test_auto_archive_error_handling(scheduler_db):
    """Auto-archive handles DB errors gracefully when query fails."""
    # Patch the query method to simulate a DB error inside the job
    with patch.object(scheduler_db, "query", side_effect=Exception("DB locked")):
        from app.scheduler import _job_auto_archive
        # Should not raise — error is caught internally
        asyncio.get_event_loop().run_until_complete(_job_auto_archive())


# ── _job_cache_cleanup() additional cases ─────────────────────────────


def test_cache_cleanup_handles_error():
    """Cache cleanup handles import or execution errors."""
    with patch(
        "app.cache.intel_cache.cleanup_expired",
        side_effect=Exception("Cache corrupted"),
    ):
        from app.scheduler import _job_cache_cleanup
        # Should not raise
        asyncio.get_event_loop().run_until_complete(_job_cache_cleanup())


# ── configure_scheduler() additional cases ────────────────────────────


def test_configure_scheduler_activity_tracking_jobs():
    """Activity tracking flag controls webhook_subs and ownership_sweep."""
    with patch("app.config.settings", _mock_settings(activity_tracking_enabled=True)):
        configure_scheduler()

    job_ids = {j.id for j in scheduler.get_jobs()}
    assert "webhook_subs" in job_ids
    assert "ownership_sweep" in job_ids


def test_configure_scheduler_always_includes_buyplan_jobs():
    """PO verification and stock auto-complete are always registered."""
    with patch("app.config.settings", _mock_settings()):
        configure_scheduler()

    job_ids = {j.id for j in scheduler.get_jobs()}
    assert "po_verification" in job_ids
    assert "stock_autocomplete" in job_ids


def test_configure_scheduler_always_includes_performance_and_cache():
    """Performance tracking and cache cleanup are always registered."""
    with patch("app.config.settings", _mock_settings()):
        configure_scheduler()

    job_ids = {j.id for j in scheduler.get_jobs()}
    assert "performance_tracking" in job_ids
    assert "cache_cleanup" in job_ids


# ── _parse_stock_file() ──────────────────────────────────────────────


def test_parse_stock_file_delegates_to_file_utils():
    """Stock file parser delegates to parse_tabular_file + normalize_stock_row."""
    with patch("app.file_utils.parse_tabular_file") as mock_parse, \
         patch("app.file_utils.normalize_stock_row") as mock_norm:
        mock_parse.return_value = [
            {"mpn": "LM317T", "qty": "100", "price": "0.50"},
            {"mpn": "NE555", "qty": "200", "price": "0.25"},
        ]
        mock_norm.side_effect = lambda r: r  # pass through
        from app.scheduler import _parse_stock_file
        result = _parse_stock_file(b"csv data", "test.csv")
        assert len(result) == 2
        mock_parse.assert_called_once_with(b"csv data", "test.csv")


def test_parse_stock_file_caps_at_5000_rows():
    """Stock file parser caps output at 5000 rows."""
    with patch("app.file_utils.parse_tabular_file") as mock_parse, \
         patch("app.file_utils.normalize_stock_row") as mock_norm:
        mock_parse.return_value = [{"mpn": f"MPN{i}"} for i in range(6000)]
        mock_norm.side_effect = lambda r: r
        from app.scheduler import _parse_stock_file
        result = _parse_stock_file(b"data", "big.csv")
        assert len(result) == 5000


def test_parse_stock_file_filters_invalid_rows():
    """Stock file parser filters out rows that normalize_stock_row returns None for."""
    with patch("app.file_utils.parse_tabular_file") as mock_parse, \
         patch("app.file_utils.normalize_stock_row") as mock_norm:
        mock_parse.return_value = [
            {"mpn": "VALID"},
            {"mpn": ""},  # invalid
            {"mpn": "ALSO_VALID"},
        ]
        mock_norm.side_effect = lambda r: r if r.get("mpn") else None
        from app.scheduler import _parse_stock_file
        result = _parse_stock_file(b"data", "test.csv")
        assert len(result) == 2
