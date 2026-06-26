"""test_jobs_core.py — Tests for core background jobs (auto-archive, token refresh,
batch results, inbox scan, webhooks)

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


@pytest.mark.parametrize(
    ("name", "last_searched_at", "expected_archived"),
    [
        pytest.param(
            "OLD-001",
            datetime.now(timezone.utc) - timedelta(days=45),
            True,
            id="stale_gets_archived",
        ),
        pytest.param(
            "RECENT-001",
            datetime.now(timezone.utc) - timedelta(days=5),
            False,
            id="recent_skipped",
        ),
        pytest.param(
            "UNSEARCHED-001",
            None,
            False,
            id="never_searched_skipped",
        ),
    ],
)
def test_auto_archive_by_last_searched(scheduler_db, test_user, name, last_searched_at, expected_archived):
    """Open requisitions get is_archived=True only when last searched >30 days ago."""
    req = Requisition(
        name=name,
        status="open",
        created_by=test_user.id,
        last_searched_at=last_searched_at,
    )
    scheduler_db.add(req)
    scheduler_db.commit()

    from app.jobs.core_jobs import _job_auto_archive

    asyncio.run(_job_auto_archive())

    scheduler_db.refresh(req)
    assert req.is_archived is expected_archived
    assert req.status == "open"  # archive is orthogonal — status is untouched


def test_auto_archive_only_archives_active_status(scheduler_db, test_user):
    """Only OPEN requisitions are archived; other statuses are untouched."""
    already_archived = Requisition(
        name="ALREADY-ARCHIVED",
        status="open",
        is_archived=True,
        created_by=test_user.id,
        last_searched_at=datetime.now(timezone.utc) - timedelta(days=60),
    )
    active_stale = Requisition(
        name="ACTIVE-STALE",
        status="open",
        created_by=test_user.id,
        last_searched_at=datetime.now(timezone.utc) - timedelta(days=60),
    )
    draft_stale = Requisition(
        name="DRAFT-STALE",
        status="draft",
        created_by=test_user.id,
        last_searched_at=datetime.now(timezone.utc) - timedelta(days=60),
    )
    scheduler_db.add_all([already_archived, active_stale, draft_stale])
    scheduler_db.commit()

    from app.jobs.core_jobs import _job_auto_archive

    asyncio.run(_job_auto_archive())

    scheduler_db.refresh(already_archived)
    scheduler_db.refresh(active_stale)
    scheduler_db.refresh(draft_stale)
    assert already_archived.is_archived is True  # unchanged
    assert active_stale.is_archived is True  # stale open → archived
    assert draft_stale.is_archived is False  # non-open status untouched


def test_auto_archive_error_handling(scheduler_db):
    """Auto-archive logs and re-raises DB errors for _traced_job/Sentry capture."""
    with patch.object(scheduler_db, "query", side_effect=Exception("DB locked")):
        from app.jobs.core_jobs import _job_auto_archive

        with pytest.raises(Exception, match="DB locked"):
            asyncio.run(_job_auto_archive())


# ── _job_token_refresh() ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("refresh_token", "access_token", "token_expires_at", "should_refresh"),
    [
        pytest.param(
            "rt_test_123",
            "old_token",
            datetime.now(timezone.utc) - timedelta(hours=1),
            True,
            id="expired_gets_refreshed",
        ),
        pytest.param(
            "rt_test_123",
            "still_valid",
            datetime.now(timezone.utc) + timedelta(hours=1),
            False,
            id="valid_skipped",
        ),
        pytest.param(
            "rt_test_789",
            None,
            None,
            True,
            id="no_access_token_gets_refreshed",
        ),
    ],
)
def test_token_refresh_by_token_state(
    scheduler_db, test_user, refresh_token, access_token, token_expires_at, should_refresh
):
    """Users are refreshed only when their access token is expired or missing."""
    test_user.refresh_token = refresh_token
    test_user.access_token = access_token
    test_user.token_expires_at = token_expires_at
    scheduler_db.commit()

    with patch("app.utils.token_manager.refresh_user_token", new_callable=AsyncMock) as mock_refresh:
        mock_refresh.return_value = "new_token"
        from app.jobs.core_jobs import _job_token_refresh

        asyncio.run(_job_token_refresh())
        if should_refresh:
            mock_refresh.assert_called_once()
        else:
            mock_refresh.assert_not_called()


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
    """Outer exception in _job_token_refresh is re-raised for _traced_job/Sentry."""
    with patch.object(scheduler_db, "query", side_effect=Exception("DB crash")):
        from app.jobs.core_jobs import _job_token_refresh

        with pytest.raises(Exception, match="DB crash"):
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


def _non_200_post():
    mock_response = MagicMock()
    mock_response.status_code = 400
    mock_response.text = "invalid_grant: The refresh token has expired"
    return AsyncMock(return_value=mock_response)


@pytest.mark.parametrize(
    "post_mock_factory",
    [
        pytest.param(_non_200_post, id="non_200_response"),
        pytest.param(
            lambda: AsyncMock(side_effect=Exception("Connection refused")),
            id="network_error",
        ),
    ],
)
def test_refresh_access_token_returns_none(post_mock_factory):
    """Azure AD non-200 responses and network errors both return None."""
    with patch("app.utils.token_manager.http") as mock_http:
        mock_http.post = post_mock_factory()
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
    """Batch results job re-raises asyncio.TimeoutError for _traced_job/Sentry."""
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

        with pytest.raises(asyncio.TimeoutError):
            asyncio.run(_job_batch_results())


def test_batch_results_handles_error(scheduler_db):
    """Batch results job re-raises general errors for _traced_job/Sentry."""
    with patch(
        "app.email_service.process_batch_results",
        new_callable=AsyncMock,
        side_effect=Exception("AI service down"),
    ):
        from app.jobs.core_jobs import _job_batch_results

        with pytest.raises(Exception, match="AI service down"):
            asyncio.run(_job_batch_results())


# ── _job_inbox_scan() ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("access_token", "m365_connected", "last_inbox_scan", "should_scan"),
    [
        pytest.param(
            "at_inbox",
            True,
            datetime.now(timezone.utc) - timedelta(hours=2),
            True,
            id="connected_stale_scanned",
        ),
        pytest.param(
            "at_inbox",
            False,
            None,
            False,
            id="disconnected_skipped",
        ),
        pytest.param(
            None,
            True,
            None,
            False,
            id="no_access_token_skipped",
        ),
        pytest.param(
            "at_inbox",
            True,
            None,
            True,
            id="never_scanned_scanned",
        ),
        pytest.param(
            "at_inbox",
            True,
            datetime.now(timezone.utc) - timedelta(minutes=5),
            False,
            id="recently_scanned_skipped",
        ),
    ],
)
def test_inbox_scan_eligibility(scheduler_db, test_user, access_token, m365_connected, last_inbox_scan, should_scan):
    """A user is scanned only when connected, has an access token, and is past the
    interval."""
    test_user.refresh_token = "rt_inbox"
    test_user.access_token = access_token
    test_user.m365_connected = m365_connected
    test_user.last_inbox_scan = last_inbox_scan
    scheduler_db.commit()

    with (
        patch("app.jobs.email_jobs._scan_user_inbox", new_callable=AsyncMock) as mock_scan,
        patch("app.config.settings") as mock_settings,
    ):
        mock_settings.inbox_scan_interval_min = 30
        from app.jobs.core_jobs import _job_inbox_scan

        asyncio.run(_job_inbox_scan())
        if should_scan:
            mock_scan.assert_called_once()
        else:
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
    """Error during user-gathering phase is re-raised for _traced_job/Sentry."""
    with (
        patch.object(scheduler_db, "query", side_effect=Exception("DB error")),
        patch("app.config.settings") as mock_settings,
    ):
        mock_settings.inbox_scan_interval_min = 30
        from app.jobs.core_jobs import _job_inbox_scan

        with pytest.raises(Exception, match="DB error"):
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
        from sqlalchemy.exc import OperationalError

        commit_count[0] += 1
        raise OperationalError("commit", {}, Exception("commit during timeout recovery failed"))

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
    """Webhook job re-raises errors for _traced_job/Sentry capture."""
    with patch(
        "app.services.webhook_service.renew_expiring_subscriptions",
        new_callable=AsyncMock,
        side_effect=Exception("Graph API error"),
    ):
        from app.jobs.core_jobs import _job_webhook_subscriptions

        with pytest.raises(Exception, match="Graph API error"):
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
