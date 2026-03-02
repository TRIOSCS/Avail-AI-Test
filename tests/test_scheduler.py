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

from app.models import ActivityLog, BuyPlan, Requisition, VendorCard
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
        proactive_scan_interval_hours=4,
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
    for core_id in ("auto_archive", "token_refresh", "inbox_scan", "batch_results", "engagement_scoring"):
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
    with patch(
        "app.config.settings",
        _mock_settings(
            contacts_sync_enabled=True,
            activity_tracking_enabled=True,
            proactive_matching_enabled=True,
            deep_email_mining_enabled=True,
            deep_enrichment_enabled=True,
        ),
    ):
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

    asyncio.run(_job_auto_archive())

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

    asyncio.run(_job_auto_archive())

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

    asyncio.run(_job_auto_archive())

    scheduler_db.refresh(unsearched)
    assert unsearched.status == "active"


# ── _job_cache_cleanup() ──────────────────────────────────────────────


def test_cache_cleanup_calls_cleanup_expired():
    """Cache cleanup job delegates to intel_cache.cleanup_expired."""
    with patch("app.cache.intel_cache.cleanup_expired") as mock_cleanup:
        from app.scheduler import _job_cache_cleanup

        asyncio.run(_job_cache_cleanup())
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

        asyncio.run(_job_token_refresh())
        mock_refresh.assert_called_once()


def test_token_refresh_skips_valid(scheduler_db, test_user):
    """Users with valid tokens are not refreshed."""
    test_user.refresh_token = "rt_test_123"
    test_user.token_expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    test_user.access_token = "still_valid"
    scheduler_db.commit()

    with patch("app.scheduler.refresh_user_token", new_callable=AsyncMock) as mock_refresh:
        from app.scheduler import _job_token_refresh

        asyncio.run(_job_token_refresh())
        mock_refresh.assert_not_called()


# ── _job_batch_results() ─────────────────────────────────────────────


def test_batch_results_calls_process(scheduler_db):
    """Batch results job delegates to email_service.process_batch_results."""
    with patch("app.email_service.process_batch_results", new_callable=AsyncMock) as mock_pbr:
        mock_pbr.return_value = 5
        from app.scheduler import _job_batch_results

        asyncio.run(_job_batch_results())
        mock_pbr.assert_called_once_with(scheduler_db)


# ── _job_engagement_scoring() ─────────────────────────────────────────


def test_engagement_scoring_runs_when_stale(scheduler_db):
    """Vendor scoring runs when no recent computation exists."""
    with patch("app.scheduler._compute_vendor_scores_job", new_callable=AsyncMock) as mock_compute:
        from app.scheduler import _job_engagement_scoring

        asyncio.run(_job_engagement_scoring())
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

    with patch("app.scheduler._compute_vendor_scores_job", new_callable=AsyncMock) as mock_compute:
        from app.scheduler import _job_engagement_scoring

        asyncio.run(_job_engagement_scoring())
        mock_compute.assert_not_called()


# ── _job_proactive_matching() ─────────────────────────────────────────


def test_proactive_matching_calls_scan(scheduler_db):
    """Proactive matching job delegates to scan_new_offers_for_matches."""
    with patch("app.services.proactive_service.scan_new_offers_for_matches") as mock_scan:
        mock_scan.return_value = {"matches_created": 3, "scanned": 10}
        from app.scheduler import _job_proactive_matching

        asyncio.run(_job_proactive_matching())
        mock_scan.assert_called_once_with(scheduler_db)


# ── _job_performance_tracking() ───────────────────────────────────────


def test_performance_tracking_calls_services(scheduler_db):
    """Performance tracking computes vendor scorecards and buyer leaderboard."""
    with (
        patch("app.services.performance_service.compute_all_vendor_scorecards") as mock_vs,
        patch("app.services.performance_service.compute_buyer_leaderboard") as mock_bl,
    ):
        mock_vs.return_value = {"updated": 5, "skipped_cold_start": 2}
        mock_bl.return_value = {"entries": 3}
        from app.scheduler import _job_performance_tracking

        asyncio.run(_job_performance_tracking())
        mock_vs.assert_called_once_with(scheduler_db)
        assert mock_bl.call_count >= 1  # At least current month


def test_performance_tracking_recomputes_previous_month_in_grace_period(scheduler_db):
    """During the first 7 days of a month, previous month is also recomputed."""
    # Freeze time to day 3 of a month to trigger grace period logic
    frozen_now = datetime(2026, 3, 3, 10, 0, 0, tzinfo=timezone.utc)

    with (
        patch("app.services.performance_service.compute_all_vendor_scorecards") as mock_vs,
        patch("app.services.performance_service.compute_buyer_leaderboard") as mock_bl,
        patch("app.scheduler.datetime") as mock_dt,
    ):
        mock_dt.now.return_value = frozen_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        mock_vs.return_value = {"updated": 5, "skipped_cold_start": 2}
        mock_bl.return_value = {"entries": 3}
        from app.scheduler import _job_performance_tracking

        asyncio.run(_job_performance_tracking())
        # Should be called twice: current month + previous month
        assert mock_bl.call_count == 2


def test_performance_tracking_error_handling(scheduler_db):
    """Performance tracking handles errors gracefully without propagating."""
    with patch("app.services.performance_service.compute_all_vendor_scorecards") as mock_vs:
        mock_vs.side_effect = Exception("DB error")
        from app.scheduler import _job_performance_tracking

        # Should not raise
        asyncio.run(_job_performance_tracking())


# ── get_valid_token() ──────────────────────────────────────────────────


def test_get_valid_token_returns_current_when_valid(db_session, test_user):
    """Returns existing access_token when it has not expired."""
    test_user.access_token = "valid_token_abc"
    test_user.token_expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    db_session.commit()

    from app.scheduler import get_valid_token

    token = asyncio.run(get_valid_token(test_user, db_session))
    assert token == "valid_token_abc"


def test_get_valid_token_refreshes_when_near_expiry(db_session, test_user):
    """Refreshes token when it expires within 5 minutes."""
    test_user.access_token = "about_to_expire"
    test_user.token_expires_at = datetime.now(timezone.utc) + timedelta(minutes=3)
    test_user.refresh_token = "rt_123"
    db_session.commit()

    with patch("app.utils.token_manager.refresh_user_token", new_callable=AsyncMock) as mock_refresh:
        mock_refresh.return_value = "fresh_token_xyz"
        from app.scheduler import get_valid_token

        token = asyncio.run(get_valid_token(test_user, db_session))
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

    with patch("app.utils.token_manager.refresh_user_token", new_callable=AsyncMock) as mock_refresh:
        mock_refresh.return_value = "new_token"
        from app.scheduler import get_valid_token

        token = asyncio.run(get_valid_token(test_user, db_session))
        assert token == "new_token"


def test_get_valid_token_sets_error_when_refresh_fails(db_session, test_user):
    """Sets m365_error_reason when token refresh fails."""
    test_user.access_token = "expired_token"
    test_user.token_expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
    test_user.refresh_token = "rt_bad"
    db_session.commit()

    with patch("app.utils.token_manager.refresh_user_token", new_callable=AsyncMock) as mock_refresh:
        mock_refresh.return_value = None
        from app.scheduler import get_valid_token

        token = asyncio.run(get_valid_token(test_user, db_session))
        assert token is None
        assert test_user.m365_error_reason == "Token refresh failed"


def test_get_valid_token_no_token_no_expiry(db_session, test_user):
    """Refreshes when there is no access_token at all."""
    test_user.access_token = None
    test_user.token_expires_at = None
    test_user.refresh_token = "rt_789"
    db_session.commit()

    with patch("app.utils.token_manager.refresh_user_token", new_callable=AsyncMock) as mock_refresh:
        mock_refresh.return_value = "brand_new_token"
        from app.scheduler import get_valid_token

        token = asyncio.run(get_valid_token(test_user, db_session))
        assert token == "brand_new_token"


# ── refresh_user_token() ──────────────────────────────────────────────


def test_refresh_user_token_success(db_session, test_user):
    """Successful refresh updates user fields."""
    test_user.refresh_token = "rt_old"
    test_user.access_token = "old_at"
    test_user.m365_connected = True
    db_session.commit()

    with patch("app.utils.token_manager._refresh_access_token", new_callable=AsyncMock) as mock_aat:
        mock_aat.return_value = ("new_access_token", "new_refresh_token")
        from app.scheduler import refresh_user_token

        result = asyncio.run(refresh_user_token(test_user, db_session))
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

    result = asyncio.run(refresh_user_token(test_user, db_session))
    assert result is None


def test_refresh_user_token_failure_disconnects_user(db_session, test_user):
    """Failed refresh sets m365_connected to False."""
    test_user.refresh_token = "rt_invalid"
    test_user.m365_connected = True
    db_session.commit()

    with patch("app.utils.token_manager._refresh_access_token", new_callable=AsyncMock) as mock_aat:
        mock_aat.return_value = None
        from app.scheduler import refresh_user_token

        result = asyncio.run(refresh_user_token(test_user, db_session))
        assert result is None
        assert test_user.m365_connected is False


def test_refresh_user_token_keeps_old_refresh_when_none_returned(db_session, test_user):
    """Keeps existing refresh_token when Azure returns no new one."""
    test_user.refresh_token = "rt_keep_me"
    db_session.commit()

    with patch("app.utils.token_manager._refresh_access_token", new_callable=AsyncMock) as mock_aat:
        mock_aat.return_value = ("new_at", None)  # no new refresh token
        from app.scheduler import refresh_user_token

        result = asyncio.run(refresh_user_token(test_user, db_session))
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

    with patch("app.utils.token_manager.http") as mock_http:
        mock_http.post = AsyncMock(return_value=mock_response)
        from app.scheduler import _refresh_access_token

        result = asyncio.run(_refresh_access_token("rt_old", "client_id", "client_secret", "tenant_id"))
        assert result == ("at_new", "rt_new")


def test_refresh_access_token_failure_returns_none():
    """Non-200 response from Azure AD returns None."""
    mock_response = MagicMock()
    mock_response.status_code = 400
    mock_response.text = "invalid_grant: The refresh token has expired"

    with patch("app.utils.token_manager.http") as mock_http:
        mock_http.post = AsyncMock(return_value=mock_response)
        from app.scheduler import _refresh_access_token

        result = asyncio.run(_refresh_access_token("rt_bad", "cid", "cs", "tid"))
        assert result is None


def test_refresh_access_token_exception_returns_none():
    """Network error during refresh returns None."""
    with patch("app.utils.token_manager.http") as mock_http:
        mock_http.post = AsyncMock(side_effect=Exception("Connection refused"))
        from app.scheduler import _refresh_access_token

        result = asyncio.run(_refresh_access_token("rt", "cid", "cs", "tid"))
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

        asyncio.run(_job_token_refresh())
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
        asyncio.run(_job_token_refresh())


# ── _job_inbox_scan() ──────────────────────────────────────────────────


def test_inbox_scan_scans_connected_user(scheduler_db, test_user):
    """Connected users with stale last_inbox_scan are scanned."""
    test_user.refresh_token = "rt_inbox"
    test_user.access_token = "at_inbox"
    test_user.m365_connected = True
    test_user.last_inbox_scan = datetime.now(timezone.utc) - timedelta(hours=2)
    scheduler_db.commit()

    with (
        patch("app.scheduler._scan_user_inbox", new_callable=AsyncMock) as mock_scan,
        patch("app.config.settings") as mock_settings,
    ):
        mock_settings.inbox_scan_interval_min = 30
        from app.scheduler import _job_inbox_scan

        asyncio.run(_job_inbox_scan())
        mock_scan.assert_called_once()


def test_inbox_scan_skips_disconnected_user(scheduler_db, test_user):
    """Users without m365_connected=True are skipped."""
    test_user.refresh_token = "rt_inbox"
    test_user.access_token = "at_inbox"
    test_user.m365_connected = False
    test_user.last_inbox_scan = None
    scheduler_db.commit()

    with (
        patch("app.scheduler._scan_user_inbox", new_callable=AsyncMock) as mock_scan,
        patch("app.config.settings") as mock_settings,
    ):
        mock_settings.inbox_scan_interval_min = 30
        from app.scheduler import _job_inbox_scan

        asyncio.run(_job_inbox_scan())
        mock_scan.assert_not_called()


def test_inbox_scan_skips_user_without_access_token(scheduler_db, test_user):
    """Users without an access_token are skipped even if connected."""
    test_user.refresh_token = "rt_inbox"
    test_user.access_token = None
    test_user.m365_connected = True
    test_user.last_inbox_scan = None
    scheduler_db.commit()

    with (
        patch("app.scheduler._scan_user_inbox", new_callable=AsyncMock) as mock_scan,
        patch("app.config.settings") as mock_settings,
    ):
        mock_settings.inbox_scan_interval_min = 30
        from app.scheduler import _job_inbox_scan

        asyncio.run(_job_inbox_scan())
        mock_scan.assert_not_called()


def test_inbox_scan_scans_user_with_no_previous_scan(scheduler_db, test_user):
    """Users who have never been scanned (last_inbox_scan=None) are scanned."""
    test_user.refresh_token = "rt_inbox"
    test_user.access_token = "at_inbox"
    test_user.m365_connected = True
    test_user.last_inbox_scan = None
    scheduler_db.commit()

    with (
        patch("app.scheduler._scan_user_inbox", new_callable=AsyncMock) as mock_scan,
        patch("app.config.settings") as mock_settings,
    ):
        mock_settings.inbox_scan_interval_min = 30
        from app.scheduler import _job_inbox_scan

        asyncio.run(_job_inbox_scan())
        mock_scan.assert_called_once()


def test_inbox_scan_skips_recently_scanned_user(scheduler_db, test_user):
    """Users scanned within the interval are skipped."""
    test_user.refresh_token = "rt_inbox"
    test_user.access_token = "at_inbox"
    test_user.m365_connected = True
    test_user.last_inbox_scan = datetime.now(timezone.utc) - timedelta(minutes=5)
    scheduler_db.commit()

    with (
        patch("app.scheduler._scan_user_inbox", new_callable=AsyncMock) as mock_scan,
        patch("app.config.settings") as mock_settings,
    ):
        mock_settings.inbox_scan_interval_min = 30
        from app.scheduler import _job_inbox_scan

        asyncio.run(_job_inbox_scan())
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

    with (
        patch("app.scheduler._scan_user_inbox", new_callable=AsyncMock, side_effect=slow_scan),
        patch("app.config.settings") as mock_settings,
        patch("asyncio.wait_for", new_callable=AsyncMock, side_effect=asyncio.TimeoutError()),
    ):
        mock_settings.inbox_scan_interval_min = 30
        from app.scheduler import _job_inbox_scan

        asyncio.run(_job_inbox_scan())
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

        asyncio.run(_job_contacts_sync())
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

        asyncio.run(_job_contacts_sync())
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

        asyncio.run(_job_contacts_sync())
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
        asyncio.run(_job_contacts_sync())


def test_contacts_sync_syncs_stale_user(scheduler_db, test_user):
    """Users last synced >24h ago get synced."""
    test_user.refresh_token = "rt_contacts"
    test_user.access_token = "at_contacts"
    test_user.m365_connected = True
    test_user.last_contacts_sync = datetime.now(timezone.utc) - timedelta(hours=30)
    scheduler_db.commit()

    with patch("app.scheduler._sync_user_contacts", new_callable=AsyncMock) as mock_sync:
        from app.scheduler import _job_contacts_sync

        asyncio.run(_job_contacts_sync())
        mock_sync.assert_called_once()


# ── _job_webhook_subscriptions() ──────────────────────────────────────


def test_webhook_subscriptions_delegates(scheduler_db):
    """Webhook job calls renew_expiring + ensure_all_users_subscribed."""
    with (
        patch(
            "app.services.webhook_service.renew_expiring_subscriptions",
            new_callable=AsyncMock,
        ) as mock_renew,
        patch(
            "app.services.webhook_service.ensure_all_users_subscribed",
            new_callable=AsyncMock,
        ) as mock_ensure,
    ):
        from app.scheduler import _job_webhook_subscriptions

        asyncio.run(_job_webhook_subscriptions())
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
        asyncio.run(_job_webhook_subscriptions())


# ── _job_ownership_sweep() ────────────────────────────────────────────


def test_ownership_sweep_delegates(scheduler_db):
    """Ownership sweep delegates to run_ownership_sweep."""
    with patch(
        "app.services.ownership_service.run_ownership_sweep",
        new_callable=AsyncMock,
    ) as mock_sweep:
        from app.scheduler import _job_ownership_sweep

        asyncio.run(_job_ownership_sweep())
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
        asyncio.run(_job_ownership_sweep())


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

        asyncio.run(_job_po_verification())
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

        asyncio.run(_job_po_verification())
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
        asyncio.run(_job_po_verification())


# ── _job_stock_autocomplete() ─────────────────────────────────────────


def test_stock_autocomplete_delegates(scheduler_db):
    """Stock auto-complete delegates to auto_complete_stock_sales."""
    with patch("app.services.buyplan_service.auto_complete_stock_sales") as mock_complete:
        mock_complete.return_value = 5
        from app.scheduler import _job_stock_autocomplete

        asyncio.run(_job_stock_autocomplete())
        mock_complete.assert_called_once_with(scheduler_db)


def test_stock_autocomplete_handles_zero(scheduler_db):
    """Job runs cleanly when no plans to complete."""
    with patch("app.services.buyplan_service.auto_complete_stock_sales") as mock_complete:
        mock_complete.return_value = 0
        from app.scheduler import _job_stock_autocomplete

        asyncio.run(_job_stock_autocomplete())
        mock_complete.assert_called_once()


def test_stock_autocomplete_error_handling(scheduler_db):
    """Stock auto-complete handles errors gracefully."""
    with patch(
        "app.services.buyplan_service.auto_complete_stock_sales",
        side_effect=Exception("DB error"),
    ):
        from app.scheduler import _job_stock_autocomplete

        # Should not raise
        asyncio.run(_job_stock_autocomplete())


# ── _job_batch_results() additional cases ─────────────────────────────


def test_batch_results_handles_timeout(scheduler_db):
    """Batch results job handles asyncio.TimeoutError gracefully."""
    with (
        patch(
            "app.email_service.process_batch_results",
            new_callable=AsyncMock,
        ),
        patch(
            "asyncio.wait_for",
            new_callable=AsyncMock,
            side_effect=asyncio.TimeoutError(),
        ),
    ):
        from app.scheduler import _job_batch_results

        # Should not raise
        asyncio.run(_job_batch_results())


def test_batch_results_handles_error(scheduler_db):
    """Batch results job handles general errors gracefully."""
    with patch(
        "app.email_service.process_batch_results",
        new_callable=AsyncMock,
        side_effect=Exception("AI service down"),
    ):
        from app.scheduler import _job_batch_results

        # Should not raise
        asyncio.run(_job_batch_results())


# ── _job_proactive_matching() additional cases ────────────────────────


def test_proactive_matching_no_matches(scheduler_db):
    """Proactive matching runs cleanly when no matches are created."""
    with patch("app.services.proactive_service.scan_new_offers_for_matches") as mock_scan:
        mock_scan.return_value = {"matches_created": 0, "scanned": 5}
        from app.scheduler import _job_proactive_matching

        asyncio.run(_job_proactive_matching())
        mock_scan.assert_called_once()


def test_proactive_matching_error_handling(scheduler_db):
    """Proactive matching handles errors gracefully."""
    with patch(
        "app.services.proactive_service.scan_new_offers_for_matches",
        side_effect=Exception("DB connection lost"),
    ):
        from app.scheduler import _job_proactive_matching

        # Should not raise
        asyncio.run(_job_proactive_matching())


def test_proactive_matching_configurable_interval():
    """Proactive matching interval is configurable via proactive_scan_interval_hours."""
    with patch(
        "app.config.settings",
        _mock_settings(
            proactive_matching_enabled=True,
            proactive_scan_interval_hours=6,
        ),
    ):
        configure_scheduler()

    job = scheduler.get_job("proactive_matching")
    assert job is not None
    # Check the trigger interval is 6 hours
    trigger = job.trigger
    assert trigger.interval.total_seconds() == 6 * 3600


def test_proactive_matching_interval_minimum_1h():
    """Interval is clamped to at least 1 hour."""
    with patch(
        "app.config.settings",
        _mock_settings(
            proactive_matching_enabled=True,
            proactive_scan_interval_hours=0,
        ),
    ):
        configure_scheduler()

    job = scheduler.get_job("proactive_matching")
    assert job is not None
    trigger = job.trigger
    assert trigger.interval.total_seconds() == 1 * 3600


def test_proactive_matching_logs_summary(scheduler_db):
    """Proactive matching logs a summary with new matches and total pending."""
    with (
        patch("app.services.proactive_service.scan_new_offers_for_matches") as mock_legacy,
        patch("app.services.proactive_matching.run_proactive_scan") as mock_cph,
        patch("app.services.proactive_matching.expire_old_matches") as mock_expire,
        patch("app.scheduler.logger") as mock_logger,
    ):
        mock_legacy.return_value = {"matches_created": 2, "scanned": 10}
        mock_cph.return_value = {"matches_created": 1, "scanned_offers": 5, "scanned_sightings": 3}
        mock_expire.return_value = 0
        from app.scheduler import _job_proactive_matching

        asyncio.run(_job_proactive_matching())
        # Check summary log was called with "new matches" and "pending"
        log_calls = [str(c) for c in mock_logger.info.call_args_list]
        summary_found = any("3 new matches" in c and "pending" in c for c in log_calls)
        assert summary_found, f"Expected summary log with '3 new matches' and 'pending', got: {log_calls}"


# ── _job_deep_email_mining() ──────────────────────────────────────────


def test_deep_email_mining_scans_eligible_user(scheduler_db, test_user):
    """Connected users without a recent deep scan are scanned."""
    test_user.refresh_token = "rt_deep"
    test_user.access_token = "at_deep"
    test_user.m365_connected = True
    test_user.last_deep_email_scan = None
    scheduler_db.commit()

    mock_miner_instance = MagicMock()
    mock_miner_instance.deep_scan_inbox = AsyncMock(
        return_value={
            "messages_scanned": 100,
            "contacts_found": 5,
            "per_domain": {},
        }
    )

    with (
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.connectors.email_mining.EmailMiner", return_value=mock_miner_instance),
        patch("app.services.deep_enrichment_service.link_contact_to_entities"),
    ):
        from app.scheduler import _job_deep_email_mining

        asyncio.run(_job_deep_email_mining())
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

    with (
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.connectors.email_mining.EmailMiner", return_value=mock_miner_instance),
    ):
        from app.scheduler import _job_deep_email_mining

        asyncio.run(_job_deep_email_mining())
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

    with (
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock),
        patch("app.connectors.email_mining.EmailMiner", return_value=mock_miner_instance),
    ):
        from app.scheduler import _job_deep_email_mining

        asyncio.run(_job_deep_email_mining())
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

    with (
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value=None),
        patch("app.connectors.email_mining.EmailMiner", return_value=mock_miner_instance),
    ):
        from app.scheduler import _job_deep_email_mining

        asyncio.run(_job_deep_email_mining())
        mock_miner_instance.deep_scan_inbox.assert_not_called()


def test_deep_email_mining_links_contacts(scheduler_db, test_user):
    """Contacts from per_domain results are linked via deep enrichment service."""
    test_user.refresh_token = "rt_deep"
    test_user.access_token = "at_deep"
    test_user.m365_connected = True
    test_user.last_deep_email_scan = None
    scheduler_db.commit()

    mock_miner_instance = MagicMock()
    mock_miner_instance.deep_scan_inbox = AsyncMock(
        return_value={
            "messages_scanned": 50,
            "contacts_found": 2,
            "per_domain": {
                "example.com": {
                    "emails": ["alice@example.com", "bob@example.com"],
                    "sender_names": ["Alice Smith"],
                }
            },
        }
    )

    with (
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.connectors.email_mining.EmailMiner", return_value=mock_miner_instance),
        patch("app.services.deep_enrichment_service.link_contact_to_entities") as mock_link,
    ):
        from app.scheduler import _job_deep_email_mining

        asyncio.run(_job_deep_email_mining())
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

    with (
        patch("app.config.settings") as mock_settings,
        patch(
            "app.services.deep_enrichment_service.deep_enrich_vendor",
            new_callable=AsyncMock,
        ) as mock_enrich_v,
        patch(
            "app.services.deep_enrichment_service.deep_enrich_company",
            new_callable=AsyncMock,
        ),
    ):
        mock_settings.deep_enrichment_stale_days = 30
        from app.scheduler import _job_deep_enrichment

        asyncio.run(_job_deep_enrichment())
        mock_enrich_v.assert_called_once_with(card.id, scheduler_db)


def test_deep_enrichment_enriches_stale_companies(scheduler_db, test_company):
    """Stale companies (no deep_enrichment_at) are enriched."""
    test_company.deep_enrichment_at = None
    scheduler_db.commit()

    with (
        patch("app.config.settings") as mock_settings,
        patch(
            "app.services.deep_enrichment_service.deep_enrich_vendor",
            new_callable=AsyncMock,
        ),
        patch(
            "app.services.deep_enrichment_service.deep_enrich_company",
            new_callable=AsyncMock,
        ) as mock_enrich_c,
    ):
        mock_settings.deep_enrichment_stale_days = 30
        from app.scheduler import _job_deep_enrichment

        asyncio.run(_job_deep_enrichment())
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

    with (
        patch("app.config.settings") as mock_settings,
        patch(
            "app.services.deep_enrichment_service.deep_enrich_vendor",
            new_callable=AsyncMock,
        ) as mock_enrich_v,
        patch(
            "app.services.deep_enrichment_service.deep_enrich_company",
            new_callable=AsyncMock,
        ),
    ):
        mock_settings.deep_enrichment_stale_days = 30
        from app.scheduler import _job_deep_enrichment

        asyncio.run(_job_deep_enrichment())
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

    with (
        patch("app.config.settings") as mock_settings,
        patch(
            "app.services.deep_enrichment_service.deep_enrich_vendor",
            new_callable=AsyncMock,
        ) as mock_enrich_v,
        patch(
            "app.services.deep_enrichment_service.deep_enrich_company",
            new_callable=AsyncMock,
        ),
    ):
        mock_settings.deep_enrichment_stale_days = 30
        from app.scheduler import _job_deep_enrichment

        asyncio.run(_job_deep_enrichment())
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

    with (
        patch("app.config.settings") as mock_settings,
        patch(
            "app.services.deep_enrichment_service.deep_enrich_vendor",
            new_callable=AsyncMock,
            side_effect=Exception("Enrichment API down"),
        ),
        patch(
            "app.services.deep_enrichment_service.deep_enrich_company",
            new_callable=AsyncMock,
        ),
    ):
        mock_settings.deep_enrichment_stale_days = 30
        from app.scheduler import _job_deep_enrichment

        # Should not raise (errors are caught per-vendor)
        asyncio.run(_job_deep_enrichment())


# ── _compute_vendor_scores_job() ──────────────────────────────────


def test_compute_engagement_scores_job_delegates(db_session):
    """Vendor scores job delegates to compute_all_vendor_scores."""
    with patch(
        "app.services.vendor_score.compute_all_vendor_scores",
        new_callable=AsyncMock,
    ) as mock_compute:
        mock_compute.return_value = {"updated": 10, "skipped": 2}
        from app.scheduler import _compute_vendor_scores_job

        asyncio.run(_compute_vendor_scores_job(db_session))
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
        asyncio.run(_compute_vendor_scores_job(db_session))


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
        asyncio.run(_job_engagement_scoring())


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

    with patch("app.scheduler._compute_vendor_scores_job", new_callable=AsyncMock) as mock_compute:
        from app.scheduler import _job_engagement_scoring

        asyncio.run(_job_engagement_scoring())
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

    asyncio.run(_job_auto_archive())

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
        asyncio.run(_job_auto_archive())


# ── _job_cache_cleanup() additional cases ─────────────────────────────


def test_cache_cleanup_handles_error():
    """Cache cleanup handles import or execution errors."""
    with patch(
        "app.cache.intel_cache.cleanup_expired",
        side_effect=Exception("Cache corrupted"),
    ):
        from app.scheduler import _job_cache_cleanup

        # Should not raise
        asyncio.run(_job_cache_cleanup())


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
    with (
        patch("app.file_utils.parse_tabular_file") as mock_parse,
        patch("app.file_utils.normalize_stock_row") as mock_norm,
    ):
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
    with (
        patch("app.file_utils.parse_tabular_file") as mock_parse,
        patch("app.file_utils.normalize_stock_row") as mock_norm,
    ):
        mock_parse.return_value = [{"mpn": f"MPN{i}"} for i in range(6000)]
        mock_norm.side_effect = lambda r: r
        from app.scheduler import _parse_stock_file

        result = _parse_stock_file(b"data", "big.csv")
        assert len(result) == 5000


def test_parse_stock_file_filters_invalid_rows():
    """Stock file parser filters out rows that normalize_stock_row returns None for."""
    with (
        patch("app.file_utils.parse_tabular_file") as mock_parse,
        patch("app.file_utils.normalize_stock_row") as mock_norm,
    ):
        mock_parse.return_value = [
            {"mpn": "VALID"},
            {"mpn": ""},  # invalid
            {"mpn": "ALSO_VALID"},
        ]
        mock_norm.side_effect = lambda r: r if r.get("mpn") else None
        from app.scheduler import _parse_stock_file

        result = _parse_stock_file(b"data", "test.csv")
        assert len(result) == 2


# ===========================================================================
# Additional coverage tests — _traced_job exception path (lines 47-49)
# ===========================================================================


def test_traced_job_exception_is_reraised():
    """The _traced_job wrapper re-raises exceptions after logging."""
    from app.scheduler import _traced_job

    @_traced_job
    async def boom():
        raise RuntimeError("kaboom")

    with pytest.raises(RuntimeError, match="kaboom"):
        asyncio.run(boom())


# ===========================================================================
# _job_token_refresh outer exception (lines 288-289)
# ===========================================================================


def test_token_refresh_outer_exception(scheduler_db):
    """Outer exception in _job_token_refresh is caught."""
    with patch.object(scheduler_db, "query", side_effect=Exception("DB crash")):
        from app.scheduler import _job_token_refresh

        # Should not raise
        asyncio.run(_job_token_refresh())


# ===========================================================================
# _job_inbox_scan error in user-gathering phase (lines 320-322)
# ===========================================================================


def test_inbox_scan_error_in_user_gathering(scheduler_db):
    """Error during user-gathering phase returns early."""
    with (
        patch.object(scheduler_db, "query", side_effect=Exception("DB error")),
        patch("app.config.settings") as mock_settings,
    ):
        mock_settings.inbox_scan_interval_min = 30
        from app.scheduler import _job_inbox_scan

        # Should not raise
        asyncio.run(_job_inbox_scan())


# ===========================================================================
# _job_inbox_scan _safe_scan timeout (lines 335, 345-349)
# ===========================================================================


def test_inbox_scan_safe_scan_timeout(scheduler_db, test_user):
    """Timeout during _safe_scan sets m365_error_reason on user."""
    test_user.refresh_token = "rt_timeout"
    test_user.access_token = "at_timeout"
    test_user.m365_connected = True
    test_user.last_inbox_scan = None
    scheduler_db.commit()

    async def _timeout_scan(user, db):
        raise asyncio.TimeoutError()

    with (
        patch("app.scheduler._scan_user_inbox", new_callable=AsyncMock) as mock_scan,
        patch("app.config.settings") as mock_settings,
    ):
        mock_settings.inbox_scan_interval_min = 30

        # Make wait_for raise TimeoutError
        original_wait_for = asyncio.wait_for

        async def _mock_wait_for(coro, timeout=None):
            # Cancel the actual coro
            try:
                coro.close()
            except Exception:
                pass
            raise asyncio.TimeoutError()

        with patch("asyncio.wait_for", side_effect=_mock_wait_for):
            from app.scheduler import _job_inbox_scan

            asyncio.run(_job_inbox_scan())

    scheduler_db.refresh(test_user)
    assert test_user.m365_error_reason == "Inbox scan timed out"


def test_inbox_scan_safe_scan_exception(scheduler_db, test_user):
    """General exception in _safe_scan is caught."""
    test_user.refresh_token = "rt_err"
    test_user.access_token = "at_err"
    test_user.m365_connected = True
    test_user.last_inbox_scan = None
    scheduler_db.commit()

    with (
        patch("app.scheduler._scan_user_inbox", new_callable=AsyncMock, side_effect=Exception("random error")),
        patch("app.config.settings") as mock_settings,
    ):
        mock_settings.inbox_scan_interval_min = 30
        from app.scheduler import _job_inbox_scan

        # Should not raise
        asyncio.run(_job_inbox_scan())


# ===========================================================================
# _job_contacts_sync error in user-gathering (lines 400-402)
# ===========================================================================


def test_contacts_sync_error_in_user_gathering(scheduler_db):
    """Error during user-gathering returns early."""
    with patch.object(scheduler_db, "query", side_effect=Exception("DB error")):
        from app.scheduler import _job_contacts_sync

        # Should not raise
        asyncio.run(_job_contacts_sync())


# ===========================================================================
# _job_contacts_sync timeout handling (lines 412, 415-416)
# ===========================================================================


def test_contacts_sync_timeout(scheduler_db, test_user):
    """Timeout during contacts sync is handled gracefully."""
    test_user.refresh_token = "rt_contacts"
    test_user.access_token = "at_contacts"
    test_user.m365_connected = True
    test_user.last_contacts_sync = None
    scheduler_db.commit()

    async def _mock_wait_for(coro, timeout=None):
        try:
            coro.close()
        except Exception:
            pass
        raise asyncio.TimeoutError()

    with (
        patch("asyncio.wait_for", side_effect=_mock_wait_for),
        patch("app.scheduler._sync_user_contacts", new_callable=AsyncMock),
    ):
        from app.scheduler import _job_contacts_sync

        # Should not raise
        asyncio.run(_job_contacts_sync())


# ===========================================================================
# _job_po_verification outer exception (lines 515-517)
# ===========================================================================


def test_po_verification_outer_exception(scheduler_db):
    """Outer exception in PO verification is caught."""
    with patch.object(scheduler_db, "query", side_effect=Exception("DB crash")):
        from app.scheduler import _job_po_verification

        # Should not raise
        asyncio.run(_job_po_verification())


# ===========================================================================
# _job_stock_autocomplete timeout (lines 538-539)
# ===========================================================================


def test_stock_autocomplete_timeout(scheduler_db):
    """Stock auto-complete handles timeout gracefully."""

    async def _mock_wait_for(coro, timeout=None):
        try:
            coro.close()
        except Exception:
            pass
        raise asyncio.TimeoutError()

    with (
        patch("app.services.buyplan_service.auto_complete_stock_sales") as mock_complete,
        patch("asyncio.wait_for", side_effect=_mock_wait_for),
    ):
        from app.scheduler import _job_stock_autocomplete

        # Should not raise
        asyncio.run(_job_stock_autocomplete())


# ===========================================================================
# _job_proactive_matching timeout (lines 565-566)
# ===========================================================================


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
        from app.scheduler import _job_proactive_matching

        # Should not raise
        asyncio.run(_job_proactive_matching())


# ===========================================================================
# _job_performance_tracking timeout (lines 612-613)
# ===========================================================================


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
        from app.scheduler import _job_performance_tracking

        # Should not raise
        asyncio.run(_job_performance_tracking())


# ===========================================================================
# _job_deep_email_mining: link_contact exception (line 665-666)
# ===========================================================================


def test_deep_email_mining_link_contact_exception(scheduler_db, test_user):
    """Exception in link_contact_to_entities is silently swallowed."""
    test_user.refresh_token = "rt_deep"
    test_user.access_token = "at_deep"
    test_user.m365_connected = True
    test_user.last_deep_email_scan = None
    scheduler_db.commit()

    mock_miner_instance = MagicMock()
    mock_miner_instance.deep_scan_inbox = AsyncMock(
        return_value={
            "messages_scanned": 10,
            "contacts_found": 1,
            "per_domain": {
                "example.com": {
                    "emails": ["fail@example.com"],
                    "sender_names": ["Fail User"],
                }
            },
        }
    )

    with (
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.connectors.email_mining.EmailMiner", return_value=mock_miner_instance),
        patch("app.services.deep_enrichment_service.link_contact_to_entities", side_effect=Exception("link error")),
    ):
        from app.scheduler import _job_deep_email_mining

        # Should not raise — the exception is caught with bare except+pass
        asyncio.run(_job_deep_email_mining())
        # User should still get updated
        assert test_user.last_deep_email_scan is not None


# ===========================================================================
# _job_deep_email_mining: per-user timeout (lines 674-675)
# ===========================================================================


def test_deep_email_mining_per_user_timeout(scheduler_db, test_user):
    """Per-user timeout in deep email mining is caught."""
    test_user.refresh_token = "rt_deep"
    test_user.access_token = "at_deep"
    test_user.m365_connected = True
    test_user.last_deep_email_scan = None
    scheduler_db.commit()

    mock_miner_instance = MagicMock()
    mock_miner_instance.deep_scan_inbox = AsyncMock(side_effect=asyncio.TimeoutError())

    with (
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.connectors.email_mining.EmailMiner", return_value=mock_miner_instance),
    ):
        from app.scheduler import _job_deep_email_mining

        # Should not raise
        asyncio.run(_job_deep_email_mining())


# ===========================================================================
# _job_deep_email_mining: per-user general exception (lines 676-678)
# ===========================================================================


def test_deep_email_mining_per_user_exception(scheduler_db, test_user):
    """Per-user general exception in deep email mining is caught."""
    test_user.refresh_token = "rt_deep"
    test_user.access_token = "at_deep"
    test_user.m365_connected = True
    test_user.last_deep_email_scan = None
    scheduler_db.commit()

    mock_miner_instance = MagicMock()
    mock_miner_instance.deep_scan_inbox = AsyncMock(side_effect=Exception("scan crash"))

    with (
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.connectors.email_mining.EmailMiner", return_value=mock_miner_instance),
    ):
        from app.scheduler import _job_deep_email_mining

        # Should not raise
        asyncio.run(_job_deep_email_mining())


# ===========================================================================
# _job_deep_email_mining: outer exception (lines 682-684)
# ===========================================================================


def test_deep_email_mining_outer_exception(scheduler_db):
    """Outer exception in deep email mining is caught."""
    with patch.object(scheduler_db, "query", side_effect=Exception("DB crash")):
        from app.scheduler import _job_deep_email_mining

        # Should not raise
        asyncio.run(_job_deep_email_mining())


# ===========================================================================
# _job_deep_enrichment: per-vendor/company errors (lines 750-752, 777-779)
# ===========================================================================


def test_deep_enrichment_per_vendor_error_with_savepoint(scheduler_db):
    """Per-vendor enrichment errors rollback to savepoint."""
    card = VendorCard(
        normalized_name="savepoint vendor",
        display_name="Savepoint Vendor",
        emails=[],
        phones=[],
        deep_enrichment_at=None,
        sighting_count=10,
        created_at=datetime.now(timezone.utc) - timedelta(days=30),
    )
    scheduler_db.add(card)
    scheduler_db.commit()

    with (
        patch("app.config.settings") as mock_settings,
        patch(
            "app.services.deep_enrichment_service.deep_enrich_vendor",
            new_callable=AsyncMock,
            side_effect=Exception("enrich failed"),
        ),
        patch(
            "app.services.deep_enrichment_service.deep_enrich_company",
            new_callable=AsyncMock,
        ),
    ):
        mock_settings.deep_enrichment_stale_days = 30
        from app.scheduler import _job_deep_enrichment

        # Should not raise (errors caught per-vendor with savepoint rollback)
        asyncio.run(_job_deep_enrichment())


def test_deep_enrichment_per_company_error_with_savepoint(scheduler_db, test_company):
    """Per-company enrichment errors rollback to savepoint."""
    test_company.deep_enrichment_at = None
    scheduler_db.commit()

    with (
        patch("app.config.settings") as mock_settings,
        patch(
            "app.services.deep_enrichment_service.deep_enrich_vendor",
            new_callable=AsyncMock,
        ),
        patch(
            "app.services.deep_enrichment_service.deep_enrich_company",
            new_callable=AsyncMock,
            side_effect=Exception("company enrich failed"),
        ),
    ):
        mock_settings.deep_enrichment_stale_days = 30
        from app.scheduler import _job_deep_enrichment

        # Should not raise
        asyncio.run(_job_deep_enrichment())


def test_deep_enrichment_outer_exception(scheduler_db):
    """Outer exception in deep enrichment is caught."""
    with (
        patch.object(scheduler_db, "query", side_effect=Exception("DB crash")),
        patch("app.config.settings") as mock_settings,
    ):
        mock_settings.deep_enrichment_stale_days = 30
        from app.scheduler import _job_deep_enrichment

        # Should not raise
        asyncio.run(_job_deep_enrichment())


# ===========================================================================
# _scan_user_inbox (lines 799-850)
# ===========================================================================


def test_scan_user_inbox_first_time_backfill(scheduler_db, test_user):
    """First-time scan (last_inbox_scan=None) triggers backfill."""
    test_user.access_token = "at_scan"
    test_user.last_inbox_scan = None
    scheduler_db.commit()

    with (
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.email_service.poll_inbox", new_callable=AsyncMock, return_value=["resp1"]),
        patch("app.scheduler._scan_stock_list_attachments", new_callable=AsyncMock) as mock_stock,
        patch("app.scheduler._mine_vendor_contacts", new_callable=AsyncMock) as mock_mine,
        patch("app.scheduler._scan_outbound_rfqs", new_callable=AsyncMock) as mock_outbound,
        patch("app.config.settings") as mock_settings,
    ):
        mock_settings.inbox_backfill_days = 180

        from app.scheduler import _scan_user_inbox

        asyncio.run(_scan_user_inbox(test_user, scheduler_db))

        mock_stock.assert_called_once()
        mock_mine.assert_called_once()
        mock_outbound.assert_called_once()
        # is_backfill should be True
        assert mock_stock.call_args[0][2] is True
        assert test_user.last_inbox_scan is not None


def test_scan_user_inbox_not_backfill(scheduler_db, test_user):
    """Non-first scan sets is_backfill=False."""
    test_user.access_token = "at_scan"
    test_user.last_inbox_scan = datetime.now(timezone.utc) - timedelta(hours=1)
    scheduler_db.commit()

    with (
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.email_service.poll_inbox", new_callable=AsyncMock, return_value=[]),
        patch("app.scheduler._scan_stock_list_attachments", new_callable=AsyncMock) as mock_stock,
        patch("app.scheduler._mine_vendor_contacts", new_callable=AsyncMock),
        patch("app.scheduler._scan_outbound_rfqs", new_callable=AsyncMock),
        patch("app.config.settings") as mock_settings,
    ):
        mock_settings.inbox_backfill_days = 180

        from app.scheduler import _scan_user_inbox

        asyncio.run(_scan_user_inbox(test_user, scheduler_db))

        assert mock_stock.call_args[0][2] is False


def test_scan_user_inbox_no_valid_token(scheduler_db, test_user):
    """Inbox scan is skipped when no valid token is available."""
    test_user.access_token = "at_scan"
    test_user.last_inbox_scan = None
    scheduler_db.commit()

    with (
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value=None),
        patch("app.email_service.poll_inbox", new_callable=AsyncMock) as mock_poll,
        patch("app.scheduler._scan_stock_list_attachments", new_callable=AsyncMock),
        patch("app.scheduler._mine_vendor_contacts", new_callable=AsyncMock),
        patch("app.scheduler._scan_outbound_rfqs", new_callable=AsyncMock),
        patch("app.config.settings") as mock_settings,
    ):
        mock_settings.inbox_backfill_days = 180

        from app.scheduler import _scan_user_inbox

        asyncio.run(_scan_user_inbox(test_user, scheduler_db))

        mock_poll.assert_not_called()


def test_scan_user_inbox_poll_exception(scheduler_db, test_user):
    """Exception in poll_inbox is caught and sub-operations still run."""
    test_user.access_token = "at_scan"
    test_user.last_inbox_scan = datetime.now(timezone.utc) - timedelta(hours=1)
    scheduler_db.commit()

    with (
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.email_service.poll_inbox", new_callable=AsyncMock, side_effect=Exception("poll failed")),
        patch("app.scheduler._scan_stock_list_attachments", new_callable=AsyncMock) as mock_stock,
        patch("app.scheduler._mine_vendor_contacts", new_callable=AsyncMock) as mock_mine,
        patch("app.scheduler._scan_outbound_rfqs", new_callable=AsyncMock) as mock_outbound,
        patch("app.config.settings") as mock_settings,
    ):
        mock_settings.inbox_backfill_days = 180

        from app.scheduler import _scan_user_inbox

        asyncio.run(_scan_user_inbox(test_user, scheduler_db))

        # Sub-operations should still run
        mock_stock.assert_called_once()
        mock_mine.assert_called_once()
        mock_outbound.assert_called_once()


def test_scan_user_inbox_sub_operation_exceptions(scheduler_db, test_user):
    """Exceptions in sub-operations are caught individually."""
    test_user.access_token = "at_scan"
    test_user.last_inbox_scan = datetime.now(timezone.utc) - timedelta(hours=1)
    scheduler_db.commit()

    with (
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.email_service.poll_inbox", new_callable=AsyncMock, return_value=[]),
        patch(
            "app.scheduler._scan_stock_list_attachments", new_callable=AsyncMock, side_effect=Exception("stock error")
        ),
        patch("app.scheduler._mine_vendor_contacts", new_callable=AsyncMock, side_effect=Exception("mine error")),
        patch("app.scheduler._scan_outbound_rfqs", new_callable=AsyncMock, side_effect=Exception("outbound error")),
        patch("app.config.settings") as mock_settings,
    ):
        mock_settings.inbox_backfill_days = 180

        from app.scheduler import _scan_user_inbox

        # Should not raise — errors are caught per-sub-operation
        asyncio.run(_scan_user_inbox(test_user, scheduler_db))


# ===========================================================================
# _scan_stock_list_attachments (lines 855-884)
# ===========================================================================


def test_scan_stock_list_attachments_no_emails(scheduler_db, test_user):
    """No stock emails found — returns early."""
    test_user.access_token = "at_stock"
    scheduler_db.commit()

    mock_miner = MagicMock()
    mock_miner.scan_for_stock_lists = AsyncMock(return_value=[])

    with (
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.connectors.email_mining.EmailMiner", return_value=mock_miner),
        patch("app.config.settings") as mock_settings,
    ):
        mock_settings.inbox_backfill_days = 180

        from app.scheduler import _scan_stock_list_attachments

        asyncio.run(_scan_stock_list_attachments(test_user, scheduler_db, is_backfill=False))


def test_scan_stock_list_attachments_with_files(scheduler_db, test_user):
    """Stock emails with attachments trigger download and import."""
    test_user.access_token = "at_stock"
    scheduler_db.commit()

    mock_miner = MagicMock()
    mock_miner.scan_for_stock_lists = AsyncMock(
        return_value=[
            {
                "vendor_name": "Arrow",
                "from_email": "sales@arrow.com",
                "stock_files": [
                    {
                        "message_id": "msg1",
                        "attachment_id": "att1",
                        "filename": "stock.csv",
                    }
                ],
            }
        ]
    )

    with (
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.connectors.email_mining.EmailMiner", return_value=mock_miner),
        patch("app.scheduler._download_and_import_stock_list", new_callable=AsyncMock) as mock_dl,
        patch("app.config.settings") as mock_settings,
    ):
        mock_settings.inbox_backfill_days = 180

        from app.scheduler import _scan_stock_list_attachments

        asyncio.run(_scan_stock_list_attachments(test_user, scheduler_db, is_backfill=True))

        mock_dl.assert_called_once()


def test_scan_stock_list_attachments_import_error(scheduler_db, test_user):
    """Exception during import is caught per-attachment."""
    test_user.access_token = "at_stock"
    scheduler_db.commit()

    mock_miner = MagicMock()
    mock_miner.scan_for_stock_lists = AsyncMock(
        return_value=[
            {
                "vendor_name": "Arrow",
                "from_email": "sales@arrow.com",
                "stock_files": [
                    {
                        "message_id": "msg1",
                        "attachment_id": "att1",
                        "filename": "stock.csv",
                    }
                ],
            }
        ]
    )

    with (
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.connectors.email_mining.EmailMiner", return_value=mock_miner),
        patch(
            "app.scheduler._download_and_import_stock_list",
            new_callable=AsyncMock,
            side_effect=Exception("import failed"),
        ),
        patch("app.config.settings") as mock_settings,
    ):
        mock_settings.inbox_backfill_days = 180

        from app.scheduler import _scan_stock_list_attachments

        # Should not raise
        asyncio.run(_scan_stock_list_attachments(test_user, scheduler_db, is_backfill=False))


# ===========================================================================
# _download_and_import_stock_list (lines 897-1078)
# ===========================================================================


def test_download_and_import_stock_list_attachment_download_fails(scheduler_db, test_user):
    """Attachment download failure returns early."""
    test_user.access_token = "at_dl"
    scheduler_db.commit()

    mock_gc = MagicMock()
    mock_gc.get_json = AsyncMock(side_effect=Exception("download error"))

    with (
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
    ):
        from app.scheduler import _download_and_import_stock_list

        asyncio.run(
            _download_and_import_stock_list(
                test_user,
                scheduler_db,
                message_id="msg1",
                attachment_id="att1",
                filename="stock.csv",
                vendor_name="Arrow",
                vendor_email="sales@arrow.com",
            )
        )


def test_download_and_import_stock_list_error_in_att_data(scheduler_db, test_user):
    """Attachment data with error key returns early."""
    test_user.access_token = "at_dl"
    scheduler_db.commit()

    mock_gc = MagicMock()
    mock_gc.get_json = AsyncMock(return_value={"error": {"code": "NotFound"}})

    with (
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
    ):
        from app.scheduler import _download_and_import_stock_list

        asyncio.run(
            _download_and_import_stock_list(
                test_user,
                scheduler_db,
                message_id="msg1",
                attachment_id="att1",
                filename="stock.csv",
                vendor_name="Arrow",
                vendor_email="sales@arrow.com",
            )
        )


def test_download_and_import_stock_list_no_content_bytes(scheduler_db, test_user):
    """Attachment with no contentBytes returns early."""
    test_user.access_token = "at_dl"
    scheduler_db.commit()

    mock_gc = MagicMock()
    mock_gc.get_json = AsyncMock(return_value={"id": "att1"})  # no contentBytes

    with (
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
    ):
        from app.scheduler import _download_and_import_stock_list

        asyncio.run(
            _download_and_import_stock_list(
                test_user,
                scheduler_db,
                message_id="msg1",
                attachment_id="att1",
                filename="stock.csv",
                vendor_name="Arrow",
                vendor_email="sales@arrow.com",
            )
        )


def test_download_and_import_stock_list_file_validation_fails(scheduler_db, test_user):
    """Invalid file type returns early."""
    import base64

    test_user.access_token = "at_dl"
    scheduler_db.commit()

    mock_gc = MagicMock()
    mock_gc.get_json = AsyncMock(
        return_value={
            "contentBytes": base64.b64encode(b"not a csv").decode(),
        }
    )

    with (
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
        patch("app.utils.file_validation.validate_file", return_value=(False, "application/octet-stream")),
    ):
        from app.scheduler import _download_and_import_stock_list

        asyncio.run(
            _download_and_import_stock_list(
                test_user,
                scheduler_db,
                message_id="msg1",
                attachment_id="att1",
                filename="stock.bin",
                vendor_name="Arrow",
                vendor_email="sales@arrow.com",
            )
        )


def test_download_and_import_stock_list_no_rows(scheduler_db, test_user):
    """No valid rows in parsed file returns early."""
    import base64

    test_user.access_token = "at_dl"
    scheduler_db.commit()

    mock_gc = MagicMock()
    mock_gc.get_json = AsyncMock(
        return_value={
            "contentBytes": base64.b64encode(b"header\n").decode(),
        }
    )

    with (
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
        patch("app.utils.file_validation.validate_file", return_value=(True, "text/csv")),
        patch("app.services.attachment_parser.parse_attachment", new_callable=AsyncMock, return_value=[]),
    ):
        from app.scheduler import _download_and_import_stock_list

        asyncio.run(
            _download_and_import_stock_list(
                test_user,
                scheduler_db,
                message_id="msg1",
                attachment_id="att1",
                filename="stock.csv",
                vendor_name="Arrow",
                vendor_email="sales@arrow.com",
            )
        )


def test_download_and_import_stock_list_ai_parser_fallback(scheduler_db, test_user):
    """AI parser failure falls back to legacy _parse_stock_file."""
    import base64

    test_user.access_token = "at_dl"
    scheduler_db.commit()

    mock_gc = MagicMock()
    mock_gc.get_json = AsyncMock(
        return_value={
            "contentBytes": base64.b64encode(b"mpn,qty\nLM317T,100").decode(),
        }
    )

    with (
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
        patch("app.utils.file_validation.validate_file", return_value=(True, "text/csv")),
        patch(
            "app.services.attachment_parser.parse_attachment",
            new_callable=AsyncMock,
            side_effect=Exception("AI parser down"),
        ),
        patch("app.scheduler._parse_stock_file", return_value=[{"mpn": "LM317T", "qty": 100}]) as mock_legacy,
        patch("app.vendor_utils.normalize_vendor_name", return_value="arrow"),
        patch("app.services.activity_service.match_email_to_entity", return_value=None),
    ):
        from app.scheduler import _download_and_import_stock_list

        asyncio.run(
            _download_and_import_stock_list(
                test_user,
                scheduler_db,
                message_id="msg1",
                attachment_id="att1",
                filename="stock.csv",
                vendor_name="Arrow",
                vendor_email="sales@arrow.com",
            )
        )
        mock_legacy.assert_called_once()


def test_download_and_import_stock_list_creates_cards_and_mvh(scheduler_db, test_user):
    """Successful import creates MaterialCard and MaterialVendorHistory."""
    import base64

    from app.models import MaterialCard, MaterialVendorHistory

    test_user.access_token = "at_dl"
    scheduler_db.commit()

    mock_gc = MagicMock()
    mock_gc.get_json = AsyncMock(
        return_value={
            "contentBytes": base64.b64encode(b"data").decode(),
        }
    )

    rows = [
        {"mpn": "LM317T", "qty": 100, "price": 0.50, "manufacturer": "TI", "description": "Reg"},
    ]

    with (
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
        patch("app.utils.file_validation.validate_file", return_value=(True, "text/csv")),
        patch("app.services.attachment_parser.parse_attachment", new_callable=AsyncMock, return_value=rows),
        patch("app.vendor_utils.normalize_vendor_name", return_value="arrow"),
        patch("app.services.activity_service.match_email_to_entity", return_value=None),
        patch("app.services.teams.send_stock_match_alert", new_callable=AsyncMock),
    ):
        from app.scheduler import _download_and_import_stock_list

        asyncio.run(
            _download_and_import_stock_list(
                test_user,
                scheduler_db,
                message_id="msg1",
                attachment_id="att1",
                filename="stock.csv",
                vendor_name="Arrow",
                vendor_email="sales@arrow.com",
            )
        )

    # Verify card was created
    card = scheduler_db.query(MaterialCard).filter_by(normalized_mpn="lm317t").first()
    assert card is not None
    assert card.display_mpn == "LM317T"
    assert card.manufacturer == "TI"

    # Verify MVH was created
    mvh = scheduler_db.query(MaterialVendorHistory).filter_by(material_card_id=card.id, vendor_name="arrow").first()
    assert mvh is not None
    assert mvh.last_qty == 100


def test_download_and_import_stock_list_hyphenated_mpn_no_duplicate(scheduler_db, test_user):
    """Bug fix: MPN with hyphens should normalize to canonical key, not create duplicate cards."""
    import base64

    from app.models import MaterialCard

    test_user.access_token = "at_dl"
    scheduler_db.commit()

    # Pre-create a card as if the UI add-part flow created it (proper normalized key)
    existing = MaterialCard(
        normalized_mpn="qatest001",  # canonical key (lowercase, no dashes)
        display_mpn="QA-TEST-001",
        search_count=0,
    )
    scheduler_db.add(existing)
    scheduler_db.commit()

    mock_gc = MagicMock()
    mock_gc.get_json = AsyncMock(
        return_value={"contentBytes": base64.b64encode(b"data").decode()}
    )

    rows = [{"mpn": "QA-TEST-001", "qty": 50, "manufacturer": "Test Corp"}]

    with (
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
        patch("app.utils.file_validation.validate_file", return_value=(True, "text/csv")),
        patch("app.services.attachment_parser.parse_attachment", new_callable=AsyncMock, return_value=rows),
        patch("app.vendor_utils.normalize_vendor_name", return_value="testvendor"),
        patch("app.services.activity_service.match_email_to_entity", return_value=None),
        patch("app.services.teams.send_stock_match_alert", new_callable=AsyncMock),
    ):
        from app.scheduler import _download_and_import_stock_list

        asyncio.run(
            _download_and_import_stock_list(
                test_user,
                scheduler_db,
                message_id="msg1",
                attachment_id="att1",
                filename="stock.csv",
                vendor_name="TestVendor",
                vendor_email="sales@test.com",
            )
        )

    # Should NOT create a second card — the existing one should be reused
    all_cards = scheduler_db.query(MaterialCard).all()
    qa_cards = [c for c in all_cards if "qatest" in c.normalized_mpn]
    assert len(qa_cards) == 1, f"Expected 1 card, got {len(qa_cards)}: {[(c.normalized_mpn, c.display_mpn) for c in qa_cards]}"
    assert qa_cards[0].normalized_mpn == "qatest001"


def test_download_and_import_stock_list_updates_existing_mvh(scheduler_db, test_user):
    """Importing into existing MaterialCard updates MVH."""
    import base64

    from app.models import MaterialCard, MaterialVendorHistory

    test_user.access_token = "at_dl"
    scheduler_db.commit()

    # Pre-create card + MVH (use canonical key form for normalized_mpn)
    card = MaterialCard(
        normalized_mpn="ne555",
        display_mpn="NE555",
        manufacturer="TI",
        description="Timer",
    )
    scheduler_db.add(card)
    scheduler_db.flush()
    mvh = MaterialVendorHistory(
        material_card_id=card.id,
        vendor_name="arrow",
        source_type="email_auto_import",
        last_qty=50,
        times_seen=1,
    )
    scheduler_db.add(mvh)
    scheduler_db.commit()

    mock_gc = MagicMock()
    mock_gc.get_json = AsyncMock(
        return_value={
            "contentBytes": base64.b64encode(b"data").decode(),
        }
    )

    rows = [
        {"mpn": "NE555", "qty": 200, "unit_price": 0.30, "manufacturer": "TI"},
    ]

    with (
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
        patch("app.utils.file_validation.validate_file", return_value=(True, "text/csv")),
        patch("app.services.attachment_parser.parse_attachment", new_callable=AsyncMock, return_value=rows),
        patch("app.vendor_utils.normalize_vendor_name", return_value="arrow"),
        patch("app.services.activity_service.match_email_to_entity", return_value=None),
        patch("app.services.teams.send_stock_match_alert", new_callable=AsyncMock),
    ):
        from app.scheduler import _download_and_import_stock_list

        asyncio.run(
            _download_and_import_stock_list(
                test_user,
                scheduler_db,
                message_id="msg1",
                attachment_id="att1",
                filename="stock.csv",
                vendor_name="Arrow",
                vendor_email="sales@arrow.com",
            )
        )

    scheduler_db.refresh(mvh)
    assert mvh.times_seen == 2
    assert mvh.last_qty == 200
    assert mvh.last_price == 0.30


def test_download_and_import_stock_list_excess_list(scheduler_db, test_user, test_company):
    """Import from a company email is classified as excess_list."""
    import base64

    from app.models import MaterialVendorHistory

    test_user.access_token = "at_dl"
    scheduler_db.commit()

    mock_gc = MagicMock()
    mock_gc.get_json = AsyncMock(
        return_value={
            "contentBytes": base64.b64encode(b"data").decode(),
        }
    )

    rows = [
        {"mpn": "EXCESS1", "qty": 500, "manufacturer": "Murata"},
    ]

    with (
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
        patch("app.utils.file_validation.validate_file", return_value=(True, "text/csv")),
        patch("app.services.attachment_parser.parse_attachment", new_callable=AsyncMock, return_value=rows),
        patch("app.vendor_utils.normalize_vendor_name", return_value="acme"),
        patch(
            "app.services.activity_service.match_email_to_entity",
            return_value={
                "type": "company",
                "id": test_company.id,
                "name": "Acme Electronics",
            },
        ),
        patch("app.services.teams.send_stock_match_alert", new_callable=AsyncMock),
    ):
        from app.scheduler import _download_and_import_stock_list

        asyncio.run(
            _download_and_import_stock_list(
                test_user,
                scheduler_db,
                message_id="msg1",
                attachment_id="att1",
                filename="excess.csv",
                vendor_name="Acme Electronics",
                vendor_email="purchasing@acme-electronics.com",
            )
        )

    mvh = scheduler_db.query(MaterialVendorHistory).filter_by(vendor_name="acme").first()
    assert mvh is not None
    assert mvh.source_type == "excess_list"


def test_download_and_import_stock_list_skips_short_mpn(scheduler_db, test_user):
    """MPNs shorter than 3 chars are skipped."""
    import base64

    from app.models import MaterialCard

    test_user.access_token = "at_dl"
    scheduler_db.commit()

    mock_gc = MagicMock()
    mock_gc.get_json = AsyncMock(
        return_value={
            "contentBytes": base64.b64encode(b"data").decode(),
        }
    )

    rows = [
        {"mpn": "AB", "qty": 100},  # too short
        {"mpn": "", "qty": 200},  # empty
        {"mpn": "ABC", "qty": 300},  # OK
    ]

    with (
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
        patch("app.utils.file_validation.validate_file", return_value=(True, "text/csv")),
        patch("app.services.attachment_parser.parse_attachment", new_callable=AsyncMock, return_value=rows),
        patch("app.vendor_utils.normalize_vendor_name", return_value="arrow"),
        patch("app.services.activity_service.match_email_to_entity", return_value=None),
        patch("app.services.teams.send_stock_match_alert", new_callable=AsyncMock),
    ):
        from app.scheduler import _download_and_import_stock_list

        asyncio.run(
            _download_and_import_stock_list(
                test_user,
                scheduler_db,
                message_id="msg1",
                attachment_id="att1",
                filename="stock.csv",
                vendor_name="Arrow",
                vendor_email="sales@arrow.com",
            )
        )

    cards = scheduler_db.query(MaterialCard).all()
    # Only "ABC" should have been created (existing test cards may be present)
    abc_card = scheduler_db.query(MaterialCard).filter_by(normalized_mpn="abc").first()
    assert abc_card is not None


def test_download_and_import_stock_list_commit_fails(scheduler_db, test_user):
    """Commit failure during import is handled gracefully."""
    import base64

    test_user.access_token = "at_dl"
    scheduler_db.commit()

    mock_gc = MagicMock()
    mock_gc.get_json = AsyncMock(
        return_value={
            "contentBytes": base64.b64encode(b"data").decode(),
        }
    )

    rows = [{"mpn": "FAIL1", "qty": 100}]

    original_commit = scheduler_db.commit

    call_count = [0]

    def _failing_commit():
        call_count[0] += 1
        # Fail on the final commit (after import loop)
        if call_count[0] > 2:
            raise Exception("commit failed")
        return original_commit()

    with (
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
        patch("app.utils.file_validation.validate_file", return_value=(True, "text/csv")),
        patch("app.services.attachment_parser.parse_attachment", new_callable=AsyncMock, return_value=rows),
        patch("app.vendor_utils.normalize_vendor_name", return_value="arrow"),
        patch("app.services.activity_service.match_email_to_entity", return_value=None),
    ):
        scheduler_db.commit = _failing_commit
        from app.scheduler import _download_and_import_stock_list

        asyncio.run(
            _download_and_import_stock_list(
                test_user,
                scheduler_db,
                message_id="msg1",
                attachment_id="att1",
                filename="stock.csv",
                vendor_name="Arrow",
                vendor_email="sales@arrow.com",
            )
        )
        scheduler_db.commit = original_commit


def test_download_and_import_stock_list_no_vendor_email(scheduler_db, test_user):
    """Import works with empty vendor_email."""
    import base64

    from app.models import MaterialCard

    test_user.access_token = "at_dl"
    scheduler_db.commit()

    mock_gc = MagicMock()
    mock_gc.get_json = AsyncMock(
        return_value={
            "contentBytes": base64.b64encode(b"data").decode(),
        }
    )

    rows = [{"mpn": "NOEMAIL1", "qty": 100}]

    with (
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
        patch("app.utils.file_validation.validate_file", return_value=(True, "text/csv")),
        patch("app.services.attachment_parser.parse_attachment", new_callable=AsyncMock, return_value=rows),
        patch("app.vendor_utils.normalize_vendor_name", return_value="unknown"),
        patch("app.services.teams.send_stock_match_alert", new_callable=AsyncMock),
    ):
        from app.scheduler import _download_and_import_stock_list

        asyncio.run(
            _download_and_import_stock_list(
                test_user,
                scheduler_db,
                message_id="msg1",
                attachment_id="att1",
                filename="stock.csv",
                vendor_name="Unknown",
                vendor_email="",
            )
        )

    card = scheduler_db.query(MaterialCard).filter_by(normalized_mpn="noemail1").first()
    assert card is not None


def test_download_and_import_stock_list_price_field_fallback(scheduler_db, test_user):
    """MVH uses price field when unit_price is absent."""
    import base64

    from app.models import MaterialCard, MaterialVendorHistory

    test_user.access_token = "at_dl"
    scheduler_db.commit()

    # Pre-create card + MVH (use canonical key form for normalized_mpn)
    card = MaterialCard(
        normalized_mpn="pricefb",
        display_mpn="PRICEFB",
        manufacturer="Test",
    )
    scheduler_db.add(card)
    scheduler_db.flush()
    mvh = MaterialVendorHistory(
        material_card_id=card.id,
        vendor_name="arrow",
        source_type="email_auto_import",
        times_seen=1,
    )
    scheduler_db.add(mvh)
    scheduler_db.commit()

    mock_gc = MagicMock()
    mock_gc.get_json = AsyncMock(
        return_value={
            "contentBytes": base64.b64encode(b"data").decode(),
        }
    )

    # Row with "price" but no "unit_price"
    rows = [{"mpn": "PRICEFB", "qty": 100, "price": 1.25}]

    with (
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
        patch("app.utils.file_validation.validate_file", return_value=(True, "text/csv")),
        patch("app.services.attachment_parser.parse_attachment", new_callable=AsyncMock, return_value=rows),
        patch("app.vendor_utils.normalize_vendor_name", return_value="arrow"),
        patch("app.services.activity_service.match_email_to_entity", return_value=None),
        patch("app.services.teams.send_stock_match_alert", new_callable=AsyncMock),
    ):
        from app.scheduler import _download_and_import_stock_list

        asyncio.run(
            _download_and_import_stock_list(
                test_user,
                scheduler_db,
                message_id="msg1",
                attachment_id="att1",
                filename="stock.csv",
                vendor_name="Arrow",
                vendor_email="sales@arrow.com",
            )
        )

    scheduler_db.refresh(mvh)
    assert mvh.last_price == 1.25


def test_download_and_import_stock_list_teams_alert(scheduler_db, test_user, test_requisition):
    """Teams alert is sent when imported MPNs match open requirements."""
    import base64

    test_user.access_token = "at_dl"
    # Status must be in ["active", "sourcing", "offers"] for match
    test_requisition.status = "active"
    scheduler_db.commit()

    mock_gc = MagicMock()
    mock_gc.get_json = AsyncMock(
        return_value={
            "contentBytes": base64.b64encode(b"data").decode(),
        }
    )

    rows = [{"mpn": "LM317T", "qty": 100}]

    with (
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
        patch("app.utils.file_validation.validate_file", return_value=(True, "text/csv")),
        patch("app.services.attachment_parser.parse_attachment", new_callable=AsyncMock, return_value=rows),
        patch("app.vendor_utils.normalize_vendor_name", return_value="arrow"),
        patch("app.services.activity_service.match_email_to_entity", return_value=None),
        patch("app.services.teams.send_stock_match_alert", new_callable=AsyncMock) as mock_alert,
    ):
        from app.scheduler import _download_and_import_stock_list

        asyncio.run(
            _download_and_import_stock_list(
                test_user,
                scheduler_db,
                message_id="msg1",
                attachment_id="att1",
                filename="stock.csv",
                vendor_name="Arrow",
                vendor_email="sales@arrow.com",
            )
        )

        mock_alert.assert_called_once()


def test_download_and_import_stock_list_teams_alert_exception(scheduler_db, test_user, test_requisition):
    """Teams alert exception is caught silently."""
    import base64

    test_user.access_token = "at_dl"
    test_requisition.status = "active"
    scheduler_db.commit()

    mock_gc = MagicMock()
    mock_gc.get_json = AsyncMock(
        return_value={
            "contentBytes": base64.b64encode(b"data").decode(),
        }
    )

    rows = [{"mpn": "LM317T", "qty": 100}]

    with (
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
        patch("app.utils.file_validation.validate_file", return_value=(True, "text/csv")),
        patch("app.services.attachment_parser.parse_attachment", new_callable=AsyncMock, return_value=rows),
        patch("app.vendor_utils.normalize_vendor_name", return_value="arrow"),
        patch("app.services.activity_service.match_email_to_entity", return_value=None),
        patch(
            "app.services.teams.send_stock_match_alert", new_callable=AsyncMock, side_effect=Exception("Teams error")
        ),
    ):
        from app.scheduler import _download_and_import_stock_list

        # Should not raise
        asyncio.run(
            _download_and_import_stock_list(
                test_user,
                scheduler_db,
                message_id="msg1",
                attachment_id="att1",
                filename="stock.csv",
                vendor_name="Arrow",
                vendor_email="sales@arrow.com",
            )
        )


def test_download_and_import_stock_list_null_att_data(scheduler_db, test_user):
    """None response from get_json returns early."""
    test_user.access_token = "at_dl"
    scheduler_db.commit()

    mock_gc = MagicMock()
    mock_gc.get_json = AsyncMock(return_value=None)

    with (
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
    ):
        from app.scheduler import _download_and_import_stock_list

        asyncio.run(
            _download_and_import_stock_list(
                test_user,
                scheduler_db,
                message_id="msg1",
                attachment_id="att1",
                filename="stock.csv",
                vendor_name="Arrow",
                vendor_email="sales@arrow.com",
            )
        )


# ===========================================================================
# _mine_vendor_contacts (lines 1099-1164)
# ===========================================================================


def test_mine_vendor_contacts_no_contacts(scheduler_db, test_user):
    """No contacts found returns early."""
    test_user.access_token = "at_mine"
    scheduler_db.commit()

    mock_miner = MagicMock()
    mock_miner.scan_inbox = AsyncMock(return_value={"contacts_enriched": []})

    with (
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.connectors.email_mining.EmailMiner", return_value=mock_miner),
        patch("app.config.settings") as mock_settings,
    ):
        mock_settings.inbox_backfill_days = 180

        from app.scheduler import _mine_vendor_contacts

        asyncio.run(_mine_vendor_contacts(test_user, scheduler_db, is_backfill=False))


def test_mine_vendor_contacts_creates_new_card(scheduler_db, test_user):
    """New vendor contacts create VendorCard entries."""
    test_user.access_token = "at_mine"
    scheduler_db.commit()

    mock_miner = MagicMock()
    mock_miner.scan_inbox = AsyncMock(
        return_value={
            "contacts_enriched": [
                {
                    "vendor_name": "New Vendor Co",
                    "emails": ["contact@newvendor.com"],
                    "phones": ["+1-555-1234"],
                    "websites": ["newvendor.com"],
                }
            ]
        }
    )

    with (
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.connectors.email_mining.EmailMiner", return_value=mock_miner),
        patch("app.vendor_utils.normalize_vendor_name", return_value="new vendor co"),
        patch("app.vendor_utils.merge_emails_into_card", return_value=1) as mock_merge_emails,
        patch("app.vendor_utils.merge_phones_into_card") as mock_merge_phones,
        patch("app.config.settings") as mock_settings,
    ):
        mock_settings.inbox_backfill_days = 180

        from app.scheduler import _mine_vendor_contacts

        asyncio.run(_mine_vendor_contacts(test_user, scheduler_db, is_backfill=True))

        mock_merge_emails.assert_called_once()
        mock_merge_phones.assert_called_once()


def test_mine_vendor_contacts_updates_existing_card(scheduler_db, test_user, test_vendor_card):
    """Existing vendor cards get emails/phones merged."""
    test_user.access_token = "at_mine"
    scheduler_db.commit()

    mock_miner = MagicMock()
    mock_miner.scan_inbox = AsyncMock(
        return_value={
            "contacts_enriched": [
                {
                    "vendor_name": "Arrow Electronics",
                    "emails": ["new@arrow.com"],
                    "phones": ["+1-555-9999"],
                    "websites": [],
                }
            ]
        }
    )

    with (
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.connectors.email_mining.EmailMiner", return_value=mock_miner),
        patch("app.vendor_utils.normalize_vendor_name", return_value="arrow electronics"),
        patch("app.vendor_utils.merge_emails_into_card", return_value=1) as mock_merge_emails,
        patch("app.vendor_utils.merge_phones_into_card") as mock_merge_phones,
        patch("app.config.settings") as mock_settings,
    ):
        mock_settings.inbox_backfill_days = 180

        from app.scheduler import _mine_vendor_contacts

        asyncio.run(_mine_vendor_contacts(test_user, scheduler_db, is_backfill=False))

        mock_merge_emails.assert_called_once()
        mock_merge_phones.assert_called_once()


def test_mine_vendor_contacts_skips_empty_vendor_name(scheduler_db, test_user):
    """Contacts with empty vendor_name are skipped."""
    test_user.access_token = "at_mine"
    scheduler_db.commit()

    mock_miner = MagicMock()
    mock_miner.scan_inbox = AsyncMock(
        return_value={
            "contacts_enriched": [
                {"vendor_name": "", "emails": ["test@test.com"]},
            ]
        }
    )

    with (
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.connectors.email_mining.EmailMiner", return_value=mock_miner),
        patch("app.vendor_utils.merge_emails_into_card") as mock_merge,
        patch("app.config.settings") as mock_settings,
    ):
        mock_settings.inbox_backfill_days = 180

        from app.scheduler import _mine_vendor_contacts

        asyncio.run(_mine_vendor_contacts(test_user, scheduler_db, is_backfill=False))

        mock_merge.assert_not_called()


def test_mine_vendor_contacts_commit_error(scheduler_db, test_user):
    """Commit failure during contact mining is handled."""
    test_user.access_token = "at_mine"
    scheduler_db.commit()

    mock_miner = MagicMock()
    mock_miner.scan_inbox = AsyncMock(
        return_value={
            "contacts_enriched": [
                {
                    "vendor_name": "Test Vendor",
                    "emails": ["test@test.com"],
                    "phones": [],
                    "websites": [],
                }
            ]
        }
    )

    original_commit = scheduler_db.commit
    call_count = [0]

    def _failing_commit():
        call_count[0] += 1
        if call_count[0] > 2:
            raise Exception("commit failed")
        return original_commit()

    with (
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.connectors.email_mining.EmailMiner", return_value=mock_miner),
        patch("app.vendor_utils.normalize_vendor_name", return_value="test vendor"),
        patch("app.vendor_utils.merge_emails_into_card", return_value=1),
        patch("app.vendor_utils.merge_phones_into_card"),
        patch("app.config.settings") as mock_settings,
    ):
        mock_settings.inbox_backfill_days = 180

        scheduler_db.commit = _failing_commit
        from app.scheduler import _mine_vendor_contacts

        asyncio.run(_mine_vendor_contacts(test_user, scheduler_db, is_backfill=False))
        scheduler_db.commit = original_commit


# ===========================================================================
# _scan_outbound_rfqs (lines 1172-1225)
# ===========================================================================


def test_scan_outbound_rfqs_no_vendors(scheduler_db, test_user):
    """No vendors contacted returns early."""
    test_user.access_token = "at_out"
    scheduler_db.commit()

    mock_miner = MagicMock()
    mock_miner.scan_sent_items = AsyncMock(
        return_value={
            "rfqs_detected": 0,
            "vendors_contacted": {},
        }
    )

    with (
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.connectors.email_mining.EmailMiner", return_value=mock_miner),
        patch("app.config.settings") as mock_settings,
    ):
        mock_settings.inbox_backfill_days = 180

        from app.scheduler import _scan_outbound_rfqs

        asyncio.run(_scan_outbound_rfqs(test_user, scheduler_db, is_backfill=False))


def test_scan_outbound_rfqs_updates_vendor_card(scheduler_db, test_user, test_vendor_card):
    """Outbound RFQs update vendor card outreach counts."""
    test_user.access_token = "at_out"
    test_vendor_card.domain = "arrow.com"
    test_vendor_card.total_outreach = 5
    scheduler_db.commit()

    mock_miner = MagicMock()
    mock_miner.scan_sent_items = AsyncMock(
        return_value={
            "rfqs_detected": 3,
            "vendors_contacted": {"arrow.com": 3},
        }
    )

    with (
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.connectors.email_mining.EmailMiner", return_value=mock_miner),
        patch("app.config.settings") as mock_settings,
    ):
        mock_settings.inbox_backfill_days = 180

        from app.scheduler import _scan_outbound_rfqs

        asyncio.run(_scan_outbound_rfqs(test_user, scheduler_db, is_backfill=False))

    scheduler_db.refresh(test_vendor_card)
    assert test_vendor_card.total_outreach == 8
    assert test_vendor_card.last_contact_at is not None


def test_scan_outbound_rfqs_unmatched_domain(scheduler_db, test_user):
    """Domains without matching vendor cards are ignored."""
    test_user.access_token = "at_out"
    scheduler_db.commit()

    mock_miner = MagicMock()
    mock_miner.scan_sent_items = AsyncMock(
        return_value={
            "rfqs_detected": 2,
            "vendors_contacted": {"unknown-vendor.com": 2},
        }
    )

    with (
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.connectors.email_mining.EmailMiner", return_value=mock_miner),
        patch("app.config.settings") as mock_settings,
    ):
        mock_settings.inbox_backfill_days = 180

        from app.scheduler import _scan_outbound_rfqs

        asyncio.run(_scan_outbound_rfqs(test_user, scheduler_db, is_backfill=False))


def test_scan_outbound_rfqs_commit_error(scheduler_db, test_user, test_vendor_card):
    """Commit failure during outbound scan is handled."""
    test_user.access_token = "at_out"
    test_vendor_card.domain = "arrow.com"
    scheduler_db.commit()

    mock_miner = MagicMock()
    mock_miner.scan_sent_items = AsyncMock(
        return_value={
            "rfqs_detected": 1,
            "vendors_contacted": {"arrow.com": 1},
        }
    )

    original_commit = scheduler_db.commit
    call_count = [0]

    def _failing_commit():
        call_count[0] += 1
        if call_count[0] > 1:
            raise Exception("commit failed")
        return original_commit()

    with (
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.connectors.email_mining.EmailMiner", return_value=mock_miner),
        patch("app.config.settings") as mock_settings,
    ):
        mock_settings.inbox_backfill_days = 180

        scheduler_db.commit = _failing_commit
        from app.scheduler import _scan_outbound_rfqs

        asyncio.run(_scan_outbound_rfqs(test_user, scheduler_db, is_backfill=True))
        scheduler_db.commit = original_commit


def test_scan_outbound_rfqs_fallback_name_match(scheduler_db, test_user, test_vendor_card):
    """Vendor card matched by normalized_name prefix when domain doesn't match."""
    test_user.access_token = "at_out"
    test_vendor_card.domain = None
    test_vendor_card.normalized_name = "arrow"
    scheduler_db.commit()

    mock_miner = MagicMock()
    mock_miner.scan_sent_items = AsyncMock(
        return_value={
            "rfqs_detected": 1,
            "vendors_contacted": {"arrow.com": 1},
        }
    )

    with (
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.connectors.email_mining.EmailMiner", return_value=mock_miner),
        patch("app.config.settings") as mock_settings,
    ):
        mock_settings.inbox_backfill_days = 180

        from app.scheduler import _scan_outbound_rfqs

        asyncio.run(_scan_outbound_rfqs(test_user, scheduler_db, is_backfill=False))

    scheduler_db.refresh(test_vendor_card)
    assert (test_vendor_card.total_outreach or 0) >= 1


# ===========================================================================
# _sync_user_contacts (lines 1253-1334)
# ===========================================================================


def test_sync_user_contacts_empty(scheduler_db, test_user):
    """No contacts from Graph API — updates sync timestamp only."""
    test_user.access_token = "at_sync"
    test_user.last_contacts_sync = None
    scheduler_db.commit()

    mock_gc = MagicMock()
    mock_gc.delta_query = AsyncMock(return_value=([], "delta-token"))

    with patch("app.utils.graph_client.GraphClient", return_value=mock_gc):
        from app.scheduler import _sync_user_contacts

        asyncio.run(_sync_user_contacts(test_user, scheduler_db))

    assert test_user.last_contacts_sync is not None


def test_sync_user_contacts_creates_vendor_card(scheduler_db, test_user):
    """Outlook contacts create VendorCard entries."""
    test_user.access_token = "at_sync"
    scheduler_db.commit()

    mock_gc = MagicMock()
    mock_gc.delta_query = AsyncMock(
        return_value=(
            [
                {
                    "companyName": "New Outlook Co",
                    "displayName": "Jane Doe",
                    "emailAddresses": [{"address": "jane@outlookco.com"}],
                    "businessPhones": ["+1-555-0001"],
                    "mobilePhone": "+1-555-0002",
                }
            ],
            "delta-token",
        )
    )

    with (
        patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
        patch("app.vendor_utils.normalize_vendor_name", return_value="new outlook co"),
        patch("app.vendor_utils.merge_emails_into_card", return_value=1) as mock_merge_e,
        patch("app.vendor_utils.merge_phones_into_card") as mock_merge_p,
    ):
        from app.scheduler import _sync_user_contacts

        asyncio.run(_sync_user_contacts(test_user, scheduler_db))

        mock_merge_e.assert_called_once()
        mock_merge_p.assert_called_once()
        # Mobile phone should be included
        phones_arg = mock_merge_p.call_args[0][1]
        assert "+1-555-0002" in phones_arg


def test_sync_user_contacts_uses_display_name_fallback(scheduler_db, test_user):
    """When companyName is empty, displayName is used."""
    test_user.access_token = "at_sync"
    scheduler_db.commit()

    mock_gc = MagicMock()
    mock_gc.delta_query = AsyncMock(
        return_value=(
            [
                {
                    "companyName": None,
                    "displayName": "Solo Contact",
                    "emailAddresses": [{"address": "solo@example.com"}],
                    "businessPhones": [],
                    "mobilePhone": None,
                }
            ],
            "delta-token",
        )
    )

    with (
        patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
        patch("app.vendor_utils.normalize_vendor_name", return_value="solo contact"),
        patch("app.vendor_utils.merge_emails_into_card", return_value=0),
        patch("app.vendor_utils.merge_phones_into_card"),
    ):
        from app.scheduler import _sync_user_contacts

        asyncio.run(_sync_user_contacts(test_user, scheduler_db))


def test_sync_user_contacts_skips_short_company(scheduler_db, test_user):
    """Companies with names shorter than 2 chars are skipped."""
    test_user.access_token = "at_sync"
    scheduler_db.commit()

    mock_gc = MagicMock()
    mock_gc.delta_query = AsyncMock(
        return_value=(
            [
                {
                    "companyName": "X",
                    "displayName": "X",
                    "emailAddresses": [],
                    "businessPhones": [],
                    "mobilePhone": None,
                }
            ],
            "delta-token",
        )
    )

    with (
        patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
        patch("app.vendor_utils.merge_emails_into_card") as mock_merge,
    ):
        from app.scheduler import _sync_user_contacts

        asyncio.run(_sync_user_contacts(test_user, scheduler_db))

        mock_merge.assert_not_called()


def test_sync_user_contacts_graph_error(scheduler_db, test_user):
    """Graph API error during contacts sync is handled."""
    test_user.access_token = "at_sync"
    scheduler_db.commit()

    mock_gc = MagicMock()
    mock_gc.delta_query = AsyncMock(side_effect=Exception("Graph API error"))

    with patch("app.utils.graph_client.GraphClient", return_value=mock_gc):
        from app.scheduler import _sync_user_contacts

        asyncio.run(_sync_user_contacts(test_user, scheduler_db))

    # last_contacts_sync should NOT be updated since sync failed
    assert test_user.last_contacts_sync is None


def test_sync_user_contacts_commit_error(scheduler_db, test_user):
    """Commit failure during contacts sync is handled."""
    test_user.access_token = "at_sync"
    scheduler_db.commit()

    mock_gc = MagicMock()
    mock_gc.delta_query = AsyncMock(
        return_value=(
            [
                {
                    "companyName": "Commit Fail Co",
                    "displayName": "Test",
                    "emailAddresses": [],
                    "businessPhones": [],
                    "mobilePhone": None,
                }
            ],
            "delta-token",
        )
    )

    original_commit = scheduler_db.commit
    call_count = [0]

    def _failing_commit():
        call_count[0] += 1
        if call_count[0] > 1:
            raise Exception("commit failed")
        return original_commit()

    with (
        patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
        patch("app.vendor_utils.normalize_vendor_name", return_value="commit fail co"),
        patch("app.vendor_utils.merge_emails_into_card", return_value=0),
        patch("app.vendor_utils.merge_phones_into_card"),
    ):
        scheduler_db.commit = _failing_commit
        from app.scheduler import _sync_user_contacts

        asyncio.run(_sync_user_contacts(test_user, scheduler_db))
        scheduler_db.commit = original_commit


def test_sync_user_contacts_flush_conflict(scheduler_db, test_user):
    """Flush conflict for new VendorCard is handled gracefully."""
    test_user.access_token = "at_sync"
    scheduler_db.commit()

    mock_gc = MagicMock()
    mock_gc.delta_query = AsyncMock(
        return_value=(
            [
                {
                    "companyName": "Conflict Co",
                    "displayName": "Test",
                    "emailAddresses": [{"address": "test@conflict.com"}],
                    "businessPhones": [],
                    "mobilePhone": None,
                }
            ],
            "delta-token",
        )
    )

    original_flush = scheduler_db.flush

    def _failing_flush():
        raise Exception("unique constraint violation")

    with (
        patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
        patch("app.vendor_utils.normalize_vendor_name", return_value="conflict co"),
    ):
        scheduler_db.flush = _failing_flush
        from app.scheduler import _sync_user_contacts

        asyncio.run(_sync_user_contacts(test_user, scheduler_db))
        scheduler_db.flush = original_flush


def test_sync_user_contacts_existing_card(scheduler_db, test_user, test_vendor_card):
    """Existing vendor cards get updated with Outlook contact data."""
    test_user.access_token = "at_sync"
    scheduler_db.commit()

    mock_gc = MagicMock()
    mock_gc.delta_query = AsyncMock(
        return_value=(
            [
                {
                    "companyName": "Arrow Electronics",
                    "displayName": "Arrow Rep",
                    "emailAddresses": [{"address": "rep@arrow.com"}],
                    "businessPhones": ["+1-555-0300"],
                    "mobilePhone": None,
                }
            ],
            "delta-token",
        )
    )

    with (
        patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
        patch("app.vendor_utils.normalize_vendor_name", return_value="arrow electronics"),
        patch("app.vendor_utils.merge_emails_into_card", return_value=1) as mock_merge_e,
        patch("app.vendor_utils.merge_phones_into_card") as mock_merge_p,
    ):
        from app.scheduler import _sync_user_contacts

        asyncio.run(_sync_user_contacts(test_user, scheduler_db))

        mock_merge_e.assert_called_once()
        mock_merge_p.assert_called_once()


# ===========================================================================
# _job_engagement_scoring: naive datetime handling (line 443-444)
# ===========================================================================


def test_engagement_scoring_naive_datetime(scheduler_db):
    """Vendor with naive vendor_score_computed_at gets UTC-ified."""
    card = VendorCard(
        normalized_name="naive dt vendor",
        display_name="Naive DT Vendor",
        emails=[],
        phones=[],
        # Naive datetime (no tzinfo) — recent enough to skip
        vendor_score_computed_at=datetime.now() - timedelta(hours=2),
    )
    scheduler_db.add(card)
    scheduler_db.commit()

    with patch("app.scheduler._compute_vendor_scores_job", new_callable=AsyncMock) as mock_compute:
        from app.scheduler import _job_engagement_scoring

        asyncio.run(_job_engagement_scoring())
        # Naive datetime should be made UTC-aware; 2h old = skip
        mock_compute.assert_not_called()


# ===========================================================================
# _mine_vendor_contacts: flush conflict (new card)
# ===========================================================================


def test_mine_vendor_contacts_flush_conflict(scheduler_db, test_user):
    """Flush conflict for new VendorCard during mining is handled."""
    test_user.access_token = "at_mine"
    scheduler_db.commit()

    mock_miner = MagicMock()
    mock_miner.scan_inbox = AsyncMock(
        return_value={
            "contacts_enriched": [
                {
                    "vendor_name": "Conflict Vendor",
                    "emails": ["test@conflict.com"],
                    "phones": [],
                    "websites": [],
                }
            ]
        }
    )

    original_flush = scheduler_db.flush

    def _failing_flush():
        raise Exception("unique constraint")

    with (
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.connectors.email_mining.EmailMiner", return_value=mock_miner),
        patch("app.vendor_utils.normalize_vendor_name", return_value="conflict vendor"),
        patch("app.config.settings") as mock_settings,
    ):
        mock_settings.inbox_backfill_days = 180

        scheduler_db.flush = _failing_flush
        from app.scheduler import _mine_vendor_contacts

        asyncio.run(_mine_vendor_contacts(test_user, scheduler_db, is_backfill=False))
        scheduler_db.flush = original_flush


# ===========================================================================
# _deep_email_mining: per_domain with empty sender_names
# ===========================================================================


def test_deep_email_mining_empty_sender_names(scheduler_db, test_user):
    """per_domain with empty sender_names doesn't crash."""
    test_user.refresh_token = "rt_deep"
    test_user.access_token = "at_deep"
    test_user.m365_connected = True
    test_user.last_deep_email_scan = None
    scheduler_db.commit()

    mock_miner_instance = MagicMock()
    mock_miner_instance.deep_scan_inbox = AsyncMock(
        return_value={
            "messages_scanned": 10,
            "contacts_found": 1,
            "per_domain": {
                "example.com": {
                    "emails": ["user@example.com"],
                    "sender_names": [],  # empty
                }
            },
        }
    )

    with (
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.connectors.email_mining.EmailMiner", return_value=mock_miner_instance),
        patch("app.services.deep_enrichment_service.link_contact_to_entities") as mock_link,
    ):
        from app.scheduler import _job_deep_email_mining

        asyncio.run(_job_deep_email_mining())
        mock_link.assert_called_once()
        # full_name should be None when sender_names is empty
        call_kwargs = mock_link.call_args[0][2]
        assert call_kwargs.get("full_name") is None


# ===========================================================================
# _download_and_import_stock_list: MaterialCard flush conflict
# ===========================================================================


# ===========================================================================
# Coverage: _safe_scan returning early when user not found (line 335)
# ===========================================================================


def test_inbox_scan_safe_scan_user_not_found(scheduler_db, test_user):
    """_safe_scan returns early when user is not found in the scan session."""
    test_user.refresh_token = "rt_notfound"
    test_user.access_token = "at_notfound"
    test_user.m365_connected = True
    test_user.last_inbox_scan = None
    scheduler_db.commit()

    user_id = test_user.id

    # Make the inner SessionLocal().get(User, user_id) return None
    # while the outer query still finds the user
    original_get = scheduler_db.get
    call_count = [0]

    def _get_none_second_time(model, id_):
        call_count[0] += 1
        # First get is in _safe_scan; return None to trigger the early return
        if call_count[0] >= 1:
            return None
        return original_get(model, id_)

    with (
        patch("app.scheduler._scan_user_inbox", new_callable=AsyncMock) as mock_scan,
        patch("app.config.settings") as mock_settings,
    ):
        mock_settings.inbox_scan_interval_min = 30

        # Temporarily override get
        scheduler_db.get = _get_none_second_time
        from app.scheduler import _job_inbox_scan

        asyncio.run(_job_inbox_scan())
        scheduler_db.get = original_get

        # _scan_user_inbox should NOT have been called
        mock_scan.assert_not_called()


# ===========================================================================
# Coverage: _safe_scan timeout commit exception (lines 345-346)
# ===========================================================================


def test_inbox_scan_safe_scan_timeout_commit_exception(scheduler_db, test_user):
    """Exception during timeout recovery commit (lines 345-346) is handled.

    The _safe_scan() function catches asyncio.TimeoutError, does rollback,
    then tries to set m365_error_reason and commit. If THAT commit fails,
    there is a bare except that does another rollback.
    """
    test_user.refresh_token = "rt_tce"
    test_user.access_token = "at_tce"
    test_user.m365_connected = True
    test_user.last_inbox_scan = None
    scheduler_db.commit()

    original_commit = scheduler_db.commit

    # Track how many times commit is called — fail on the recovery commit
    # inside the timeout handler (the one that commits m365_error_reason).
    # Commits before this: test setup commit (already done).
    # Inside _safe_scan's TimeoutError handler:
    #   scan_db.rollback()
    #   user = scan_db.get(User, user_id)
    #   user.m365_error_reason = "Inbox scan timed out"
    #   scan_db.commit()  <-- THIS needs to fail
    commit_count = [0]

    def _fail_recovery_commit():
        commit_count[0] += 1
        raise Exception("commit during timeout recovery failed")

    with (
        patch("app.scheduler._scan_user_inbox", new_callable=AsyncMock) as mock_scan,
        patch("app.config.settings") as mock_settings,
    ):
        mock_settings.inbox_scan_interval_min = 30

        # Make _scan_user_inbox trigger TimeoutError through wait_for
        async def _slow(*a, **kw):
            await asyncio.sleep(9999)

        mock_scan.side_effect = _slow

        original_wait_for = asyncio.wait_for

        async def _mock_wait_for(coro, timeout=None):
            try:
                coro.close()
            except Exception:
                pass
            raise asyncio.TimeoutError()

        # Replace commit so the recovery commit fails
        scheduler_db.commit = _fail_recovery_commit

        with patch("asyncio.wait_for", side_effect=_mock_wait_for):
            from app.scheduler import _job_inbox_scan

            asyncio.run(_job_inbox_scan())

        scheduler_db.commit = original_commit


# ===========================================================================
# Coverage: _job_contacts_sync user not found (line 412)
# ===========================================================================


def test_contacts_sync_user_not_found(scheduler_db, test_user):
    """_job_contacts_sync continues when user is not found in sync session."""
    test_user.refresh_token = "rt_nf"
    test_user.access_token = "at_nf"
    test_user.m365_connected = True
    test_user.last_contacts_sync = None
    scheduler_db.commit()

    original_get = scheduler_db.get
    get_count = [0]

    def _get_none_on_second(model, id_):
        get_count[0] += 1
        # Return None when the per-user sync tries to get the user
        if get_count[0] >= 1:
            return None
        return original_get(model, id_)

    with patch("app.scheduler._sync_user_contacts", new_callable=AsyncMock) as mock_sync:
        scheduler_db.get = _get_none_on_second
        from app.scheduler import _job_contacts_sync

        asyncio.run(_job_contacts_sync())
        scheduler_db.get = original_get

        mock_sync.assert_not_called()


# ===========================================================================
# Coverage: _download_and_import commit failure (lines 1044-1047)
# ===========================================================================


def test_download_and_import_stock_list_final_commit_fails(scheduler_db, test_user):
    """Final db.commit() failure in _download_and_import is handled (lines 1044-1047)."""
    import base64

    test_user.access_token = "at_dl"
    scheduler_db.commit()

    mock_gc = MagicMock()
    mock_gc.get_json = AsyncMock(
        return_value={
            "contentBytes": base64.b64encode(b"data").decode(),
        }
    )

    rows = [{"mpn": "COMMITFAIL1", "qty": 100}]

    # Use a mock db so we can precisely control commit behavior
    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.all.return_value = []
    mock_db.flush = MagicMock()
    mock_db.add = MagicMock()
    mock_db.commit = MagicMock(side_effect=Exception("final commit failed"))
    mock_db.rollback = MagicMock()

    with (
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
        patch("app.utils.file_validation.validate_file", return_value=(True, "text/csv")),
        patch("app.services.attachment_parser.parse_attachment", new_callable=AsyncMock, return_value=rows),
        patch("app.vendor_utils.normalize_vendor_name", return_value="arrow"),
        patch("app.services.activity_service.match_email_to_entity", return_value=None),
    ):
        from app.scheduler import _download_and_import_stock_list

        asyncio.run(
            _download_and_import_stock_list(
                test_user,
                mock_db,
                message_id="msg1",
                attachment_id="att1",
                filename="stock.csv",
                vendor_name="Arrow",
                vendor_email="sales@arrow.com",
            )
        )
        mock_db.rollback.assert_called()


# ===========================================================================
# Coverage: _mine_vendor_contacts final commit error (lines 1162-1164)
# ===========================================================================


def test_mine_vendor_contacts_final_commit_error(scheduler_db, test_user):
    """Final commit error in _mine_vendor_contacts is handled (lines 1162-1164)."""
    test_user.access_token = "at_mine"
    scheduler_db.commit()

    mock_miner = MagicMock()
    mock_miner.scan_inbox = AsyncMock(
        return_value={
            "contacts_enriched": [
                {
                    "vendor_name": "Commit Error Vendor",
                    "emails": ["err@vendor.com"],
                    "phones": [],
                    "websites": [],
                }
            ]
        }
    )

    # Use a mock db to precisely control commit behavior
    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.all.return_value = []
    mock_db.flush = MagicMock()
    mock_db.add = MagicMock()
    mock_db.commit = MagicMock(side_effect=Exception("final commit failed"))
    mock_db.rollback = MagicMock()

    mock_user = MagicMock()
    mock_user.access_token = "at_mine"

    with (
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.connectors.email_mining.EmailMiner", return_value=mock_miner),
        patch("app.vendor_utils.normalize_vendor_name", return_value="commit error vendor"),
        patch("app.vendor_utils.merge_emails_into_card", return_value=1),
        patch("app.vendor_utils.merge_phones_into_card"),
        patch("app.config.settings") as mock_settings,
    ):
        mock_settings.inbox_backfill_days = 180

        from app.scheduler import _mine_vendor_contacts

        asyncio.run(_mine_vendor_contacts(mock_user, mock_db, is_backfill=False))
        mock_db.rollback.assert_called()


# ===========================================================================
# Coverage: _scan_outbound_rfqs final commit error (lines 1223-1225)
# ===========================================================================


def test_scan_outbound_rfqs_final_commit_error(scheduler_db, test_user, test_vendor_card):
    """Final commit error in _scan_outbound_rfqs is handled (lines 1223-1225)."""
    test_user.access_token = "at_out"
    scheduler_db.commit()

    mock_miner = MagicMock()
    mock_miner.scan_sent_items = AsyncMock(
        return_value={
            "rfqs_detected": 1,
            "vendors_contacted": {"arrow.com": 1},
        }
    )

    # Use a mock db so the commit failure is targeted
    mock_card = MagicMock()
    mock_card.total_outreach = 5
    mock_card.last_contact_at = None
    mock_card.domain = "arrow.com"
    mock_card.normalized_name = "arrow"

    mock_db = MagicMock()
    # Make query chain return our mock card for domain lookup
    mock_db.query.return_value.filter.return_value.all.return_value = [mock_card]
    mock_db.commit = MagicMock(side_effect=Exception("final commit failed"))
    mock_db.rollback = MagicMock()

    mock_user = MagicMock()
    mock_user.access_token = "at_out"

    with (
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.connectors.email_mining.EmailMiner", return_value=mock_miner),
        patch("app.config.settings") as mock_settings,
    ):
        mock_settings.inbox_backfill_days = 180

        from app.scheduler import _scan_outbound_rfqs

        asyncio.run(_scan_outbound_rfqs(mock_user, mock_db, is_backfill=False))
        mock_db.rollback.assert_called()


def test_download_and_import_stock_list_card_flush_conflict(scheduler_db, test_user):
    """MaterialCard flush conflict is handled (rollback + continue)."""
    import base64

    test_user.access_token = "at_dl"
    scheduler_db.commit()

    mock_gc = MagicMock()
    mock_gc.get_json = AsyncMock(
        return_value={
            "contentBytes": base64.b64encode(b"data").decode(),
        }
    )

    rows = [
        {"mpn": "CONFLICT1", "qty": 100},
        {"mpn": "NOCONFLICT", "qty": 200},
    ]

    original_flush = scheduler_db.flush
    flush_count = [0]

    def _sometimes_failing_flush():
        flush_count[0] += 1
        # Fail on first flush (the CONFLICT1 card) but succeed on second
        if flush_count[0] == 1:
            raise Exception("unique constraint")
        return original_flush()

    with (
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
        patch("app.utils.file_validation.validate_file", return_value=(True, "text/csv")),
        patch("app.services.attachment_parser.parse_attachment", new_callable=AsyncMock, return_value=rows),
        patch("app.vendor_utils.normalize_vendor_name", return_value="arrow"),
        patch("app.services.activity_service.match_email_to_entity", return_value=None),
        patch("app.services.teams.send_stock_match_alert", new_callable=AsyncMock),
    ):
        scheduler_db.flush = _sometimes_failing_flush
        from app.scheduler import _download_and_import_stock_list

        asyncio.run(
            _download_and_import_stock_list(
                test_user,
                scheduler_db,
                message_id="msg1",
                attachment_id="att1",
                filename="stock.csv",
                vendor_name="Arrow",
                vendor_email="sales@arrow.com",
            )
        )
        scheduler_db.flush = original_flush


# ===========================================================================
# _job_token_refresh: Redis lock paths (lines 296-299, 306-309)
# ===========================================================================


def test_token_refresh_redis_lock_acquired(scheduler_db, test_user):
    """Token refresh acquires Redis lock and refreshes user."""
    test_user.refresh_token = "rt_lock"
    test_user.token_expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
    test_user.access_token = "old_token"
    scheduler_db.commit()

    mock_redis = MagicMock()
    mock_redis.set.return_value = True  # Lock acquired

    with (
        patch("app.scheduler.refresh_user_token", new_callable=AsyncMock) as mock_refresh,
        patch("app.cache.intel_cache._get_redis", return_value=mock_redis),
    ):
        from app.scheduler import _job_token_refresh

        asyncio.run(_job_token_refresh())
        mock_refresh.assert_called_once()
        mock_redis.set.assert_called_once()
        mock_redis.delete.assert_called_once()


def test_token_refresh_redis_lock_not_acquired(scheduler_db, test_user):
    """Token refresh skipped when Redis lock is held by another process."""
    test_user.refresh_token = "rt_lock_held"
    test_user.token_expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
    test_user.access_token = "old_token"
    scheduler_db.commit()

    mock_redis = MagicMock()
    mock_redis.set.return_value = False  # Lock NOT acquired

    with (
        patch("app.scheduler.refresh_user_token", new_callable=AsyncMock) as mock_refresh,
        patch("app.cache.intel_cache._get_redis", return_value=mock_redis),
    ):
        from app.scheduler import _job_token_refresh

        asyncio.run(_job_token_refresh())
        mock_refresh.assert_not_called()  # Skipped due to lock


def test_token_refresh_redis_delete_exception(scheduler_db, test_user):
    """Redis lock delete exception in finally block is swallowed."""
    test_user.refresh_token = "rt_lock_del_err"
    test_user.token_expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
    test_user.access_token = "old_token"
    scheduler_db.commit()

    mock_redis = MagicMock()
    mock_redis.set.return_value = True  # Lock acquired
    mock_redis.delete.side_effect = Exception("Redis connection lost")

    with (
        patch("app.scheduler.refresh_user_token", new_callable=AsyncMock) as mock_refresh,
        patch("app.cache.intel_cache._get_redis", return_value=mock_redis),
    ):
        from app.scheduler import _job_token_refresh

        # Should not raise — exception in finally is swallowed
        asyncio.run(_job_token_refresh())
        mock_refresh.assert_called_once()


# ===========================================================================
# _job_site_ownership_sweep (lines 521-531)
# ===========================================================================


def test_site_ownership_sweep_delegates(scheduler_db):
    """Site ownership sweep delegates to run_site_ownership_sweep."""
    with patch("app.services.ownership_service.run_site_ownership_sweep") as mock_sweep:
        from app.scheduler import _job_site_ownership_sweep

        asyncio.run(_job_site_ownership_sweep())
        mock_sweep.assert_called_once_with(scheduler_db)


def test_site_ownership_sweep_error_handling(scheduler_db):
    """Site ownership sweep handles errors gracefully."""
    with patch(
        "app.services.ownership_service.run_site_ownership_sweep",
        side_effect=Exception("Sweep failed"),
    ):
        from app.scheduler import _job_site_ownership_sweep

        # Should not raise
        asyncio.run(_job_site_ownership_sweep())


# ===========================================================================
# _job_contact_scoring (lines 828-849)
# ===========================================================================


def test_contact_scoring_runs_successfully(scheduler_db):
    """Contact scoring job delegates to compute_all_contact_scores."""
    with patch("app.services.contact_intelligence.compute_all_contact_scores") as mock_compute:
        mock_compute.return_value = {"updated": 10, "skipped": 0}
        from app.scheduler import _job_contact_scoring

        asyncio.run(_job_contact_scoring())
        mock_compute.assert_called_once_with(scheduler_db)


def test_contact_scoring_timeout(scheduler_db):
    """Contact scoring handles TimeoutError gracefully."""

    async def _mock_wait_for(coro, timeout=None):
        try:
            coro.close()
        except Exception:
            pass
        raise asyncio.TimeoutError()

    with (
        patch("app.services.contact_intelligence.compute_all_contact_scores"),
        patch("asyncio.wait_for", side_effect=_mock_wait_for),
    ):
        from app.scheduler import _job_contact_scoring

        # Should not raise
        asyncio.run(_job_contact_scoring())


def test_contact_scoring_general_error(scheduler_db):
    """Contact scoring handles general exceptions gracefully."""
    with patch(
        "app.services.contact_intelligence.compute_all_contact_scores",
        side_effect=Exception("Scoring crashed"),
    ):
        from app.scheduler import _job_contact_scoring

        # Should not raise
        asyncio.run(_job_contact_scoring())


# ===========================================================================
# _job_contact_status_compute: 7-30 day continue + error (lines 1459, 1479-1481)
# ===========================================================================


def test_contact_status_compute_7_to_30_day_window(scheduler_db, test_user, test_company, test_customer_site):
    """Contacts with last activity 7-30 days ago keep current status (no downgrade)."""
    from app.models import SiteContact

    sc = SiteContact(
        customer_site_id=test_customer_site.id,
        full_name="Seven Day Contact",
        is_active=True,
        contact_status="active",
    )
    scheduler_db.add(sc)
    scheduler_db.flush()

    # Create an activity log 15 days ago for this site contact
    activity = ActivityLog(
        user_id=test_user.id,
        activity_type="email_sent",
        channel="outlook",
        site_contact_id=sc.id,
        auto_logged=True,
        occurred_at=datetime.now(timezone.utc) - timedelta(days=15),
        created_at=datetime.now(timezone.utc) - timedelta(days=15),
    )
    scheduler_db.add(activity)
    scheduler_db.commit()

    from app.scheduler import _job_contact_status_compute

    asyncio.run(_job_contact_status_compute())

    scheduler_db.refresh(sc)
    assert sc.contact_status == "active"  # Not downgraded in 7-30 day window


def test_contact_status_compute_champion_not_downgraded(scheduler_db, test_user, test_company, test_customer_site):
    """Line 1450: Champion contacts are never downgraded."""
    from app.models import SiteContact

    sc = SiteContact(
        customer_site_id=test_customer_site.id,
        full_name="Champion Contact",
        is_active=True,
        contact_status="champion",
    )
    scheduler_db.add(sc)
    scheduler_db.commit()

    from app.scheduler import _job_contact_status_compute

    asyncio.run(_job_contact_status_compute())

    scheduler_db.refresh(sc)
    assert sc.contact_status == "champion"


def test_contact_status_compute_active_recent(scheduler_db, test_user, test_company, test_customer_site):
    """Line 1456: contact with activity <= 7 days ago -> active."""
    from app.models import SiteContact

    sc = SiteContact(
        customer_site_id=test_customer_site.id,
        full_name="Recent Contact",
        is_active=True,
        contact_status="new",
    )
    scheduler_db.add(sc)
    scheduler_db.flush()

    activity = ActivityLog(
        user_id=test_user.id,
        activity_type="email_sent",
        channel="outlook",
        site_contact_id=sc.id,
        auto_logged=True,
        occurred_at=datetime.now(timezone.utc) - timedelta(days=3),
        created_at=datetime.now(timezone.utc) - timedelta(days=3),
    )
    scheduler_db.add(activity)
    scheduler_db.commit()

    from app.scheduler import _job_contact_status_compute

    asyncio.run(_job_contact_status_compute())

    scheduler_db.refresh(sc)
    assert sc.contact_status == "active"


def test_contact_status_compute_quiet_and_inactive(scheduler_db, test_user, test_company, test_customer_site):
    """Lines 1460-1463: 30-90 days -> quiet, >90 days -> inactive."""
    from app.models import SiteContact

    quiet_sc = SiteContact(
        customer_site_id=test_customer_site.id,
        full_name="Quiet Contact",
        is_active=True,
        contact_status="active",
    )
    inactive_sc = SiteContact(
        customer_site_id=test_customer_site.id,
        full_name="Inactive Contact",
        is_active=True,
        contact_status="active",
    )
    scheduler_db.add_all([quiet_sc, inactive_sc])
    scheduler_db.flush()

    # Activity 60 days ago -> quiet
    quiet_activity = ActivityLog(
        user_id=test_user.id,
        activity_type="email_sent",
        channel="outlook",
        site_contact_id=quiet_sc.id,
        auto_logged=True,
        occurred_at=datetime.now(timezone.utc) - timedelta(days=60),
        created_at=datetime.now(timezone.utc) - timedelta(days=60),
    )
    # Activity 120 days ago -> inactive
    inactive_activity = ActivityLog(
        user_id=test_user.id,
        activity_type="email_sent",
        channel="outlook",
        site_contact_id=inactive_sc.id,
        auto_logged=True,
        occurred_at=datetime.now(timezone.utc) - timedelta(days=120),
        created_at=datetime.now(timezone.utc) - timedelta(days=120),
    )
    scheduler_db.add_all([quiet_activity, inactive_activity])
    scheduler_db.commit()

    from app.scheduler import _job_contact_status_compute

    asyncio.run(_job_contact_status_compute())

    scheduler_db.refresh(quiet_sc)
    scheduler_db.refresh(inactive_sc)
    assert quiet_sc.contact_status == "quiet"
    assert inactive_sc.contact_status == "inactive"


def test_contact_status_compute_no_activity_old_created(scheduler_db, test_user, test_company, test_customer_site):
    """Lines 1465-1471: no activity + created >90 days ago -> inactive; recent created -> keep 'new'."""
    from app.models import SiteContact

    old_sc = SiteContact(
        customer_site_id=test_customer_site.id,
        full_name="Old No Activity",
        is_active=True,
        contact_status="new",
        created_at=datetime.now(timezone.utc) - timedelta(days=120),
    )
    new_sc = SiteContact(
        customer_site_id=test_customer_site.id,
        full_name="New No Activity",
        is_active=True,
        contact_status="new",
        created_at=datetime.now(timezone.utc) - timedelta(days=30),
    )
    scheduler_db.add_all([old_sc, new_sc])
    scheduler_db.commit()

    from app.scheduler import _job_contact_status_compute

    asyncio.run(_job_contact_status_compute())

    scheduler_db.refresh(old_sc)
    scheduler_db.refresh(new_sc)
    assert old_sc.contact_status == "inactive"
    assert new_sc.contact_status == "new"  # Kept as-is


def test_contact_status_compute_error_handler(scheduler_db):
    """Exception in _job_contact_status_compute is caught and rolled back."""
    with patch.object(scheduler_db, "query", side_effect=Exception("DB crash")):
        from app.scheduler import _job_contact_status_compute

        # Should not raise
        asyncio.run(_job_contact_status_compute())


# ── Reset Connector Errors ─────────────────────────────────────────────


def test_reset_connector_errors(scheduler_db):
    """_job_reset_connector_errors zeroes error_count_24h on all sources."""
    from app.models import ApiSource

    src1 = ApiSource(
        name="src_a",
        display_name="A",
        category="dist",
        source_type="api",
        status="live",
        error_count_24h=5,
    )
    src2 = ApiSource(
        name="src_b",
        display_name="B",
        category="broker",
        source_type="api",
        status="live",
        error_count_24h=0,
    )
    scheduler_db.add_all([src1, src2])
    scheduler_db.commit()

    from app.scheduler import _job_reset_connector_errors

    asyncio.run(_job_reset_connector_errors())

    scheduler_db.refresh(src1)
    scheduler_db.refresh(src2)
    assert src1.error_count_24h == 0
    assert src2.error_count_24h == 0


def test_reset_connector_errors_registered():
    """configure_scheduler registers the reset_connector_errors job."""
    configure_scheduler()
    job_ids = [j.id for j in scheduler.get_jobs()]
    assert "reset_connector_errors" in job_ids
    scheduler.remove_all_jobs()


def test_reset_connector_errors_exception(scheduler_db):
    """_job_reset_connector_errors handles DB exceptions gracefully (lines 1966-1968)."""
    with patch.object(scheduler_db, "query", side_effect=Exception("DB crashed")):
        from app.scheduler import _job_reset_connector_errors

        asyncio.run(_job_reset_connector_errors())


# ── Proactive Matching: expired branch (line 604) ────────────────────────


def test_proactive_matching_expired_branch(scheduler_db):
    """Line 604: When expire_old_matches returns a nonzero count, logger.info is called."""
    mock_scan = MagicMock(return_value={"matches_created": 0, "scanned": 5})
    mock_cph = MagicMock(return_value={"matches_created": 0, "scanned_offers": 3, "scanned_sightings": 2})
    mock_expire = MagicMock(return_value=7)  # 7 expired matches

    with (
        patch("app.services.proactive_matching.expire_old_matches", mock_expire),
        patch("app.services.proactive_service.scan_new_offers_for_matches", mock_scan),
        patch("app.services.proactive_matching.run_proactive_scan", mock_cph),
    ):
        from app.scheduler import _job_proactive_matching

        asyncio.run(_job_proactive_matching())


# ── Customer Enrichment Sweep (lines 924-939) ────────────────────────────


def test_customer_enrichment_sweep_success(scheduler_db):
    """Lines 924-939: _job_customer_enrichment_sweep happy path."""
    mock_batch = AsyncMock(return_value={"processed": 10, "enriched": 5})
    with patch("app.services.customer_enrichment_batch.run_customer_enrichment_batch", mock_batch):
        from app.scheduler import _job_customer_enrichment_sweep

        asyncio.run(_job_customer_enrichment_sweep())
    mock_batch.assert_called_once()


def test_customer_enrichment_sweep_error(scheduler_db):
    """Lines 935-937: exception rolls back."""
    mock_batch = AsyncMock(side_effect=Exception("Enrichment failed"))
    with patch("app.services.customer_enrichment_batch.run_customer_enrichment_batch", mock_batch):
        from app.scheduler import _job_customer_enrichment_sweep

        asyncio.run(_job_customer_enrichment_sweep())


# ── Email Reverification (lines 945-960) ────────────────────────────────


def test_email_reverification_success(scheduler_db):
    """Lines 945-960: _job_email_reverification happy path."""
    mock_reverify = AsyncMock(return_value={"processed": 20, "invalidated": 3})
    with patch("app.services.customer_enrichment_batch.run_email_reverification", mock_reverify):
        from app.scheduler import _job_email_reverification

        asyncio.run(_job_email_reverification())
    mock_reverify.assert_called_once()


def test_email_reverification_error(scheduler_db):
    """Lines 956-958: exception rolls back."""
    mock_reverify = AsyncMock(side_effect=Exception("Reverify failed"))
    with patch("app.services.customer_enrichment_batch.run_email_reverification", mock_reverify):
        from app.scheduler import _job_email_reverification

        asyncio.run(_job_email_reverification())


# ── Email Health Update timeout (lines 1021-1022) ────────────────────────


def test_email_health_update_timeout(scheduler_db):
    """Lines 1021-1022: asyncio.TimeoutError rolls back."""
    with patch("app.services.response_analytics.batch_update_email_health", side_effect=Exception("slow")):
        # We need to trigger the TimeoutError path — patch wait_for
        with patch("asyncio.wait_for", new_callable=AsyncMock, side_effect=asyncio.TimeoutError):
            from app.scheduler import _job_email_health_update

            asyncio.run(_job_email_health_update())


# ── Calendar Scan (lines 1043-1045, 1057, 1069-1074) ─────────────────────


def test_calendar_scan_user_query_error(scheduler_db):
    """Lines 1043-1045: exception in user query causes early return."""
    with patch.object(scheduler_db, "query", side_effect=Exception("DB error")):
        from app.scheduler import _job_calendar_scan

        asyncio.run(_job_calendar_scan())


def test_calendar_scan_user_not_found(scheduler_db, test_user):
    """Line 1057: user not found in scan_db returns early."""
    test_user.access_token = "at_cal"
    test_user.refresh_token = "rt_cal"
    test_user.m365_connected = True
    scheduler_db.commit()

    mock_scan_db = MagicMock()
    mock_scan_db.get.return_value = None  # user not found

    call_count = 0
    original_session_local = None

    def _session_factory():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # First call: main query for users
            return scheduler_db
        # Subsequent calls: per-user scan sessions
        return mock_scan_db

    with patch("app.database.SessionLocal", side_effect=_session_factory):
        from app.scheduler import _job_calendar_scan

        asyncio.run(_job_calendar_scan())


def test_calendar_scan_timeout(scheduler_db, test_user):
    """Lines 1069-1071: asyncio.TimeoutError in _safe_cal_scan."""
    test_user.access_token = "at_cal"
    test_user.refresh_token = "rt_cal"
    test_user.m365_connected = True
    scheduler_db.commit()

    mock_scan_db = MagicMock()
    mock_user = MagicMock()
    mock_user.id = test_user.id
    mock_user.email = test_user.email
    mock_scan_db.get.return_value = mock_user

    call_count = 0

    def _session_factory():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return scheduler_db
        return mock_scan_db

    with (
        patch("app.database.SessionLocal", side_effect=_session_factory),
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch(
            "app.services.calendar_intelligence.scan_calendar_events",
            new_callable=AsyncMock,
            side_effect=asyncio.TimeoutError,
        ),
    ):
        from app.scheduler import _job_calendar_scan

        asyncio.run(_job_calendar_scan())

    mock_scan_db.rollback.assert_called()


def test_calendar_scan_generic_error(scheduler_db, test_user):
    """Lines 1072-1074: generic exception in _safe_cal_scan."""
    test_user.access_token = "at_cal"
    test_user.refresh_token = "rt_cal"
    test_user.m365_connected = True
    scheduler_db.commit()

    mock_scan_db = MagicMock()
    mock_user = MagicMock()
    mock_user.id = test_user.id
    mock_user.email = test_user.email
    mock_scan_db.get.return_value = mock_user

    call_count = 0

    def _session_factory():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return scheduler_db
        return mock_scan_db

    with (
        patch("app.database.SessionLocal", side_effect=_session_factory),
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch(
            "app.services.calendar_intelligence.scan_calendar_events",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ),
    ):
        from app.scheduler import _job_calendar_scan

        asyncio.run(_job_calendar_scan())

    mock_scan_db.rollback.assert_called()


# ── Contacts Sync: delta token paths (lines 1574-1575, 1587-1601, 1638-1641, 1662-1664) ──


def test_sync_user_contacts_delta_token_update_existing(scheduler_db, test_user):
    """Lines 1574-1575: existing sync_state gets delta_token updated."""
    from app.models.pipeline import SyncState

    test_user.access_token = "at_sync"
    scheduler_db.commit()

    # Pre-create a SyncState record
    ss = SyncState(user_id=test_user.id, folder="contacts_sync", delta_token="old-token")
    scheduler_db.add(ss)
    scheduler_db.commit()

    mock_gc = MagicMock()
    mock_gc.delta_query = AsyncMock(return_value=([], "new-delta-token"))

    with patch("app.utils.graph_client.GraphClient", return_value=mock_gc):
        from app.scheduler import _sync_user_contacts

        asyncio.run(_sync_user_contacts(test_user, scheduler_db))

    scheduler_db.refresh(ss)
    assert ss.delta_token == "new-delta-token"


def test_sync_user_contacts_delta_expired_with_sync_state(scheduler_db, test_user):
    """Lines 1587-1588: GraphSyncStateExpired with existing sync_state clears delta_token."""
    from app.models.pipeline import SyncState
    from app.utils.graph_client import GraphSyncStateExpired

    test_user.access_token = "at_sync"
    scheduler_db.commit()

    ss = SyncState(user_id=test_user.id, folder="contacts_sync", delta_token="expired-token")
    scheduler_db.add(ss)
    scheduler_db.commit()

    mock_gc = MagicMock()
    mock_gc.delta_query = AsyncMock(side_effect=GraphSyncStateExpired("token expired"))
    mock_gc.get_all_pages = AsyncMock(return_value=[])

    with patch("app.utils.graph_client.GraphClient", return_value=mock_gc):
        from app.scheduler import _sync_user_contacts

        asyncio.run(_sync_user_contacts(test_user, scheduler_db))

    scheduler_db.refresh(ss)
    assert ss.delta_token is None


def test_sync_user_contacts_delta_expired_full_resync_fails(scheduler_db, test_user):
    """Lines 1599-1601: GraphSyncStateExpired followed by full pull failure returns early."""
    from app.utils.graph_client import GraphSyncStateExpired

    test_user.access_token = "at_sync"
    test_user.last_contacts_sync = None
    scheduler_db.commit()

    mock_gc = MagicMock()
    mock_gc.delta_query = AsyncMock(side_effect=GraphSyncStateExpired("token expired"))
    mock_gc.get_all_pages = AsyncMock(side_effect=Exception("Full pull failed"))

    with patch("app.utils.graph_client.GraphClient", return_value=mock_gc):
        from app.scheduler import _sync_user_contacts

        asyncio.run(_sync_user_contacts(test_user, scheduler_db))

    # Should not update last_contacts_sync since sync failed
    assert test_user.last_contacts_sync is None


def test_sync_user_contacts_vendor_card_flush_conflict(scheduler_db, test_user):
    """Lines 1638-1641: VendorCard flush conflict rolls back and continues."""
    test_user.access_token = "at_sync"
    scheduler_db.commit()

    contact = {
        "companyName": "Conflict Co",
        "displayName": "Someone",
        "emailAddresses": [{"address": "x@conflict.com"}],
        "businessPhones": [],
        "mobilePhone": None,
    }

    mock_gc = MagicMock()
    mock_gc.delta_query = AsyncMock(return_value=([contact], "delta-token"))

    # Make flush raise an error to simulate a uniqueness conflict
    original_flush = scheduler_db.flush
    flush_call_count = 0

    def flaky_flush(*args, **kwargs):
        nonlocal flush_call_count
        flush_call_count += 1
        # The first flush comes from the delta_token persist (line 1583)
        # The second flush is for the new VendorCard (line 1636)
        if flush_call_count == 2:
            raise Exception("Uniqueness conflict")
        return original_flush(*args, **kwargs)

    with (
        patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
        patch.object(scheduler_db, "flush", side_effect=flaky_flush),
        patch("app.vendor_utils.normalize_vendor_name", return_value="conflict co"),
    ):
        from app.scheduler import _sync_user_contacts

        asyncio.run(_sync_user_contacts(test_user, scheduler_db))


def test_sync_user_contacts_commit_error_final(scheduler_db, test_user):
    """Lines 1662-1664: commit failure in final sync."""
    test_user.access_token = "at_sync"
    scheduler_db.commit()

    mock_gc = MagicMock()
    mock_gc.delta_query = AsyncMock(return_value=([], "delta-token"))

    original_commit = scheduler_db.commit

    def failing_commit(*args, **kwargs):
        raise Exception("Commit failed")

    with (
        patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
        patch.object(scheduler_db, "commit", side_effect=failing_commit),
    ):
        from app.scheduler import _sync_user_contacts

        asyncio.run(_sync_user_contacts(test_user, scheduler_db))


# ── Proactive Offer Expiry (lines 1804-1825) ────────────────────────────


def test_proactive_offer_expiry_expires_old(scheduler_db, test_user, test_company, test_customer_site):
    """Lines 1804-1825: _job_proactive_offer_expiry marks old sent offers as expired."""
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

    from app.scheduler import _job_proactive_offer_expiry

    asyncio.run(_job_proactive_offer_expiry())

    scheduler_db.refresh(old_offer)
    assert old_offer.status == "expired"


def test_proactive_offer_expiry_no_expired(scheduler_db):
    """Lines 1804-1825: no offers to expire — no commit needed."""
    from app.scheduler import _job_proactive_offer_expiry

    asyncio.run(_job_proactive_offer_expiry())


def test_proactive_offer_expiry_error(scheduler_db):
    """Lines 1821-1823: DB error rolls back."""
    with patch.object(scheduler_db, "query", side_effect=Exception("DB error")):
        from app.scheduler import _job_proactive_offer_expiry

        asyncio.run(_job_proactive_offer_expiry())


# ── Flag Stale Offers (lines 1838-1860) ──────────────────────────────────


def test_flag_stale_offers_flags_old(scheduler_db, test_user, test_requisition):
    """Lines 1838-1860: _job_flag_stale_offers sets is_stale on old active offers."""
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

    from app.scheduler import _job_flag_stale_offers

    asyncio.run(_job_flag_stale_offers())

    scheduler_db.refresh(old_offer)
    assert old_offer.is_stale is True


def test_flag_stale_offers_no_matches(scheduler_db):
    """Lines 1838-1860: no stale offers — no commit."""
    from app.scheduler import _job_flag_stale_offers

    asyncio.run(_job_flag_stale_offers())


def test_flag_stale_offers_error(scheduler_db):
    """Lines 1856-1858: DB error rolls back."""
    with patch.object(scheduler_db, "query", side_effect=Exception("DB error")):
        from app.scheduler import _job_flag_stale_offers

        asyncio.run(_job_flag_stale_offers())


# ── Auto Attribution (lines 1913-1929) ───────────────────────────────────


def test_auto_attribute_activities_success(scheduler_db):
    """Lines 1913-1929: _job_auto_attribute_activities happy path with matches."""
    mock_attribution = MagicMock(
        return_value={
            "rule_matched": 5,
            "ai_matched": 3,
            "auto_dismissed": 1,
        }
    )
    with patch("app.services.auto_attribution_service.run_auto_attribution", mock_attribution):
        from app.scheduler import _job_auto_attribute_activities

        asyncio.run(_job_auto_attribute_activities())
    mock_attribution.assert_called_once()


def test_auto_attribute_activities_no_matches(scheduler_db):
    """Lines 1913-1929: no matches — no logging."""
    mock_attribution = MagicMock(
        return_value={
            "rule_matched": 0,
            "ai_matched": 0,
            "auto_dismissed": 0,
        }
    )
    with patch("app.services.auto_attribution_service.run_auto_attribution", mock_attribution):
        from app.scheduler import _job_auto_attribute_activities

        asyncio.run(_job_auto_attribute_activities())


def test_auto_attribute_activities_error(scheduler_db):
    """Lines 1925-1927: exception rolls back."""
    mock_attribution = MagicMock(side_effect=Exception("Attribution failed"))
    with patch("app.services.auto_attribution_service.run_auto_attribution", mock_attribution):
        from app.scheduler import _job_auto_attribute_activities

        asyncio.run(_job_auto_attribute_activities())


# ── Auto Dedup (lines 1935-1951) ─────────────────────────────────────────


def test_auto_dedup_success(scheduler_db):
    """Lines 1935-1951: _job_auto_dedup happy path with merges."""
    mock_dedup = MagicMock(
        return_value={
            "vendors_merged": 2,
            "companies_merged": 1,
        }
    )
    with patch("app.services.auto_dedup_service.run_auto_dedup", mock_dedup):
        from app.scheduler import _job_auto_dedup

        asyncio.run(_job_auto_dedup())
    mock_dedup.assert_called_once()


def test_auto_dedup_no_merges(scheduler_db):
    """Lines 1935-1951: no merges — no logging."""
    mock_dedup = MagicMock(
        return_value={
            "vendors_merged": 0,
            "companies_merged": 0,
        }
    )
    with patch("app.services.auto_dedup_service.run_auto_dedup", mock_dedup):
        from app.scheduler import _job_auto_dedup

        asyncio.run(_job_auto_dedup())


def test_auto_dedup_error(scheduler_db):
    """Lines 1947-1949: exception rolls back."""
    mock_dedup = MagicMock(side_effect=Exception("Dedup failed"))
    with patch("app.services.auto_dedup_service.run_auto_dedup", mock_dedup):
        from app.scheduler import _job_auto_dedup

        asyncio.run(_job_auto_dedup())


# ── Prospecting Module Jobs (lines 1754-1790) ────────────────────────────


def test_pool_health_report(scheduler_db):
    """Lines 1754-1755: _job_pool_health_report delegates to prospect_scheduler."""
    mock_fn = AsyncMock()
    with patch("app.services.prospect_scheduler.job_pool_health_report", mock_fn):
        from app.scheduler import _job_pool_health_report

        asyncio.run(_job_pool_health_report())
    mock_fn.assert_called_once()


def test_discover_prospects(scheduler_db):
    """Lines 1761-1762: _job_discover_prospects delegates to prospect_scheduler."""
    mock_fn = AsyncMock()
    with patch("app.services.prospect_scheduler.job_discover_prospects", mock_fn):
        from app.scheduler import _job_discover_prospects

        asyncio.run(_job_discover_prospects())
    mock_fn.assert_called_once()


def test_enrich_pool(scheduler_db):
    """Lines 1768-1769: _job_enrich_pool delegates to prospect_scheduler."""
    mock_fn = AsyncMock()
    with patch("app.services.prospect_scheduler.job_enrich_pool", mock_fn):
        from app.scheduler import _job_enrich_pool

        asyncio.run(_job_enrich_pool())
    mock_fn.assert_called_once()


def test_find_contacts(scheduler_db):
    """Lines 1775-1776: _job_find_contacts delegates to prospect_scheduler."""
    mock_fn = AsyncMock()
    with patch("app.services.prospect_scheduler.job_find_contacts", mock_fn):
        from app.scheduler import _job_find_contacts

        asyncio.run(_job_find_contacts())
    mock_fn.assert_called_once()


def test_refresh_scores(scheduler_db):
    """Lines 1782-1783: _job_refresh_scores delegates to prospect_scheduler."""
    mock_fn = AsyncMock()
    with patch("app.services.prospect_scheduler.job_refresh_scores", mock_fn):
        from app.scheduler import _job_refresh_scores

        asyncio.run(_job_refresh_scores())
    mock_fn.assert_called_once()


def test_expire_and_resurface(scheduler_db):
    """Lines 1789-1790: _job_expire_and_resurface delegates to prospect_scheduler."""
    mock_fn = AsyncMock()
    with patch("app.services.prospect_scheduler.job_expire_and_resurface", mock_fn):
        from app.scheduler import _job_expire_and_resurface

        asyncio.run(_job_expire_and_resurface())
    mock_fn.assert_called_once()


# ── Integrity Check (lines 1869-1886) ────────────────────────────────────


def test_integrity_check_success(scheduler_db):
    """Lines 1869-1886: _job_integrity_check runs integrity service."""
    mock_report = {
        "status": "healthy",
        "material_cards_total": 100,
        "healed": {"requirements": 0, "sightings": 0, "offers": 0},
    }
    mock_check = MagicMock(return_value=mock_report)
    with patch("app.services.integrity_service.run_integrity_check", mock_check):
        from app.scheduler import _job_integrity_check

        asyncio.run(_job_integrity_check())
    mock_check.assert_called_once()


def test_integrity_check_error(scheduler_db):
    """Lines 1883-1884: exception is caught."""
    mock_check = MagicMock(side_effect=Exception("Integrity failed"))
    with patch("app.services.integrity_service.run_integrity_check", mock_check):
        from app.scheduler import _job_integrity_check

        asyncio.run(_job_integrity_check())


# ── Material Enrichment (lines 1891-1907) ────────────────────────────────


def test_material_enrichment_success(scheduler_db):
    """Lines 1891-1907: _job_material_enrichment enriches pending cards."""
    mock_enrich = AsyncMock(return_value={"enriched": 5, "errors": 1, "pending": 10})
    with patch("app.services.material_enrichment_service.enrich_pending_cards", mock_enrich):
        from app.scheduler import _job_material_enrichment

        asyncio.run(_job_material_enrichment())
    mock_enrich.assert_called_once()


def test_material_enrichment_error(scheduler_db):
    """Lines 1904-1905: exception is caught."""
    mock_enrich = AsyncMock(side_effect=Exception("Enrichment failed"))
    with patch("app.services.material_enrichment_service.enrich_pending_cards", mock_enrich):
        from app.scheduler import _job_material_enrichment

        asyncio.run(_job_material_enrichment())


# ── Monthly Enrichment Refresh (lines 885-918) ──────────────────────────


def test_monthly_enrichment_refresh_success(scheduler_db):
    """Lines 885-918: _job_monthly_enrichment_refresh happy path."""

    mock_backfill = AsyncMock(return_value=42)
    mock_flush = MagicMock(return_value=10)

    with (
        patch("app.services.deep_enrichment_service.run_backfill_job", mock_backfill),
        patch("app.cache.intel_cache.flush_enrichment_cache", mock_flush),
    ):
        from app.scheduler import _job_monthly_enrichment_refresh

        asyncio.run(_job_monthly_enrichment_refresh())
    mock_backfill.assert_called_once()


def test_monthly_enrichment_refresh_already_running(scheduler_db):
    """Lines 892-897: skip when a job is already running."""
    from app.models import EnrichmentJob

    running_job = EnrichmentJob(
        job_type="backfill",
        status="running",
    )
    scheduler_db.add(running_job)
    scheduler_db.commit()

    from app.scheduler import _job_monthly_enrichment_refresh

    asyncio.run(_job_monthly_enrichment_refresh())


def test_monthly_enrichment_refresh_error(scheduler_db):
    """Lines 915-916: exception is caught."""
    mock_flush = MagicMock(side_effect=Exception("Cache flush error"))
    # No running job, but flush_enrichment_cache fails
    with patch("app.cache.intel_cache.flush_enrichment_cache", mock_flush):
        from app.scheduler import _job_monthly_enrichment_refresh

        asyncio.run(_job_monthly_enrichment_refresh())


# ── Email Health Update: remaining paths (lines 1017, 1023-1025) ─────────


def test_email_health_update_success(scheduler_db):
    """Line 1017: happy path logs result."""
    mock_health = MagicMock(return_value={"updated": 15})
    with patch("app.services.response_analytics.batch_update_email_health", mock_health):
        from app.scheduler import _job_email_health_update

        asyncio.run(_job_email_health_update())
    mock_health.assert_called_once()


def test_email_health_update_generic_error(scheduler_db):
    """Lines 1023-1025: generic exception (not timeout) rolls back."""
    mock_health = MagicMock(side_effect=RuntimeError("DB error"))
    with patch("app.services.response_analytics.batch_update_email_health", mock_health):
        from app.scheduler import _job_email_health_update

        asyncio.run(_job_email_health_update())


# ── Calendar Scan: no-token and success paths (lines 1060-1061, 1066) ────


def test_calendar_scan_no_token(scheduler_db, test_user):
    """Lines 1060-1061: get_valid_token returns None for a user."""
    test_user.access_token = "at_cal"
    test_user.refresh_token = "rt_cal"
    test_user.m365_connected = True
    scheduler_db.commit()

    mock_scan_db = MagicMock()
    mock_user_obj = MagicMock()
    mock_user_obj.id = test_user.id
    mock_user_obj.email = test_user.email
    mock_scan_db.get.return_value = mock_user_obj

    call_count = 0

    def _session_factory():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return scheduler_db
        return mock_scan_db

    with (
        patch("app.database.SessionLocal", side_effect=_session_factory),
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value=None),
    ):
        from app.scheduler import _job_calendar_scan

        asyncio.run(_job_calendar_scan())


def test_calendar_scan_success(scheduler_db, test_user):
    """Line 1066: successful calendar scan logs events."""
    test_user.access_token = "at_cal"
    test_user.refresh_token = "rt_cal"
    test_user.m365_connected = True
    scheduler_db.commit()

    mock_scan_db = MagicMock()
    mock_user_obj = MagicMock()
    mock_user_obj.id = test_user.id
    mock_user_obj.email = test_user.email
    mock_scan_db.get.return_value = mock_user_obj

    call_count = 0

    def _session_factory():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return scheduler_db
        return mock_scan_db

    with (
        patch("app.database.SessionLocal", side_effect=_session_factory),
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="valid-token"),
        patch(
            "app.services.calendar_intelligence.scan_calendar_events",
            new_callable=AsyncMock,
            return_value={"events_found": 5},
        ),
    ):
        from app.scheduler import _job_calendar_scan

        asyncio.run(_job_calendar_scan())


# ── Health Check Jobs (lines 1986-1994) ───────────────────────────────


def test_job_health_ping():
    """_job_health_ping delegates to run_health_checks('ping') (lines 1986-1987)."""
    with patch(
        "app.services.health_monitor.run_health_checks",
        new_callable=AsyncMock,
        return_value={"total": 3, "passed": 3, "failed": 0},
    ) as mock_check:
        from app.scheduler import _job_health_ping

        asyncio.run(_job_health_ping())
        mock_check.assert_awaited_once_with("ping")


def test_job_health_deep():
    """_job_health_deep delegates to run_health_checks('deep') (lines 1993-1994)."""
    with patch(
        "app.services.health_monitor.run_health_checks",
        new_callable=AsyncMock,
        return_value={"total": 3, "passed": 2, "failed": 1},
    ) as mock_check:
        from app.scheduler import _job_health_deep

        asyncio.run(_job_health_deep())
        mock_check.assert_awaited_once_with("deep")


# ── Usage Log Cleanup (lines 2000-2014) ───────────────────────────────


def test_job_cleanup_usage_log_deletes_old(scheduler_db):
    """_job_cleanup_usage_log deletes entries older than 90 days (lines 2000-2014)."""
    from app.models.config import ApiSource, ApiUsageLog

    src = ApiSource(
        name="cleanup_src",
        display_name="Cleanup Src",
        category="api",
        source_type="test",
        status="live",
        is_active=True,
    )
    scheduler_db.add(src)
    scheduler_db.commit()

    old = ApiUsageLog(
        source_id=src.id,
        timestamp=datetime.now(timezone.utc) - timedelta(days=120),
        endpoint="/test",
        status_code=200,
        response_ms=100,
        success=True,
        check_type="ping",
    )
    recent = ApiUsageLog(
        source_id=src.id,
        timestamp=datetime.now(timezone.utc) - timedelta(days=10),
        endpoint="/test",
        status_code=200,
        response_ms=50,
        success=True,
        check_type="ping",
    )
    scheduler_db.add_all([old, recent])
    scheduler_db.commit()

    from app.scheduler import _job_cleanup_usage_log

    asyncio.run(_job_cleanup_usage_log())

    remaining = scheduler_db.query(ApiUsageLog).all()
    assert len(remaining) == 1
    assert remaining[0].id == recent.id


def test_job_cleanup_usage_log_handles_error():
    """_job_cleanup_usage_log catches and rolls back on error (lines 2010-2012)."""
    mock_db = MagicMock()
    mock_db.query.side_effect = Exception("DB error")

    with patch("app.database.SessionLocal", return_value=mock_db):
        from app.scheduler import _job_cleanup_usage_log

        asyncio.run(_job_cleanup_usage_log())

    mock_db.rollback.assert_called_once()
    mock_db.close.assert_called_once()


# ── Monthly Usage Reset (lines 2020-2032) ─────────────────────────────


def test_job_reset_monthly_usage(scheduler_db):
    """_job_reset_monthly_usage resets calls_this_month to 0 (lines 2020-2032)."""
    from app.models.config import ApiSource

    src1 = ApiSource(
        name="monthly_a",
        display_name="A",
        category="api",
        source_type="test",
        status="live",
        is_active=True,
        calls_this_month=150,
    )
    src2 = ApiSource(
        name="monthly_b",
        display_name="B",
        category="api",
        source_type="test",
        status="live",
        is_active=True,
        calls_this_month=300,
    )
    scheduler_db.add_all([src1, src2])
    scheduler_db.commit()

    from app.scheduler import _job_reset_monthly_usage

    asyncio.run(_job_reset_monthly_usage())

    scheduler_db.refresh(src1)
    scheduler_db.refresh(src2)
    assert src1.calls_this_month == 0
    assert src2.calls_this_month == 0


def test_job_reset_monthly_usage_handles_error():
    """_job_reset_monthly_usage catches and rolls back on error (lines 2028-2030)."""
    mock_db = MagicMock()
    mock_db.query.return_value.update.side_effect = Exception("DB error")

    with patch("app.database.SessionLocal", return_value=mock_db):
        from app.scheduler import _job_reset_monthly_usage

        asyncio.run(_job_reset_monthly_usage())


# ── Phase 7: Scheduler Optimization ──────────────────────────────────


def test_nexar_validate_job_registered():
    """Nexar validate job is registered."""
    with patch("app.config.settings", _mock_settings()):
        configure_scheduler()

    job = scheduler.get_job("nexar_validate")
    assert job is not None


def test_connector_enrichment_2hour_interval():
    """Connector enrichment runs every 2 hours (was 4)."""
    with patch("app.config.settings", _mock_settings()):
        configure_scheduler()

    job = scheduler.get_job("connector_enrichment")
    assert job is not None
    # Check the trigger interval is 2 hours
    trigger = job.trigger
    assert trigger.interval.total_seconds() == 7200  # 2 hours


def test_nexar_validate_job_6hour_interval():
    """Nexar validate job runs every 6 hours."""
    with patch("app.config.settings", _mock_settings()):
        configure_scheduler()

    job = scheduler.get_job("nexar_validate")
    assert job is not None
    trigger = job.trigger
    assert trigger.interval.total_seconds() == 21600  # 6 hours


def test_nexar_validate_job_runs():
    """_job_nexar_validate calls nexar_bulk_validate and logs result."""
    mock_db = MagicMock()

    with (
        patch("app.database.SessionLocal", return_value=mock_db),
        patch(
            "app.services.enrichment.nexar_bulk_validate",
            new_callable=AsyncMock,
            return_value={"validated": 10, "upgraded": 3},
        ) as mock_validate,
    ):
        from app.scheduler import _job_nexar_validate

        asyncio.run(_job_nexar_validate())

    mock_validate.assert_called_once_with(mock_db, limit=2000)
    mock_db.close.assert_called_once()


def test_nexar_validate_job_handles_error():
    """_job_nexar_validate rolls back on error and re-raises."""
    mock_db = MagicMock()

    with (
        patch("app.database.SessionLocal", return_value=mock_db),
        patch(
            "app.services.enrichment.nexar_bulk_validate",
            new_callable=AsyncMock,
            side_effect=Exception("Nexar API error"),
        ),
    ):
        from app.scheduler import _job_nexar_validate

        with pytest.raises(Exception, match="Nexar API error"):
            asyncio.run(_job_nexar_validate())

    mock_db.rollback.assert_called_once()
    mock_db.close.assert_called_once()


def test_connector_enrichment_boost_cascade():
    """_job_connector_enrichment runs boost cascade after enrichment."""
    mock_db = MagicMock()
    mock_db.query.return_value.join.return_value.join.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = []

    with (
        patch("app.database.SessionLocal", return_value=mock_db),
        patch(
            "app.services.enrichment.cross_validate_batch",
            new_callable=AsyncMock,
            return_value={"total": 0},
        ),
        patch(
            "app.services.enrichment.boost_confidence_internal",
            return_value={"total_boosted": 5},
        ) as mock_boost,
        patch(
            "app.services.tagging_backfill.backfill_manufacturer_from_sightings",
            return_value={"total_tagged": 3},
        ) as mock_sighting,
    ):
        from app.scheduler import _job_connector_enrichment

        asyncio.run(_job_connector_enrichment())

    mock_boost.assert_called_once_with(mock_db)
    mock_sighting.assert_called_once_with(mock_db)
    mock_db.close.assert_called_once()
