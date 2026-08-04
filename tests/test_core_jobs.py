"""Tests for app/jobs/core_jobs.py — background job registration and execution.

Called by: pytest
Depends on: conftest fixtures, unittest.mock
"""

import asyncio
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models import User

# ═══════════════════════════════════════════════════════════════════════
#  register_core_jobs
# ═══════════════════════════════════════════════════════════════════════


@contextmanager
def _m365_creds(client_id="test-client", client_secret="test-secret", tenant_id="test-tenant"):
    """Patch the real app settings' Azure creds (all three) for m365_configured()."""
    from app.config import settings

    with (
        patch.object(settings, "azure_client_id", client_id),
        patch.object(settings, "azure_client_secret", client_secret),
        patch.object(settings, "azure_tenant_id", tenant_id),
    ):
        yield


class TestRegisterCoreJobs:
    """Tests for register_core_jobs()."""

    def test_registers_base_jobs(self):
        """The three kernel core jobs are registered — nothing else.

        M365-configured is the kernel assumption: token_refresh registration is gated
        on m365_configured(), so the scheduler's kernel-7 total holds only when the
        Azure creds are set (as here).
        """
        from app.jobs.core_jobs import register_core_jobs

        scheduler = MagicMock()
        settings = MagicMock()
        settings.inbox_scan_interval_min = 5

        with _m365_creds():
            register_core_jobs(scheduler, settings)

        job_ids = [call.kwargs["id"] for call in scheduler.add_job.call_args_list]
        assert "token_refresh" in job_ids
        assert "inbox_scan" in job_ids
        assert "batch_results" in job_ids
        # Deleted W1 (spec §3 kernel-only): signature batch pipeline + webhook subs
        assert "batch_parse_signatures" not in job_ids
        assert "poll_signature_batch" not in job_ids
        assert "webhook_subs" not in job_ids

    def test_total_job_count(self):
        """3 kernel core jobs when M365 is configured, 2 when it is not."""
        from app.jobs.core_jobs import register_core_jobs

        settings = MagicMock()
        settings.inbox_scan_interval_min = 10

        scheduler = MagicMock()
        with _m365_creds():
            register_core_jobs(scheduler, settings)
        assert scheduler.add_job.call_count == 3

        scheduler = MagicMock()
        with _m365_creds("", "", ""):
            register_core_jobs(scheduler, settings)
        assert scheduler.add_job.call_count == 2

    def test_keys_off_skips_with_notice(self):
        """W1.15 (48h-gate), enforced at registration (the sibling register_*_jobs
        idiom): no Azure creds -> token_refresh is never registered and ONE honest INFO
        notice is logged; creds set -> registered."""
        from app.jobs.core_jobs import register_core_jobs

        settings = MagicMock()
        settings.inbox_scan_interval_min = 5

        scheduler = MagicMock()
        with _m365_creds("", "", ""), patch("app.jobs.core_jobs.logger") as mock_logger:
            register_core_jobs(scheduler, settings)
        job_ids = [call.kwargs["id"] for call in scheduler.add_job.call_args_list]
        assert "token_refresh" not in job_ids
        notices = [c for c in mock_logger.info.call_args_list if "Token refresh not registered" in c.args[0]]
        assert len(notices) == 1

        scheduler = MagicMock()
        with _m365_creds():
            register_core_jobs(scheduler, settings)
        job_ids = [call.kwargs["id"] for call in scheduler.add_job.call_args_list]
        assert "token_refresh" in job_ids


# ═══════════════════════════════════════════════════════════════════════
#  _job_token_refresh
# ═══════════════════════════════════════════════════════════════════════


class TestJobTokenRefresh:
    """Tests for _job_token_refresh().

    The job body no longer guards on Azure creds — registration is gated instead
    (TestRegisterCoreJobs.test_keys_off_skips_with_notice) — so no creds fixture.
    """

    @pytest.mark.asyncio
    async def test_refreshes_expiring_tokens(self, db_session: Session, test_user: User):
        """Users with tokens expiring within 15 min get refreshed."""
        from app.jobs.core_jobs import _job_token_refresh

        test_user.refresh_token = "rt-test-123"
        test_user.access_token = "at-test-123"
        test_user.token_expires_at = datetime.now(UTC) + timedelta(minutes=5)
        db_session.commit()

        mock_refresh = AsyncMock()
        mock_redis = MagicMock()
        mock_redis.set.return_value = True  # acquired lock

        with (
            patch("app.database.SessionLocal", return_value=db_session),
            patch.object(db_session, "close"),
            patch("app.utils.token_manager.refresh_user_token", mock_refresh),
            patch("app.cache.intel_cache._get_redis", return_value=mock_redis),
        ):
            await _job_token_refresh.__wrapped__()

        mock_refresh.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_skips_users_without_refresh_token(self, db_session: Session, test_user: User):
        """Users without refresh_token are excluded."""
        from app.jobs.core_jobs import _job_token_refresh

        test_user.refresh_token = None
        db_session.commit()

        mock_refresh = AsyncMock()

        with (
            patch("app.database.SessionLocal", return_value=db_session),
            patch.object(db_session, "close"),
            patch("app.utils.token_manager.refresh_user_token", mock_refresh),
        ):
            await _job_token_refresh.__wrapped__()

        mock_refresh.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_users_with_valid_token(self, db_session: Session, test_user: User):
        """Users with tokens expiring >15 min out are skipped."""
        from app.jobs.core_jobs import _job_token_refresh

        test_user.refresh_token = "rt-test"
        test_user.access_token = "at-test"
        test_user.token_expires_at = datetime.now(UTC) + timedelta(hours=1)
        db_session.commit()

        mock_refresh = AsyncMock()

        with (
            patch("app.database.SessionLocal", return_value=db_session),
            patch.object(db_session, "close"),
            patch("app.utils.token_manager.refresh_user_token", mock_refresh),
        ):
            await _job_token_refresh.__wrapped__()

        mock_refresh.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_refreshes_user_without_access_token(self, db_session: Session, test_user: User):
        """Users with refresh_token but no access_token get refreshed."""
        from app.jobs.core_jobs import _job_token_refresh

        test_user.refresh_token = "rt-test"
        test_user.access_token = None
        test_user.token_expires_at = None
        db_session.commit()

        mock_refresh = AsyncMock()
        mock_redis = MagicMock()
        mock_redis.set.return_value = True

        with (
            patch("app.database.SessionLocal", return_value=db_session),
            patch.object(db_session, "close"),
            patch("app.utils.token_manager.refresh_user_token", mock_refresh),
            patch("app.cache.intel_cache._get_redis", return_value=mock_redis),
        ):
            await _job_token_refresh.__wrapped__()

        mock_refresh.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_skips_locked_user(self, db_session: Session, test_user: User):
        """Users with Redis lock held are skipped."""
        from app.jobs.core_jobs import _job_token_refresh

        test_user.refresh_token = "rt-test"
        test_user.access_token = "at-test"
        test_user.token_expires_at = datetime.now(UTC) + timedelta(minutes=5)
        db_session.commit()

        mock_refresh = AsyncMock()
        mock_redis = MagicMock()
        mock_redis.set.return_value = False  # lock NOT acquired

        with (
            patch("app.database.SessionLocal", return_value=db_session),
            patch.object(db_session, "close"),
            patch("app.utils.token_manager.refresh_user_token", mock_refresh),
            patch("app.cache.intel_cache._get_redis", return_value=mock_redis),
        ):
            await _job_token_refresh.__wrapped__()

        mock_refresh.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_handles_refresh_error(self, db_session: Session, test_user: User):
        """Error during refresh is caught; m365_error_reason is set."""
        from app.jobs.core_jobs import _job_token_refresh

        test_user.refresh_token = "rt-test"
        test_user.access_token = None
        test_user.token_expires_at = None
        db_session.commit()

        mock_refresh = AsyncMock(side_effect=RuntimeError("Token endpoint down"))
        mock_redis = MagicMock()
        mock_redis.set.return_value = True

        with (
            patch("app.database.SessionLocal", return_value=db_session),
            patch.object(db_session, "close"),
            patch("app.utils.token_manager.refresh_user_token", mock_refresh),
            patch("app.cache.intel_cache._get_redis", return_value=mock_redis),
        ):
            await _job_token_refresh.__wrapped__()

        db_session.refresh(test_user)
        # The raw exception text must NOT leak into the user-facing reason; a
        # non-auth failure surfaces as the friendly self-healing message.
        from app.services.m365_status import REASON_TRANSIENT

        assert test_user.m365_error_reason == REASON_TRANSIENT
        assert "Token endpoint down" not in (test_user.m365_error_reason or "")

    @pytest.mark.asyncio
    async def test_works_without_redis(self, db_session: Session, test_user: User):
        """Refresh proceeds when Redis is unavailable (r is None)."""
        from app.jobs.core_jobs import _job_token_refresh

        test_user.refresh_token = "rt-test"
        test_user.access_token = None
        test_user.token_expires_at = None
        db_session.commit()

        mock_refresh = AsyncMock()

        with (
            patch("app.database.SessionLocal", return_value=db_session),
            patch.object(db_session, "close"),
            patch("app.utils.token_manager.refresh_user_token", mock_refresh),
            patch("app.cache.intel_cache._get_redis", return_value=None),
        ):
            await _job_token_refresh.__wrapped__()

        mock_refresh.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_selector_error_reraises(self, db_session: Session):
        """Exception during user selection phase is re-raised."""
        from app.jobs.core_jobs import _job_token_refresh

        mock_db = MagicMock()
        mock_db.query.side_effect = RuntimeError("selector DB error")

        with patch("app.database.SessionLocal", return_value=mock_db):
            with pytest.raises(RuntimeError, match="selector DB error"):
                await _job_token_refresh.__wrapped__()
        mock_db.close.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════
#  _job_inbox_scan
# ═══════════════════════════════════════════════════════════════════════


class TestJobInboxScan:
    """Tests for _job_inbox_scan()."""

    @pytest.mark.asyncio
    async def test_scans_connected_users(self, db_session: Session, test_user: User):
        """Connected users due for scan get scanned."""
        from app.jobs.core_jobs import _job_inbox_scan

        test_user.refresh_token = "rt-test"
        test_user.access_token = "at-test"
        test_user.m365_connected = True
        test_user.last_inbox_scan = None
        db_session.commit()

        mock_scan = AsyncMock()

        with (
            patch("app.database.SessionLocal", return_value=db_session),
            patch.object(db_session, "close"),
            patch("app.jobs.email_jobs._scan_user_inbox", mock_scan),
        ):
            await _job_inbox_scan.__wrapped__()

        mock_scan.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_skips_users_without_access_token(self, db_session: Session, test_user: User):
        """Users without access_token are skipped."""
        from app.jobs.core_jobs import _job_inbox_scan

        test_user.refresh_token = "rt-test"
        test_user.access_token = None
        test_user.m365_connected = True
        db_session.commit()

        mock_scan = AsyncMock()

        with (
            patch("app.database.SessionLocal", return_value=db_session),
            patch.object(db_session, "close"),
            patch("app.jobs.email_jobs._scan_user_inbox", mock_scan),
        ):
            await _job_inbox_scan.__wrapped__()

        mock_scan.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_disconnected_users(self, db_session: Session, test_user: User):
        """Users with m365_connected=False are skipped."""
        from app.jobs.core_jobs import _job_inbox_scan

        test_user.refresh_token = "rt-test"
        test_user.access_token = "at-test"
        test_user.m365_connected = False
        db_session.commit()

        mock_scan = AsyncMock()

        with (
            patch("app.database.SessionLocal", return_value=db_session),
            patch.object(db_session, "close"),
            patch("app.jobs.email_jobs._scan_user_inbox", mock_scan),
        ):
            await _job_inbox_scan.__wrapped__()

        mock_scan.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_recently_scanned_user(self, db_session: Session, test_user: User):
        """Users scanned within the interval are skipped."""
        from app.jobs.core_jobs import _job_inbox_scan

        test_user.refresh_token = "rt-test"
        test_user.access_token = "at-test"
        test_user.m365_connected = True
        test_user.last_inbox_scan = datetime.now(UTC) - timedelta(minutes=1)
        db_session.commit()

        mock_scan = AsyncMock()

        with (
            patch("app.database.SessionLocal", return_value=db_session),
            patch.object(db_session, "close"),
            patch("app.jobs.email_jobs._scan_user_inbox", mock_scan),
        ):
            await _job_inbox_scan.__wrapped__()

        mock_scan.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_handles_scan_timeout(self, db_session: Session, test_user: User):
        """TimeoutError during scan is caught, m365_error_reason set."""
        from app.jobs.core_jobs import _job_inbox_scan

        test_user.refresh_token = "rt-test"
        test_user.access_token = "at-test"
        test_user.m365_connected = True
        test_user.last_inbox_scan = None
        db_session.commit()

        async def slow_scan(user, db):
            await asyncio.sleep(999)

        with (
            patch("app.database.SessionLocal", return_value=db_session),
            patch.object(db_session, "close"),
            patch("app.jobs.email_jobs._scan_user_inbox", slow_scan),
            patch("asyncio.wait_for", side_effect=TimeoutError()),
        ):
            await _job_inbox_scan.__wrapped__()

        db_session.refresh(test_user)
        from app.services.m365_status import REASON_TRANSIENT

        assert test_user.m365_error_reason == REASON_TRANSIENT

    @pytest.mark.asyncio
    async def test_handles_scan_exception(self, db_session: Session, test_user: User):
        """Generic exception during scan sets m365_error_reason."""
        from app.jobs.core_jobs import _job_inbox_scan

        test_user.refresh_token = "rt-test"
        test_user.access_token = "at-test"
        test_user.m365_connected = True
        test_user.last_inbox_scan = None
        db_session.commit()

        mock_scan = AsyncMock(side_effect=RuntimeError("Graph API error"))

        with (
            patch("app.database.SessionLocal", return_value=db_session),
            patch.object(db_session, "close"),
            patch("app.jobs.email_jobs._scan_user_inbox", mock_scan),
        ):
            await _job_inbox_scan.__wrapped__()

        db_session.refresh(test_user)
        # Raw Graph exception text must not reach the user-facing reason.
        from app.services.m365_status import REASON_TRANSIENT

        assert test_user.m365_error_reason == REASON_TRANSIENT
        assert "Graph API error" not in (test_user.m365_error_reason or "")

    @pytest.mark.asyncio
    async def test_selector_error_reraises(self):
        """Exception during user selection phase is re-raised."""
        from app.jobs.core_jobs import _job_inbox_scan

        mock_db = MagicMock()
        mock_db.query.side_effect = RuntimeError("selector error")

        with patch("app.database.SessionLocal", return_value=mock_db):
            with pytest.raises(RuntimeError, match="selector error"):
                await _job_inbox_scan.__wrapped__()


# ═══════════════════════════════════════════════════════════════════════
#  _job_batch_results
# ═══════════════════════════════════════════════════════════════════════


class TestJobBatchResults:
    """Tests for _job_batch_results()."""

    @pytest.mark.asyncio
    async def test_processes_batch_results(self):
        """Successful batch processing logs result count."""
        from app.jobs.core_jobs import _job_batch_results

        mock_db = MagicMock()
        mock_process = AsyncMock(return_value=5)

        with (
            patch("app.database.SessionLocal", return_value=mock_db),
            patch("app.email_service.process_batch_results", mock_process),
            patch("asyncio.wait_for", mock_process),
        ):
            await _job_batch_results.__wrapped__()

        mock_db.close.assert_called_once()

    @pytest.mark.parametrize(
        ("side_effect", "expected_exc", "match"),
        [
            pytest.param(TimeoutError(), asyncio.TimeoutError, None, id="timeout"),
            pytest.param(ValueError("bad data"), ValueError, "bad data", id="generic_exception"),
        ],
    )
    @pytest.mark.asyncio
    async def test_reraises_and_closes(self, side_effect, expected_exc, match):
        """TimeoutError / generic exception is re-raised; session is closed."""
        from app.jobs.core_jobs import _job_batch_results

        mock_db = MagicMock()

        with (
            patch("app.database.SessionLocal", return_value=mock_db),
            patch("app.email_service.process_batch_results", AsyncMock()),
            patch("asyncio.wait_for", side_effect=side_effect),
        ):
            with pytest.raises(expected_exc, match=match):
                await _job_batch_results.__wrapped__()

        mock_db.close.assert_called_once()
