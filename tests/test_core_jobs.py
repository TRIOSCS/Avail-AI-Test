"""Tests for app/jobs/core_jobs.py — background job registration and execution.

Called by: pytest
Depends on: conftest fixtures, unittest.mock
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.constants import RequisitionStatus
from app.models import Requisition, User

# ═══════════════════════════════════════════════════════════════════════
#  register_core_jobs
# ═══════════════════════════════════════════════════════════════════════


class TestRegisterCoreJobs:
    """Tests for register_core_jobs()."""

    def test_registers_base_jobs(self):
        """All standard jobs are registered (without activity tracking)."""
        from app.jobs.core_jobs import register_core_jobs

        scheduler = MagicMock()
        settings = MagicMock()
        settings.inbox_scan_interval_min = 5
        settings.activity_tracking_enabled = False

        register_core_jobs(scheduler, settings)

        job_ids = [call.kwargs["id"] for call in scheduler.add_job.call_args_list]
        assert "auto_archive" in job_ids
        assert "token_refresh" in job_ids
        assert "inbox_scan" in job_ids
        assert "batch_results" in job_ids
        assert "batch_parse_signatures" in job_ids
        assert "poll_signature_batch" in job_ids
        # Webhook subs NOT registered when activity_tracking_enabled=False
        assert "webhook_subs" not in job_ids

    def test_registers_webhook_job_when_activity_tracking(self):
        """Webhook subscription job registered when activity_tracking_enabled=True."""
        from app.jobs.core_jobs import register_core_jobs

        scheduler = MagicMock()
        settings = MagicMock()
        settings.inbox_scan_interval_min = 5
        settings.activity_tracking_enabled = True

        register_core_jobs(scheduler, settings)

        job_ids = [call.kwargs["id"] for call in scheduler.add_job.call_args_list]
        assert "webhook_subs" in job_ids

    def test_total_job_count_without_webhooks(self):
        """Exactly 6 jobs when activity tracking disabled."""
        from app.jobs.core_jobs import register_core_jobs

        scheduler = MagicMock()
        settings = MagicMock()
        settings.inbox_scan_interval_min = 10
        settings.activity_tracking_enabled = False

        register_core_jobs(scheduler, settings)
        assert scheduler.add_job.call_count == 6

    def test_total_job_count_with_webhooks(self):
        """Exactly 7 jobs when activity tracking enabled."""
        from app.jobs.core_jobs import register_core_jobs

        scheduler = MagicMock()
        settings = MagicMock()
        settings.inbox_scan_interval_min = 10
        settings.activity_tracking_enabled = True

        register_core_jobs(scheduler, settings)
        assert scheduler.add_job.call_count == 7


# ═══════════════════════════════════════════════════════════════════════
#  _job_auto_archive
# ═══════════════════════════════════════════════════════════════════════


class TestJobAutoArchive:
    """Tests for _job_auto_archive()."""

    @pytest.mark.asyncio
    async def test_archives_stale_requisitions(self, db_session: Session, test_user: User):
        """Requisitions inactive >30 days get archived."""
        from app.jobs.core_jobs import _job_auto_archive

        req = Requisition(
            name="REQ-STALE-001",
            customer_name="Stale Corp",
            status=RequisitionStatus.ACTIVE,
            created_by=test_user.id,
            last_searched_at=datetime.now(timezone.utc) - timedelta(days=31),
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.commit()

        with patch("app.jobs.core_jobs.SessionLocal", return_value=db_session):
            await _job_auto_archive.__wrapped__()

        db_session.refresh(req)
        assert req.status == RequisitionStatus.ARCHIVED

    @pytest.mark.asyncio
    async def test_does_not_archive_recent_requisitions(self, db_session: Session, test_user: User):
        """Requisitions active within 30 days are untouched."""
        from app.jobs.core_jobs import _job_auto_archive

        req = Requisition(
            name="REQ-FRESH-001",
            customer_name="Fresh Corp",
            status=RequisitionStatus.ACTIVE,
            created_by=test_user.id,
            last_searched_at=datetime.now(timezone.utc) - timedelta(days=10),
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.commit()

        with patch("app.jobs.core_jobs.SessionLocal", return_value=db_session):
            await _job_auto_archive.__wrapped__()

        db_session.refresh(req)
        assert req.status == RequisitionStatus.ACTIVE

    @pytest.mark.asyncio
    async def test_does_not_archive_null_last_searched(self, db_session: Session, test_user: User):
        """Requisitions with no last_searched_at are skipped."""
        from app.jobs.core_jobs import _job_auto_archive

        req = Requisition(
            name="REQ-NULL-001",
            customer_name="Null Corp",
            status=RequisitionStatus.ACTIVE,
            created_by=test_user.id,
            last_searched_at=None,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.commit()

        with patch("app.jobs.core_jobs.SessionLocal", return_value=db_session):
            await _job_auto_archive.__wrapped__()

        db_session.refresh(req)
        assert req.status == RequisitionStatus.ACTIVE

    @pytest.mark.asyncio
    async def test_archives_multiple_stale(self, db_session: Session, test_user: User):
        """Multiple stale requisitions all get archived."""
        from app.jobs.core_jobs import _job_auto_archive

        for i in range(3):
            req = Requisition(
                name=f"REQ-MULTI-{i}",
                customer_name="Multi Corp",
                status=RequisitionStatus.ACTIVE,
                created_by=test_user.id,
                last_searched_at=datetime.now(timezone.utc) - timedelta(days=35),
                created_at=datetime.now(timezone.utc),
            )
            db_session.add(req)
        db_session.commit()

        with patch("app.jobs.core_jobs.SessionLocal", return_value=db_session):
            await _job_auto_archive.__wrapped__()

        stale = db_session.query(Requisition).filter(Requisition.status == RequisitionStatus.ARCHIVED).count()
        assert stale == 3

    @pytest.mark.asyncio
    async def test_auto_archive_handles_exception(self, db_session: Session):
        """Exception during query is caught and re-raised."""
        from app.jobs.core_jobs import _job_auto_archive

        mock_db = MagicMock()
        mock_db.query.side_effect = RuntimeError("DB connection lost")

        with patch("app.jobs.core_jobs.SessionLocal", return_value=mock_db):
            with pytest.raises(RuntimeError, match="DB connection lost"):
                await _job_auto_archive.__wrapped__()
        mock_db.rollback.assert_called_once()
        mock_db.close.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════
#  _job_token_refresh
# ═══════════════════════════════════════════════════════════════════════


class TestJobTokenRefresh:
    """Tests for _job_token_refresh()."""

    @pytest.mark.asyncio
    async def test_refreshes_expiring_tokens(self, db_session: Session, test_user: User):
        """Users with tokens expiring within 15 min get refreshed."""
        from app.jobs.core_jobs import _job_token_refresh

        test_user.refresh_token = "rt-test-123"
        test_user.access_token = "at-test-123"
        test_user.token_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
        db_session.commit()

        mock_refresh = AsyncMock()
        mock_redis = MagicMock()
        mock_redis.set.return_value = True  # acquired lock

        with (
            patch("app.jobs.core_jobs.SessionLocal", return_value=db_session),
            patch("app.jobs.core_jobs.refresh_user_token", mock_refresh),
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
            patch("app.jobs.core_jobs.SessionLocal", return_value=db_session),
            patch("app.jobs.core_jobs.refresh_user_token", mock_refresh),
        ):
            await _job_token_refresh.__wrapped__()

        mock_refresh.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_users_with_valid_token(self, db_session: Session, test_user: User):
        """Users with tokens expiring >15 min out are skipped."""
        from app.jobs.core_jobs import _job_token_refresh

        test_user.refresh_token = "rt-test"
        test_user.access_token = "at-test"
        test_user.token_expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
        db_session.commit()

        mock_refresh = AsyncMock()

        with (
            patch("app.jobs.core_jobs.SessionLocal", return_value=db_session),
            patch("app.jobs.core_jobs.refresh_user_token", mock_refresh),
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
            patch("app.jobs.core_jobs.SessionLocal", return_value=db_session),
            patch("app.jobs.core_jobs.refresh_user_token", mock_refresh),
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
        test_user.token_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
        db_session.commit()

        mock_refresh = AsyncMock()
        mock_redis = MagicMock()
        mock_redis.set.return_value = False  # lock NOT acquired

        with (
            patch("app.jobs.core_jobs.SessionLocal", return_value=db_session),
            patch("app.jobs.core_jobs.refresh_user_token", mock_refresh),
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
            patch("app.jobs.core_jobs.SessionLocal", return_value=db_session),
            patch("app.jobs.core_jobs.refresh_user_token", mock_refresh),
            patch("app.cache.intel_cache._get_redis", return_value=mock_redis),
        ):
            await _job_token_refresh.__wrapped__()

        db_session.refresh(test_user)
        assert "Token endpoint down" in (test_user.m365_error_reason or "")

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
            patch("app.jobs.core_jobs.SessionLocal", return_value=db_session),
            patch("app.jobs.core_jobs.refresh_user_token", mock_refresh),
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

        with patch("app.jobs.core_jobs.SessionLocal", return_value=mock_db):
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
            patch("app.jobs.core_jobs.SessionLocal", return_value=db_session),
            patch("app.jobs.core_jobs._scan_user_inbox", mock_scan),
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
            patch("app.jobs.core_jobs.SessionLocal", return_value=db_session),
            patch("app.jobs.core_jobs._scan_user_inbox", mock_scan),
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
            patch("app.jobs.core_jobs.SessionLocal", return_value=db_session),
            patch("app.jobs.core_jobs._scan_user_inbox", mock_scan),
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
        test_user.last_inbox_scan = datetime.now(timezone.utc) - timedelta(minutes=1)
        db_session.commit()

        mock_scan = AsyncMock()

        with (
            patch("app.jobs.core_jobs.SessionLocal", return_value=db_session),
            patch("app.jobs.core_jobs._scan_user_inbox", mock_scan),
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
            patch("app.jobs.core_jobs.SessionLocal", return_value=db_session),
            patch("app.jobs.core_jobs._scan_user_inbox", slow_scan),
            patch("asyncio.wait_for", side_effect=asyncio.TimeoutError()),
        ):
            await _job_inbox_scan.__wrapped__()

        db_session.refresh(test_user)
        assert test_user.m365_error_reason == "Inbox scan timed out"

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
            patch("app.jobs.core_jobs.SessionLocal", return_value=db_session),
            patch("app.jobs.core_jobs._scan_user_inbox", mock_scan),
        ):
            await _job_inbox_scan.__wrapped__()

        db_session.refresh(test_user)
        assert "Graph API error" in (test_user.m365_error_reason or "")

    @pytest.mark.asyncio
    async def test_selector_error_reraises(self):
        """Exception during user selection phase is re-raised."""
        from app.jobs.core_jobs import _job_inbox_scan

        mock_db = MagicMock()
        mock_db.query.side_effect = RuntimeError("selector error")

        with patch("app.jobs.core_jobs.SessionLocal", return_value=mock_db):
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
            patch("app.jobs.core_jobs.SessionLocal", return_value=mock_db),
            patch("app.email_service.process_batch_results", mock_process),
            patch("asyncio.wait_for", mock_process),
        ):
            await _job_batch_results.__wrapped__()

        mock_db.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_handles_timeout(self):
        """TimeoutError is re-raised for _traced_job to catch."""
        from app.jobs.core_jobs import _job_batch_results

        mock_db = MagicMock()

        with (
            patch("app.jobs.core_jobs.SessionLocal", return_value=mock_db),
            patch("app.email_service.process_batch_results", AsyncMock()),
            patch("asyncio.wait_for", side_effect=asyncio.TimeoutError()),
        ):
            with pytest.raises(asyncio.TimeoutError):
                await _job_batch_results.__wrapped__()

        mock_db.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_handles_generic_exception(self):
        """Generic exception is re-raised."""
        from app.jobs.core_jobs import _job_batch_results

        mock_db = MagicMock()

        with (
            patch("app.jobs.core_jobs.SessionLocal", return_value=mock_db),
            patch("app.email_service.process_batch_results", AsyncMock()),
            patch("asyncio.wait_for", side_effect=ValueError("bad data")),
        ):
            with pytest.raises(ValueError, match="bad data"):
                await _job_batch_results.__wrapped__()

        mock_db.close.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════
#  _job_batch_parse_signatures / _job_poll_signature_batch
# ═══════════════════════════════════════════════════════════════════════


class TestJobBatchParseSignatures:
    """Tests for _job_batch_parse_signatures()."""

    @pytest.mark.asyncio
    async def test_submits_batch(self):
        """Successful signature parse returns batch_id."""
        from app.jobs.core_jobs import _job_batch_parse_signatures

        mock_db = MagicMock()
        mock_parse = AsyncMock(return_value="sig-batch-123")

        with (
            patch("app.jobs.core_jobs.SessionLocal", return_value=mock_db),
            patch("app.services.signature_parser.batch_parse_signatures", mock_parse),
            patch("asyncio.wait_for", mock_parse),
        ):
            await _job_batch_parse_signatures.__wrapped__()

        mock_db.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_handles_timeout(self):
        from app.jobs.core_jobs import _job_batch_parse_signatures

        mock_db = MagicMock()

        with (
            patch("app.jobs.core_jobs.SessionLocal", return_value=mock_db),
            patch("app.services.signature_parser.batch_parse_signatures", AsyncMock()),
            patch("asyncio.wait_for", side_effect=asyncio.TimeoutError()),
        ):
            with pytest.raises(asyncio.TimeoutError):
                await _job_batch_parse_signatures.__wrapped__()

        mock_db.rollback.assert_called_once()

    @pytest.mark.asyncio
    async def test_handles_exception(self):
        from app.jobs.core_jobs import _job_batch_parse_signatures

        mock_db = MagicMock()

        with (
            patch("app.jobs.core_jobs.SessionLocal", return_value=mock_db),
            patch("app.services.signature_parser.batch_parse_signatures", AsyncMock()),
            patch("asyncio.wait_for", side_effect=RuntimeError("parse error")),
        ):
            with pytest.raises(RuntimeError, match="parse error"):
                await _job_batch_parse_signatures.__wrapped__()

        mock_db.rollback.assert_called_once()


class TestJobPollSignatureBatch:
    """Tests for _job_poll_signature_batch()."""

    @pytest.mark.asyncio
    async def test_processes_results(self):
        from app.jobs.core_jobs import _job_poll_signature_batch

        mock_db = MagicMock()
        mock_poll = AsyncMock(return_value={"applied": 5, "errors": 0})

        with (
            patch("app.jobs.core_jobs.SessionLocal", return_value=mock_db),
            patch("app.services.signature_parser.process_signature_batch_results", mock_poll),
            patch("asyncio.wait_for", mock_poll),
        ):
            await _job_poll_signature_batch.__wrapped__()

        mock_db.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_handles_timeout(self):
        from app.jobs.core_jobs import _job_poll_signature_batch

        mock_db = MagicMock()

        with (
            patch("app.jobs.core_jobs.SessionLocal", return_value=mock_db),
            patch("app.services.signature_parser.process_signature_batch_results", AsyncMock()),
            patch("asyncio.wait_for", side_effect=asyncio.TimeoutError()),
        ):
            with pytest.raises(asyncio.TimeoutError):
                await _job_poll_signature_batch.__wrapped__()

        mock_db.rollback.assert_called_once()

    @pytest.mark.asyncio
    async def test_handles_exception(self):
        from app.jobs.core_jobs import _job_poll_signature_batch

        mock_db = MagicMock()

        with (
            patch("app.jobs.core_jobs.SessionLocal", return_value=mock_db),
            patch("app.services.signature_parser.process_signature_batch_results", AsyncMock()),
            patch("asyncio.wait_for", side_effect=RuntimeError("poll error")),
        ):
            with pytest.raises(RuntimeError, match="poll error"):
                await _job_poll_signature_batch.__wrapped__()

        mock_db.rollback.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════
#  _job_webhook_subscriptions
# ═══════════════════════════════════════════════════════════════════════


class TestJobWebhookSubscriptions:
    """Tests for _job_webhook_subscriptions()."""

    @pytest.mark.asyncio
    async def test_calls_renew_and_ensure(self):
        """Both renew and ensure functions are called."""
        from app.jobs.core_jobs import _job_webhook_subscriptions

        mock_db = MagicMock()
        mock_renew = AsyncMock()
        mock_ensure = AsyncMock()

        with (
            patch("app.jobs.core_jobs.SessionLocal", return_value=mock_db),
            patch("app.services.webhook_service.renew_expiring_subscriptions", mock_renew),
            patch("app.services.webhook_service.ensure_all_users_subscribed", mock_ensure),
        ):
            await _job_webhook_subscriptions.__wrapped__()

        mock_renew.assert_awaited_once()
        mock_ensure.assert_awaited_once()
        mock_db.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_handles_exception(self):
        """Exception causes rollback and re-raise."""
        from app.jobs.core_jobs import _job_webhook_subscriptions

        mock_db = MagicMock()

        with (
            patch("app.jobs.core_jobs.SessionLocal", return_value=mock_db),
            patch(
                "app.services.webhook_service.renew_expiring_subscriptions",
                AsyncMock(side_effect=RuntimeError("webhook error")),
            ),
        ):
            with pytest.raises(RuntimeError, match="webhook error"):
                await _job_webhook_subscriptions.__wrapped__()

        mock_db.rollback.assert_called_once()
        mock_db.close.assert_called_once()
