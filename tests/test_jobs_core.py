"""test_jobs_core.py — Tests for core background jobs (auto-archive, token refresh, batch results, inbox scan, webhooks)

Tests cover: _job_auto_archive, _job_token_refresh, _job_batch_results, _job_inbox_scan,
_job_webhook_subscriptions, plus get_valid_token, refresh_user_token, and _refresh_access_token
helper functions.

All jobs use SessionLocal() internally, so we patch app.database.SessionLocal
to return the test DB session with close() disabled.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models import Requisition
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

    from app.jobs.core_jobs import _job_auto_archive

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

    from app.jobs.core_jobs import _job_auto_archive

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

    from app.jobs.core_jobs import _job_auto_archive

    asyncio.run(_job_auto_archive())

    scheduler_db.refresh(unsearched)
    assert unsearched.status == "active"


def test_auto_archive_only_archives_active_status(scheduler_db, test_user):
    """Only requisitions with status='active' are archived."""
    already_archived = Requisition(
        name="ALREADY-ARCHIVED",
        status="archived",
        created_by=test_user.id,
        last_searched_at=datetime.now(timezone.utc) - timedelta(days=60),
    )
    open_req = Requisition(
        name="OPEN-001",
        status="open",
        created_by=test_user.id,
        last_searched_at=datetime.now(timezone.utc) - timedelta(days=60),
    )
    scheduler_db.add_all([already_archived, open_req])
    scheduler_db.commit()

    from app.jobs.core_jobs import _job_auto_archive

    asyncio.run(_job_auto_archive())

    scheduler_db.refresh(already_archived)
    scheduler_db.refresh(open_req)
    assert already_archived.status == "archived"  # unchanged
    assert open_req.status == "open"  # only "active" status is targeted


def test_auto_archive_error_handling(scheduler_db):
    """Auto-archive handles DB errors gracefully when query fails."""
    with patch.object(scheduler_db, "query", side_effect=Exception("DB locked")):
        from app.jobs.core_jobs import _job_auto_archive

        # Should not raise — error is caught internally
        asyncio.run(_job_auto_archive())


# ── _job_token_refresh() ──────────────────────────────────────────────


def test_token_refresh_refreshes_expired(scheduler_db, test_user):
    """Users with expired tokens get refreshed."""
    test_user.refresh_token = "rt_test_123"
    test_user.token_expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
    test_user.access_token = "old_token"
    scheduler_db.commit()

    with patch("app.utils.token_manager.refresh_user_token", new_callable=AsyncMock) as mock_refresh:
        mock_refresh.return_value = "new_token"
        from app.jobs.core_jobs import _job_token_refresh

        asyncio.run(_job_token_refresh())
        mock_refresh.assert_called_once()


def test_token_refresh_skips_valid(scheduler_db, test_user):
    """Users with valid tokens are not refreshed."""
    test_user.refresh_token = "rt_test_123"
    test_user.token_expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    test_user.access_token = "still_valid"
    scheduler_db.commit()

    with patch("app.utils.token_manager.refresh_user_token", new_callable=AsyncMock) as mock_refresh:
        from app.jobs.core_jobs import _job_token_refresh

        asyncio.run(_job_token_refresh())
        mock_refresh.assert_not_called()


def test_token_refresh_refreshes_user_without_access_token(scheduler_db, test_user):
    """Users with a refresh token but no access token get refreshed."""
    test_user.refresh_token = "rt_test_789"
    test_user.access_token = None
    test_user.token_expires_at = None
    scheduler_db.commit()

    with patch("app.utils.token_manager.refresh_user_token", new_callable=AsyncMock) as mock_refresh:
        mock_refresh.return_value = "new_token"
        from app.jobs.core_jobs import _job_token_refresh

        asyncio.run(_job_token_refresh())
        mock_refresh.assert_called_once()


def test_token_refresh_handles_error_per_user(scheduler_db, test_user):
    """Errors during per-user refresh are caught and do not crash the job."""
    test_user.refresh_token = "rt_test_err"
    test_user.token_expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
    test_user.access_token = "old"
    scheduler_db.commit()

    with patch("app.utils.token_manager.refresh_user_token", new_callable=AsyncMock) as mock_refresh:
        mock_refresh.side_effect = Exception("Unexpected error")
        from app.jobs.core_jobs import _job_token_refresh

        # Should not raise
        asyncio.run(_job_token_refresh())


def test_token_refresh_outer_exception(scheduler_db):
    """Outer exception in _job_token_refresh is caught."""
    with patch.object(scheduler_db, "query", side_effect=Exception("DB crash")):
        from app.jobs.core_jobs import _job_token_refresh

        # Should not raise
        asyncio.run(_job_token_refresh())


def test_token_refresh_redis_lock_acquired(scheduler_db, test_user):
    """Token refresh acquires Redis lock and refreshes user."""
    test_user.refresh_token = "rt_lock"
    test_user.token_expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
    test_user.access_token = "old_token"
    scheduler_db.commit()

    mock_redis = MagicMock()
    mock_redis.set.return_value = True  # Lock acquired

    with (
        patch("app.utils.token_manager.refresh_user_token", new_callable=AsyncMock) as mock_refresh,
        patch("app.cache.intel_cache._get_redis", return_value=mock_redis),
    ):
        from app.jobs.core_jobs import _job_token_refresh

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
        patch("app.utils.token_manager.refresh_user_token", new_callable=AsyncMock) as mock_refresh,
        patch("app.cache.intel_cache._get_redis", return_value=mock_redis),
    ):
        from app.jobs.core_jobs import _job_token_refresh

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
        patch("app.utils.token_manager.refresh_user_token", new_callable=AsyncMock) as mock_refresh,
        patch("app.cache.intel_cache._get_redis", return_value=mock_redis),
    ):
        from app.jobs.core_jobs import _job_token_refresh

        # Should not raise — exception in finally is swallowed
        asyncio.run(_job_token_refresh())
        mock_refresh.assert_called_once()


# ── get_valid_token() ──────────────────────────────────────────────────


def test_get_valid_token_returns_current_when_valid(db_session, test_user):
    """Returns existing access_token when it has not expired."""
    test_user.access_token = "valid_token_abc"
    test_user.token_expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    db_session.commit()

    from app.utils.token_manager import get_valid_token

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
        from app.utils.token_manager import get_valid_token

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
        from app.utils.token_manager import get_valid_token

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
        from app.utils.token_manager import get_valid_token

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
        from app.utils.token_manager import get_valid_token

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
        from app.utils.token_manager import refresh_user_token

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

    from app.utils.token_manager import refresh_user_token

    result = asyncio.run(refresh_user_token(test_user, db_session))
    assert result is None


def test_refresh_user_token_failure_disconnects_user(db_session, test_user):
    """Failed refresh sets m365_connected to False."""
    test_user.refresh_token = "rt_invalid"
    test_user.m365_connected = True
    db_session.commit()

    with patch("app.utils.token_manager._refresh_access_token", new_callable=AsyncMock) as mock_aat:
        mock_aat.return_value = None
        from app.utils.token_manager import refresh_user_token

        result = asyncio.run(refresh_user_token(test_user, db_session))
        assert result is None
        assert test_user.m365_connected is False


def test_refresh_user_token_keeps_old_refresh_when_none_returned(db_session, test_user):
    """Keeps existing refresh_token when Azure returns no new one."""
    test_user.refresh_token = "rt_keep_me"
    db_session.commit()

    with patch("app.utils.token_manager._refresh_access_token", new_callable=AsyncMock) as mock_aat:
        mock_aat.return_value = ("new_at", None)  # no new refresh token
        from app.utils.token_manager import refresh_user_token

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
        from app.utils.token_manager import _refresh_access_token

        result = asyncio.run(_refresh_access_token("rt_old", "client_id", "client_secret", "tenant_id"))
        assert result == ("at_new", "rt_new")


def test_refresh_access_token_failure_returns_none():
    """Non-200 response from Azure AD returns None."""
    mock_response = MagicMock()
    mock_response.status_code = 400
    mock_response.text = "invalid_grant: The refresh token has expired"

    with patch("app.utils.token_manager.http") as mock_http:
        mock_http.post = AsyncMock(return_value=mock_response)
        from app.utils.token_manager import _refresh_access_token

        result = asyncio.run(_refresh_access_token("rt_bad", "cid", "cs", "tid"))
        assert result is None


def test_refresh_access_token_exception_returns_none():
    """Network error during refresh returns None."""
    with patch("app.utils.token_manager.http") as mock_http:
        mock_http.post = AsyncMock(side_effect=Exception("Connection refused"))
        from app.utils.token_manager import _refresh_access_token

        result = asyncio.run(_refresh_access_token("rt", "cid", "cs", "tid"))
        assert result is None


# ── _job_batch_results() ─────────────────────────────────────────────


def test_batch_results_calls_process(scheduler_db):
    """Batch results job delegates to email_service.process_batch_results."""
    with patch("app.email_service.process_batch_results", new_callable=AsyncMock) as mock_pbr:
        mock_pbr.return_value = 5
        from app.jobs.core_jobs import _job_batch_results

        asyncio.run(_job_batch_results())
        mock_pbr.assert_called_once_with(scheduler_db)


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
        from app.jobs.core_jobs import _job_batch_results

        # Should not raise
        asyncio.run(_job_batch_results())


def test_batch_results_handles_error(scheduler_db):
    """Batch results job handles general errors gracefully."""
    with patch(
        "app.email_service.process_batch_results",
        new_callable=AsyncMock,
        side_effect=Exception("AI service down"),
    ):
        from app.jobs.core_jobs import _job_batch_results

        # Should not raise
        asyncio.run(_job_batch_results())


# ── _job_inbox_scan() ──────────────────────────────────────────────────


def test_inbox_scan_scans_connected_user(scheduler_db, test_user):
    """Connected users with stale last_inbox_scan are scanned."""
    test_user.refresh_token = "rt_inbox"
    test_user.access_token = "at_inbox"
    test_user.m365_connected = True
    test_user.last_inbox_scan = datetime.now(timezone.utc) - timedelta(hours=2)
    scheduler_db.commit()

    with (
        patch("app.jobs.email_jobs._scan_user_inbox", new_callable=AsyncMock) as mock_scan,
        patch("app.config.settings") as mock_settings,
    ):
        mock_settings.inbox_scan_interval_min = 30
        from app.jobs.core_jobs import _job_inbox_scan

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
        patch("app.jobs.email_jobs._scan_user_inbox", new_callable=AsyncMock) as mock_scan,
        patch("app.config.settings") as mock_settings,
    ):
        mock_settings.inbox_scan_interval_min = 30
        from app.jobs.core_jobs import _job_inbox_scan

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
        patch("app.jobs.email_jobs._scan_user_inbox", new_callable=AsyncMock) as mock_scan,
        patch("app.config.settings") as mock_settings,
    ):
        mock_settings.inbox_scan_interval_min = 30
        from app.jobs.core_jobs import _job_inbox_scan

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
        patch("app.jobs.email_jobs._scan_user_inbox", new_callable=AsyncMock) as mock_scan,
        patch("app.config.settings") as mock_settings,
    ):
        mock_settings.inbox_scan_interval_min = 30
        from app.jobs.core_jobs import _job_inbox_scan

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
        patch("app.jobs.email_jobs._scan_user_inbox", new_callable=AsyncMock) as mock_scan,
        patch("app.config.settings") as mock_settings,
    ):
        mock_settings.inbox_scan_interval_min = 30
        from app.jobs.core_jobs import _job_inbox_scan

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
        patch("app.jobs.email_jobs._scan_user_inbox", new_callable=AsyncMock, side_effect=slow_scan),
        patch("app.config.settings") as mock_settings,
        patch("asyncio.wait_for", new_callable=AsyncMock, side_effect=asyncio.TimeoutError()),
    ):
        mock_settings.inbox_scan_interval_min = 30
        from app.jobs.core_jobs import _job_inbox_scan

        asyncio.run(_job_inbox_scan())
        # User should have an error set
        assert test_user.m365_error_reason == "Inbox scan timed out"


def test_inbox_scan_error_in_user_gathering(scheduler_db):
    """Error during user-gathering phase returns early."""
    with (
        patch.object(scheduler_db, "query", side_effect=Exception("DB error")),
        patch("app.config.settings") as mock_settings,
    ):
        mock_settings.inbox_scan_interval_min = 30
        from app.jobs.core_jobs import _job_inbox_scan

        # Should not raise
        asyncio.run(_job_inbox_scan())


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
        patch("app.jobs.email_jobs._scan_user_inbox", new_callable=AsyncMock) as mock_scan,
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
            from app.jobs.core_jobs import _job_inbox_scan

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
        patch("app.jobs.email_jobs._scan_user_inbox", new_callable=AsyncMock, side_effect=Exception("random error")),
        patch("app.config.settings") as mock_settings,
    ):
        mock_settings.inbox_scan_interval_min = 30
        from app.jobs.core_jobs import _job_inbox_scan

        # Should not raise
        asyncio.run(_job_inbox_scan())


def test_inbox_scan_safe_scan_user_not_found(scheduler_db, test_user):
    """_safe_scan returns early when user is not found in the scan session."""
    test_user.refresh_token = "rt_notfound"
    test_user.access_token = "at_notfound"
    test_user.m365_connected = True
    test_user.last_inbox_scan = None
    scheduler_db.commit()

    user_id = test_user.id

    original_get = scheduler_db.get
    call_count = [0]

    def _get_none_second_time(model, id_):
        call_count[0] += 1
        if call_count[0] >= 1:
            return None
        return original_get(model, id_)

    with (
        patch("app.jobs.email_jobs._scan_user_inbox", new_callable=AsyncMock) as mock_scan,
        patch("app.config.settings") as mock_settings,
    ):
        mock_settings.inbox_scan_interval_min = 30

        scheduler_db.get = _get_none_second_time
        from app.jobs.core_jobs import _job_inbox_scan

        asyncio.run(_job_inbox_scan())
        scheduler_db.get = original_get

        mock_scan.assert_not_called()


def test_inbox_scan_safe_scan_timeout_commit_exception(scheduler_db, test_user):
    """Exception during timeout recovery commit is handled."""
    test_user.refresh_token = "rt_tce"
    test_user.access_token = "at_tce"
    test_user.m365_connected = True
    test_user.last_inbox_scan = None
    scheduler_db.commit()

    original_commit = scheduler_db.commit

    commit_count = [0]

    def _fail_recovery_commit():
        commit_count[0] += 1
        raise Exception("commit during timeout recovery failed")

    with (
        patch("app.jobs.email_jobs._scan_user_inbox", new_callable=AsyncMock) as mock_scan,
        patch("app.config.settings") as mock_settings,
    ):
        mock_settings.inbox_scan_interval_min = 30

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

        scheduler_db.commit = _fail_recovery_commit

        with patch("asyncio.wait_for", side_effect=_mock_wait_for):
            from app.jobs.core_jobs import _job_inbox_scan

            asyncio.run(_job_inbox_scan())

        scheduler_db.commit = original_commit


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
        from app.jobs.core_jobs import _job_webhook_subscriptions

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
        from app.jobs.core_jobs import _job_webhook_subscriptions

        # Should not raise
        asyncio.run(_job_webhook_subscriptions())


# ── _traced_job exception path ────────────────────────────────────────


def test_traced_job_exception_is_reraised():
    """The _traced_job wrapper re-raises exceptions after logging."""
    from app.scheduler import _traced_job

    @_traced_job
    async def boom():
        raise RuntimeError("kaboom")

    with pytest.raises(RuntimeError, match="kaboom"):
        asyncio.run(boom())
