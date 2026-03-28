"""Tests for app/jobs/email_jobs.py — Email, contacts, and calendar background jobs.

Covers: register_email_jobs, all job functions, inbox scanning helpers,
contact mining, outbound RFQ scanning, vendor scoring, contacts sync,
sent folder scanning, attachment detection.

Called by: pytest
Depends on: conftest fixtures (db_session, test_user)
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models import User

# ── Helpers ──────────────────────────────────────────────────────────


def _make_user(db: Session, email="sync@trioscs.com", **kw) -> User:
    u = User(
        email=email,
        name="Sync User",
        role="buyer",
        azure_id=f"azure-{email}",
        m365_connected=True,
        access_token="fake-token",
        refresh_token="fake-refresh",
        created_at=datetime.now(timezone.utc),
        **kw,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


# ── register_email_jobs ──────────────────────────────────────────────


class TestRegisterEmailJobs:
    def test_register_all_enabled(self):
        from app.jobs.email_jobs import register_email_jobs

        scheduler = MagicMock()
        settings = MagicMock()
        settings.contacts_sync_enabled = True
        settings.activity_tracking_enabled = True
        settings.ownership_sweep_enabled = True
        settings.contact_scoring_enabled = True
        settings.customer_enrichment_enabled = True
        register_email_jobs(scheduler, settings)
        # At minimum: contacts_sync, ownership_sweep, site_ownership_sweep,
        # contact_scoring, contact_status_compute, email_health_update,
        # calendar_scan, scan_sent_folders, email_reverification
        assert scheduler.add_job.call_count >= 9

    def test_register_minimal(self):
        from app.jobs.email_jobs import register_email_jobs

        scheduler = MagicMock()
        settings = MagicMock()
        settings.contacts_sync_enabled = False
        settings.activity_tracking_enabled = False
        settings.ownership_sweep_enabled = False
        settings.contact_scoring_enabled = False
        settings.customer_enrichment_enabled = False
        register_email_jobs(scheduler, settings)
        # Always registered: contact_status_compute, email_health, calendar_scan, sent_folder_scan
        assert scheduler.add_job.call_count >= 4

    def test_register_activity_without_ownership(self):
        from app.jobs.email_jobs import register_email_jobs

        scheduler = MagicMock()
        settings = MagicMock()
        settings.contacts_sync_enabled = False
        settings.activity_tracking_enabled = True
        settings.ownership_sweep_enabled = False
        settings.contact_scoring_enabled = False
        settings.customer_enrichment_enabled = False
        register_email_jobs(scheduler, settings)
        # No ownership_sweep or site_ownership_sweep, but logs info
        job_ids = [call.kwargs.get("id") or call.args[2] for call in scheduler.add_job.call_args_list]
        assert "ownership_sweep" not in job_ids


# ── _job_contacts_sync ───────────────────────────────────────────────


class TestJobContactsSync:
    @pytest.mark.asyncio
    @patch("app.jobs.email_jobs._sync_user_contacts", new_callable=AsyncMock)
    async def test_contacts_sync_no_users(self, mock_sync):
        from app.jobs.email_jobs import _job_contacts_sync

        with patch("app.jobs.email_jobs.SessionLocal") as MockSL:
            mock_db = MagicMock()
            mock_db.query.return_value.filter.return_value.all.return_value = []
            MockSL.return_value = mock_db
            await _job_contacts_sync()
        mock_sync.assert_not_called()

    @pytest.mark.asyncio
    @patch("app.jobs.email_jobs._sync_user_contacts", new_callable=AsyncMock)
    async def test_contacts_sync_with_user(self, mock_sync):
        from app.jobs.email_jobs import _job_contacts_sync

        user = MagicMock()
        user.id = 1
        user.access_token = "tok"
        user.m365_connected = True
        user.last_contacts_sync = None
        user.refresh_token = "ref"

        with patch("app.jobs.email_jobs.SessionLocal") as MockSL:
            list_db = MagicMock()
            list_db.query.return_value.filter.return_value.all.return_value = [user]
            sync_db = MagicMock()
            sync_db.get.return_value = user
            MockSL.side_effect = [list_db, sync_db]
            await _job_contacts_sync()
        mock_sync.assert_called_once()

    @pytest.mark.asyncio
    @patch("app.jobs.email_jobs._sync_user_contacts", new_callable=AsyncMock, side_effect=asyncio.TimeoutError)
    async def test_contacts_sync_timeout(self, mock_sync):
        from app.jobs.email_jobs import _job_contacts_sync

        user = MagicMock()
        user.id = 1
        user.access_token = "tok"
        user.m365_connected = True
        user.last_contacts_sync = None

        with patch("app.jobs.email_jobs.SessionLocal") as MockSL:
            list_db = MagicMock()
            list_db.query.return_value.filter.return_value.all.return_value = [user]
            sync_db = MagicMock()
            sync_db.get.return_value = user
            MockSL.side_effect = [list_db, sync_db]
            # Should not raise - timeout is handled
            await _job_contacts_sync()

    @pytest.mark.asyncio
    async def test_contacts_sync_skips_no_token(self):
        from app.jobs.email_jobs import _job_contacts_sync

        user = MagicMock()
        user.id = 1
        user.access_token = None  # no token
        user.m365_connected = True
        user.last_contacts_sync = None
        user.refresh_token = "ref"

        with patch("app.jobs.email_jobs.SessionLocal") as MockSL:
            mock_db = MagicMock()
            mock_db.query.return_value.filter.return_value.all.return_value = [user]
            MockSL.return_value = mock_db
            await _job_contacts_sync()

    @pytest.mark.asyncio
    async def test_contacts_sync_skips_recent(self):
        from app.jobs.email_jobs import _job_contacts_sync

        user = MagicMock()
        user.id = 1
        user.access_token = "tok"
        user.m365_connected = True
        user.refresh_token = "ref"
        user.last_contacts_sync = datetime.now(timezone.utc) - timedelta(hours=1)  # recent

        with patch("app.jobs.email_jobs.SessionLocal") as MockSL:
            mock_db = MagicMock()
            mock_db.query.return_value.filter.return_value.all.return_value = [user]
            MockSL.return_value = mock_db
            await _job_contacts_sync()


# ── _job_ownership_sweep ─────────────────────────────────────────────


class TestJobOwnershipSweep:
    @pytest.mark.asyncio
    async def test_ownership_sweep(self):
        from app.jobs.email_jobs import _job_ownership_sweep

        with (
            patch("app.jobs.email_jobs.SessionLocal") as MockSL,
            patch("app.services.ownership_service.run_ownership_sweep", new_callable=AsyncMock) as mock_sweep,
        ):
            mock_db = MagicMock()
            MockSL.return_value = mock_db
            await _job_ownership_sweep()
            mock_sweep.assert_called_once_with(mock_db)

    @pytest.mark.asyncio
    async def test_ownership_sweep_error(self):
        from app.jobs.email_jobs import _job_ownership_sweep

        with (
            patch("app.jobs.email_jobs.SessionLocal") as MockSL,
            patch(
                "app.services.ownership_service.run_ownership_sweep",
                new_callable=AsyncMock,
                side_effect=Exception("DB error"),
            ),
        ):
            mock_db = MagicMock()
            MockSL.return_value = mock_db
            with pytest.raises(Exception, match="DB error"):
                await _job_ownership_sweep()
            mock_db.rollback.assert_called_once()


# ── _job_site_ownership_sweep ────────────────────────────────────────


class TestJobSiteOwnershipSweep:
    @pytest.mark.asyncio
    async def test_site_sweep(self):
        from app.jobs.email_jobs import _job_site_ownership_sweep

        with (
            patch("app.jobs.email_jobs.SessionLocal") as MockSL,
            patch("app.services.ownership_service.run_site_ownership_sweep") as mock_sweep,
        ):
            mock_db = MagicMock()
            MockSL.return_value = mock_db
            await _job_site_ownership_sweep()
            mock_sweep.assert_called_once()

    @pytest.mark.asyncio
    async def test_site_sweep_error(self):
        from app.jobs.email_jobs import _job_site_ownership_sweep

        with (
            patch("app.jobs.email_jobs.SessionLocal") as MockSL,
            patch(
                "app.services.ownership_service.run_site_ownership_sweep",
                side_effect=Exception("fail"),
            ),
        ):
            mock_db = MagicMock()
            MockSL.return_value = mock_db
            with pytest.raises(Exception, match="fail"):
                await _job_site_ownership_sweep()
            mock_db.rollback.assert_called_once()


# ── _job_contact_scoring ─────────────────────────────────────────────


class TestJobContactScoring:
    @pytest.mark.asyncio
    async def test_contact_scoring(self):
        from app.jobs.email_jobs import _job_contact_scoring

        with (
            patch("app.jobs.email_jobs.SessionLocal") as MockSL,
            patch(
                "app.services.contact_intelligence.compute_all_contact_scores",
                return_value={"updated": 5, "skipped": 2},
            ),
        ):
            mock_db = MagicMock()
            MockSL.return_value = mock_db
            await _job_contact_scoring()

    @pytest.mark.asyncio
    async def test_contact_scoring_timeout(self):
        from app.jobs.email_jobs import _job_contact_scoring

        def slow_fn(*a, **kw):
            import time

            time.sleep(10)

        with (
            patch("app.jobs.email_jobs.SessionLocal") as MockSL,
            patch("app.services.contact_intelligence.compute_all_contact_scores", side_effect=slow_fn),
            patch("app.jobs.email_jobs.asyncio.wait_for", side_effect=asyncio.TimeoutError),
        ):
            mock_db = MagicMock()
            MockSL.return_value = mock_db
            with pytest.raises(asyncio.TimeoutError):
                await _job_contact_scoring()
            mock_db.rollback.assert_called_once()

    @pytest.mark.asyncio
    async def test_contact_scoring_error(self):
        from app.jobs.email_jobs import _job_contact_scoring

        with (
            patch("app.jobs.email_jobs.SessionLocal") as MockSL,
            patch(
                "app.services.contact_intelligence.compute_all_contact_scores",
                side_effect=Exception("score err"),
            ),
            patch("app.jobs.email_jobs.asyncio.wait_for", side_effect=Exception("score err")),
        ):
            mock_db = MagicMock()
            MockSL.return_value = mock_db
            with pytest.raises(Exception, match="score err"):
                await _job_contact_scoring()
            mock_db.rollback.assert_called_once()


# ── _job_contact_status_compute ──────────────────────────────────────


class TestJobContactStatusCompute:
    @pytest.mark.asyncio
    async def test_status_compute_active(self):
        from app.jobs.email_jobs import _job_contact_status_compute

        sc = MagicMock()
        sc.contact_status = "new"
        sc.is_active = True
        sc.created_at = datetime.now(timezone.utc) - timedelta(days=2)
        last_at = datetime.now(timezone.utc) - timedelta(days=3)

        with patch("app.jobs.email_jobs.SessionLocal") as MockSL:
            mock_db = MagicMock()
            mock_db.query.return_value.outerjoin.return_value.filter.return_value.all.return_value = [(sc, last_at)]
            MockSL.return_value = mock_db
            await _job_contact_status_compute()
            assert sc.contact_status == "active"

    @pytest.mark.asyncio
    async def test_status_compute_quiet(self):
        from app.jobs.email_jobs import _job_contact_status_compute

        sc = MagicMock()
        sc.contact_status = "active"
        sc.is_active = True
        sc.created_at = datetime.now(timezone.utc) - timedelta(days=100)
        last_at = datetime.now(timezone.utc) - timedelta(days=60)

        with patch("app.jobs.email_jobs.SessionLocal") as MockSL:
            mock_db = MagicMock()
            mock_db.query.return_value.outerjoin.return_value.filter.return_value.all.return_value = [(sc, last_at)]
            MockSL.return_value = mock_db
            await _job_contact_status_compute()
            assert sc.contact_status == "quiet"

    @pytest.mark.asyncio
    async def test_status_compute_inactive_old(self):
        from app.jobs.email_jobs import _job_contact_status_compute

        sc = MagicMock()
        sc.contact_status = "active"
        sc.is_active = True
        sc.created_at = datetime.now(timezone.utc) - timedelta(days=200)
        last_at = datetime.now(timezone.utc) - timedelta(days=100)

        with patch("app.jobs.email_jobs.SessionLocal") as MockSL:
            mock_db = MagicMock()
            mock_db.query.return_value.outerjoin.return_value.filter.return_value.all.return_value = [(sc, last_at)]
            MockSL.return_value = mock_db
            await _job_contact_status_compute()
            assert sc.contact_status == "inactive"

    @pytest.mark.asyncio
    async def test_status_compute_champion_not_downgraded(self):
        from app.jobs.email_jobs import _job_contact_status_compute

        sc = MagicMock()
        sc.contact_status = "champion"
        sc.is_active = True
        last_at = datetime.now(timezone.utc) - timedelta(days=365)

        with patch("app.jobs.email_jobs.SessionLocal") as MockSL:
            mock_db = MagicMock()
            mock_db.query.return_value.outerjoin.return_value.filter.return_value.all.return_value = [(sc, last_at)]
            MockSL.return_value = mock_db
            await _job_contact_status_compute()
            assert sc.contact_status == "champion"

    @pytest.mark.asyncio
    async def test_status_compute_no_activity_old_creation(self):
        from app.jobs.email_jobs import _job_contact_status_compute

        sc = MagicMock()
        sc.contact_status = "new"
        sc.is_active = True
        sc.created_at = datetime.now(timezone.utc) - timedelta(days=120)

        with patch("app.jobs.email_jobs.SessionLocal") as MockSL:
            mock_db = MagicMock()
            mock_db.query.return_value.outerjoin.return_value.filter.return_value.all.return_value = [
                (sc, None)  # No activity
            ]
            MockSL.return_value = mock_db
            await _job_contact_status_compute()
            assert sc.contact_status == "inactive"

    @pytest.mark.asyncio
    async def test_status_compute_no_activity_new_creation(self):
        from app.jobs.email_jobs import _job_contact_status_compute

        sc = MagicMock()
        sc.contact_status = "new"
        sc.is_active = True
        sc.created_at = datetime.now(timezone.utc) - timedelta(days=10)

        with patch("app.jobs.email_jobs.SessionLocal") as MockSL:
            mock_db = MagicMock()
            mock_db.query.return_value.outerjoin.return_value.filter.return_value.all.return_value = [(sc, None)]
            MockSL.return_value = mock_db
            await _job_contact_status_compute()
            # Keep 'new' status since created < 90 days ago
            assert sc.contact_status == "new"

    @pytest.mark.asyncio
    async def test_status_compute_7_to_30_day_window_no_downgrade(self):
        from app.jobs.email_jobs import _job_contact_status_compute

        sc = MagicMock()
        sc.contact_status = "active"
        sc.is_active = True
        last_at = datetime.now(timezone.utc) - timedelta(days=15)

        with patch("app.jobs.email_jobs.SessionLocal") as MockSL:
            mock_db = MagicMock()
            mock_db.query.return_value.outerjoin.return_value.filter.return_value.all.return_value = [(sc, last_at)]
            MockSL.return_value = mock_db
            await _job_contact_status_compute()
            # Should not change — in 7-30 day window, no auto-downgrade
            assert sc.contact_status == "active"

    @pytest.mark.asyncio
    async def test_status_compute_error(self):
        from app.jobs.email_jobs import _job_contact_status_compute

        with patch("app.jobs.email_jobs.SessionLocal") as MockSL:
            mock_db = MagicMock()
            mock_db.query.side_effect = Exception("DB down")
            MockSL.return_value = mock_db
            with pytest.raises(Exception, match="DB down"):
                await _job_contact_status_compute()
            mock_db.rollback.assert_called_once()


# ── _job_email_health_update ─────────────────────────────────────────


class TestJobEmailHealthUpdate:
    @pytest.mark.asyncio
    async def test_email_health_update(self):
        from app.jobs.email_jobs import _job_email_health_update

        with (
            patch("app.jobs.email_jobs.SessionLocal") as MockSL,
            patch(
                "app.services.response_analytics.batch_update_email_health",
                return_value={"updated": 10},
            ) as mock_fn,
            patch("app.jobs.email_jobs.asyncio.wait_for", new_callable=AsyncMock, return_value={"updated": 10}),
        ):
            mock_db = MagicMock()
            MockSL.return_value = mock_db
            await _job_email_health_update()

    @pytest.mark.asyncio
    async def test_email_health_timeout(self):
        from app.jobs.email_jobs import _job_email_health_update

        with (
            patch("app.jobs.email_jobs.SessionLocal") as MockSL,
            patch("app.jobs.email_jobs.asyncio.wait_for", side_effect=asyncio.TimeoutError),
        ):
            mock_db = MagicMock()
            MockSL.return_value = mock_db
            with pytest.raises(asyncio.TimeoutError):
                await _job_email_health_update()
            mock_db.rollback.assert_called_once()

    @pytest.mark.asyncio
    async def test_email_health_error(self):
        from app.jobs.email_jobs import _job_email_health_update

        with (
            patch("app.jobs.email_jobs.SessionLocal") as MockSL,
            patch("app.jobs.email_jobs.asyncio.wait_for", side_effect=RuntimeError("fail")),
        ):
            mock_db = MagicMock()
            MockSL.return_value = mock_db
            with pytest.raises(RuntimeError, match="fail"):
                await _job_email_health_update()
            mock_db.rollback.assert_called_once()


# ── _job_calendar_scan ───────────────────────────────────────────────


class TestJobCalendarScan:
    @pytest.mark.asyncio
    async def test_calendar_scan_no_users(self):
        from app.jobs.email_jobs import _job_calendar_scan

        with patch("app.jobs.email_jobs.SessionLocal") as MockSL:
            mock_db = MagicMock()
            mock_db.query.return_value.filter.return_value.all.return_value = []
            MockSL.return_value = mock_db
            await _job_calendar_scan()

    @pytest.mark.asyncio
    async def test_calendar_scan_with_user(self):
        from app.jobs.email_jobs import _job_calendar_scan

        user = MagicMock()
        user.id = 1
        user.email = "test@trioscs.com"
        user.access_token = "tok"
        user.m365_connected = True

        with (
            patch("app.jobs.email_jobs.SessionLocal") as MockSL,
            patch(
                "app.services.calendar_intelligence.scan_calendar_events",
                new_callable=AsyncMock,
                return_value={"events_found": 3},
            ),
            patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="valid-tok"),
        ):
            list_db = MagicMock()
            list_db.query.return_value.filter.return_value.all.return_value = [user]
            scan_db = MagicMock()
            scan_db.get.return_value = user
            MockSL.side_effect = [list_db, scan_db]
            await _job_calendar_scan()


# ── _job_email_reverification ────────────────────────────────────────


class TestJobEmailReverification:
    @pytest.mark.asyncio
    async def test_reverification(self):
        from app.jobs.email_jobs import _job_email_reverification

        with (
            patch("app.jobs.email_jobs.SessionLocal") as MockSL,
            patch(
                "app.services.customer_enrichment_batch.run_email_reverification",
                new_callable=AsyncMock,
                return_value={"processed": 50, "invalidated": 3},
            ),
        ):
            mock_db = MagicMock()
            MockSL.return_value = mock_db
            await _job_email_reverification()
            mock_db.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_reverification_error(self):
        from app.jobs.email_jobs import _job_email_reverification

        with (
            patch("app.jobs.email_jobs.SessionLocal") as MockSL,
            patch(
                "app.services.customer_enrichment_batch.run_email_reverification",
                new_callable=AsyncMock,
                side_effect=Exception("verify err"),
            ),
        ):
            mock_db = MagicMock()
            MockSL.return_value = mock_db
            with pytest.raises(Exception, match="verify err"):
                await _job_email_reverification()
            mock_db.rollback.assert_called_once()


# ── _scan_user_inbox ─────────────────────────────────────────────────


class TestScanUserInbox:
    @pytest.mark.asyncio
    async def test_scan_inbox_basic(self):
        from app.jobs.email_jobs import _scan_user_inbox

        user = MagicMock()
        user.email = "buyer@trioscs.com"
        user.last_inbox_scan = datetime.now(timezone.utc) - timedelta(hours=1)
        user.access_token = "tok"
        mock_db = MagicMock()

        with (
            patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="valid-tok"),
            patch("app.email_service.poll_inbox", new_callable=AsyncMock, return_value=[{"id": 1}]),
            patch("app.jobs.email_jobs._scan_stock_list_attachments", new_callable=AsyncMock),
            patch("app.jobs.email_jobs._mine_vendor_contacts", new_callable=AsyncMock),
            patch("app.jobs.email_jobs._scan_outbound_rfqs", new_callable=AsyncMock),
            patch("app.jobs.email_jobs._scan_excess_bid_responses", new_callable=AsyncMock),
        ):
            await _scan_user_inbox(user, mock_db)
            mock_db.commit.assert_called()

    @pytest.mark.asyncio
    async def test_scan_inbox_no_token(self):
        from app.jobs.email_jobs import _scan_user_inbox

        user = MagicMock()
        user.email = "buyer@trioscs.com"
        user.last_inbox_scan = datetime.now(timezone.utc)
        mock_db = MagicMock()

        with patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value=None):
            await _scan_user_inbox(user, mock_db)
            # Should return without committing
            mock_db.commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_scan_inbox_poll_failure(self):
        from app.jobs.email_jobs import _scan_user_inbox

        user = MagicMock()
        user.email = "buyer@trioscs.com"
        user.last_inbox_scan = datetime.now(timezone.utc)
        mock_db = MagicMock()

        with (
            patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="tok"),
            patch("app.email_service.poll_inbox", new_callable=AsyncMock, side_effect=Exception("Graph error")),
            patch("app.jobs.email_jobs._scan_stock_list_attachments", new_callable=AsyncMock),
            patch("app.jobs.email_jobs._mine_vendor_contacts", new_callable=AsyncMock),
            patch("app.jobs.email_jobs._scan_outbound_rfqs", new_callable=AsyncMock),
            patch("app.jobs.email_jobs._scan_excess_bid_responses", new_callable=AsyncMock),
        ):
            await _scan_user_inbox(user, mock_db)
            # poll failed, so last_inbox_scan should NOT update
            mock_db.commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_scan_inbox_sub_op_failure(self):
        from app.jobs.email_jobs import _scan_user_inbox

        user = MagicMock()
        user.email = "buyer@trioscs.com"
        user.last_inbox_scan = datetime.now(timezone.utc)
        mock_db = MagicMock()

        with (
            patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="tok"),
            patch("app.email_service.poll_inbox", new_callable=AsyncMock, return_value=[]),
            patch(
                "app.jobs.email_jobs._scan_stock_list_attachments",
                new_callable=AsyncMock,
                side_effect=Exception("stock err"),
            ),
            patch("app.jobs.email_jobs._mine_vendor_contacts", new_callable=AsyncMock),
            patch("app.jobs.email_jobs._scan_outbound_rfqs", new_callable=AsyncMock),
            patch("app.jobs.email_jobs._scan_excess_bid_responses", new_callable=AsyncMock),
        ):
            # Should not raise even when sub-ops fail
            await _scan_user_inbox(user, mock_db)

    @pytest.mark.asyncio
    async def test_scan_inbox_first_time_backfill(self):
        from app.jobs.email_jobs import _scan_user_inbox

        user = MagicMock()
        user.email = "buyer@trioscs.com"
        user.last_inbox_scan = None  # first time
        mock_db = MagicMock()

        with (
            patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="tok"),
            patch("app.email_service.poll_inbox", new_callable=AsyncMock, return_value=[]),
            patch("app.jobs.email_jobs._scan_stock_list_attachments", new_callable=AsyncMock) as mock_stock,
            patch("app.jobs.email_jobs._mine_vendor_contacts", new_callable=AsyncMock) as mock_mine,
            patch("app.jobs.email_jobs._scan_outbound_rfqs", new_callable=AsyncMock) as mock_rfq,
            patch("app.jobs.email_jobs._scan_excess_bid_responses", new_callable=AsyncMock),
        ):
            await _scan_user_inbox(user, mock_db)
            # Verify is_backfill=True passed to sub-ops
            mock_stock.assert_called_once()
            args = mock_stock.call_args
            assert args[0][2] is True  # is_backfill


# ── _scan_excess_bid_responses ───────────────────────────────────────


class TestScanExcessBidResponses:
    @pytest.mark.asyncio
    async def test_scan_disabled(self):
        from app.jobs.email_jobs import _scan_excess_bid_responses

        user = MagicMock()
        mock_db = MagicMock()

        with patch("app.config.settings") as mock_settings:
            mock_settings.excess_bid_scan_enabled = False
            await _scan_excess_bid_responses(user, mock_db)

    @pytest.mark.asyncio
    async def test_scan_no_pending(self):
        from app.jobs.email_jobs import _scan_excess_bid_responses

        user = MagicMock()
        user.id = 1
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.count.return_value = 0

        with patch("app.config.settings") as mock_settings:
            mock_settings.excess_bid_scan_enabled = True
            mock_settings.excess_bid_parse_lookback_days = 30
            await _scan_excess_bid_responses(user, mock_db)

    @pytest.mark.asyncio
    async def test_scan_with_bids(self):
        from app.jobs.email_jobs import _scan_excess_bid_responses

        user = MagicMock()
        user.id = 1
        user.email = "buyer@trioscs.com"
        user.access_token = "tok"
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.count.return_value = 1

        solicitation = MagicMock()
        solicitation.status = "sent"
        mock_db.get.return_value = solicitation

        gc_mock = MagicMock()
        gc_mock.get_json = AsyncMock(
            return_value={
                "value": [
                    {
                        "subject": "Re: [EXCESS-BID-42] Stock Offer",
                        "body": {"content": "We bid $1.50 for 500 pcs"},
                        "receivedDateTime": "2026-03-01T10:00:00Z",
                    }
                ]
            }
        )

        with (
            patch("app.config.settings") as mock_settings,
            patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="tok"),
            patch("app.utils.graph_client.GraphClient", return_value=gc_mock),
            patch("app.services.excess_service.parse_bid_from_email", new_callable=AsyncMock, return_value=MagicMock()),
        ):
            mock_settings.excess_bid_scan_enabled = True
            mock_settings.excess_bid_parse_lookback_days = 30
            await _scan_excess_bid_responses(user, mock_db)


# ── _mine_vendor_contacts ────────────────────────────────────────────


class TestMineVendorContacts:
    @pytest.mark.asyncio
    async def test_mine_no_contacts(self):
        from app.jobs.email_jobs import _mine_vendor_contacts

        user = MagicMock()
        user.access_token = "tok"
        mock_db = MagicMock()

        miner_mock = MagicMock()
        miner_mock.scan_inbox = AsyncMock(return_value={"contacts_enriched": []})

        with (
            patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="tok"),
            patch("app.connectors.email_mining.EmailMiner", return_value=miner_mock),
        ):
            await _mine_vendor_contacts(user, mock_db)

    @pytest.mark.asyncio
    async def test_mine_with_contacts(self):
        from app.jobs.email_jobs import _mine_vendor_contacts

        user = MagicMock()
        user.email = "buyer@trioscs.com"
        user.access_token = "tok"
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.all.return_value = []

        miner_mock = MagicMock()
        miner_mock.scan_inbox = AsyncMock(
            return_value={
                "contacts_enriched": [
                    {
                        "vendor_name": "Arrow Electronics",
                        "emails": ["sales@arrow.com"],
                        "phones": ["+1-555-0100"],
                        "websites": ["arrow.com"],
                    }
                ]
            }
        )

        with (
            patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="tok"),
            patch("app.connectors.email_mining.EmailMiner", return_value=miner_mock),
            patch("app.vendor_utils.merge_emails_into_card", return_value=1),
            patch("app.vendor_utils.merge_phones_into_card"),
        ):
            await _mine_vendor_contacts(user, mock_db)
            mock_db.commit.assert_called()


# ── _scan_outbound_rfqs ──────────────────────────────────────────────


class TestScanOutboundRfqs:
    @pytest.mark.asyncio
    async def test_scan_no_vendors(self):
        from app.jobs.email_jobs import _scan_outbound_rfqs

        user = MagicMock()
        user.access_token = "tok"
        mock_db = MagicMock()

        miner_mock = MagicMock()
        miner_mock.scan_sent_items = AsyncMock(
            return_value={
                "rfqs_detected": 0,
                "vendors_contacted": {},
            }
        )

        with (
            patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="tok"),
            patch("app.connectors.email_mining.EmailMiner", return_value=miner_mock),
        ):
            await _scan_outbound_rfqs(user, mock_db)

    @pytest.mark.asyncio
    async def test_scan_with_vendors(self):
        from app.jobs.email_jobs import _scan_outbound_rfqs

        user = MagicMock()
        user.email = "buyer@trioscs.com"
        user.access_token = "tok"
        mock_db = MagicMock()

        card = MagicMock()
        card.total_outreach = 5
        card.last_contact_at = None
        card.domain = "arrow.com"
        card.normalized_name = "arrowelectronics"
        mock_db.query.return_value.filter.return_value.all.return_value = [card]

        miner_mock = MagicMock()
        miner_mock.scan_sent_items = AsyncMock(
            return_value={
                "rfqs_detected": 2,
                "vendors_contacted": {"arrow.com": 2},
            }
        )

        with (
            patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="tok"),
            patch("app.connectors.email_mining.EmailMiner", return_value=miner_mock),
        ):
            await _scan_outbound_rfqs(user, mock_db)
            assert card.total_outreach == 7  # 5 + 2


# ── _sync_user_contacts ─────────────────────────────────────────────


class TestSyncUserContacts:
    @pytest.mark.asyncio
    async def test_sync_basic(self):
        from app.jobs.email_jobs import _sync_user_contacts

        user = MagicMock()
        user.id = 1
        user.email = "buyer@trioscs.com"
        user.access_token = "tok"
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = None
        mock_db.query.return_value.filter.return_value.all.return_value = []

        gc_mock = MagicMock()
        gc_mock.delta_query = AsyncMock(
            return_value=(
                [
                    {
                        "companyName": "Arrow Electronics",
                        "displayName": "John Sales",
                        "emailAddresses": [{"address": "john@arrow.com"}],
                        "businessPhones": ["+1-555-0100"],
                        "mobilePhone": "+1-555-0200",
                    }
                ],
                "new-delta-token",
            )
        )

        with (
            patch("app.utils.graph_client.GraphClient", return_value=gc_mock),
            patch("app.vendor_utils.merge_emails_into_card", return_value=1),
            patch("app.vendor_utils.merge_phones_into_card"),
        ):
            await _sync_user_contacts(user, mock_db)
            mock_db.commit.assert_called()

    @pytest.mark.asyncio
    async def test_sync_delta_expired(self):
        from app.jobs.email_jobs import _sync_user_contacts
        from app.utils.graph_client import GraphSyncStateExpired

        user = MagicMock()
        user.id = 1
        user.email = "buyer@trioscs.com"
        user.access_token = "tok"

        sync_state = MagicMock()
        sync_state.delta_token = "old-token"
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = sync_state
        mock_db.query.return_value.filter.return_value.all.return_value = []

        gc_mock = MagicMock()
        gc_mock.delta_query = AsyncMock(side_effect=GraphSyncStateExpired("expired"))
        gc_mock.get_all_pages = AsyncMock(return_value=[])

        with (
            patch("app.utils.graph_client.GraphClient", return_value=gc_mock),
        ):
            await _sync_user_contacts(user, mock_db)
            assert sync_state.delta_token is None

    @pytest.mark.asyncio
    async def test_sync_general_error(self):
        from app.jobs.email_jobs import _sync_user_contacts

        user = MagicMock()
        user.id = 1
        user.email = "buyer@trioscs.com"
        user.access_token = "tok"
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = None

        gc_mock = MagicMock()
        gc_mock.delta_query = AsyncMock(side_effect=Exception("Network error"))

        with patch("app.utils.graph_client.GraphClient", return_value=gc_mock):
            # Should not raise — error handled gracefully
            await _sync_user_contacts(user, mock_db)

    @pytest.mark.asyncio
    async def test_sync_skips_short_company_names(self):
        from app.jobs.email_jobs import _sync_user_contacts

        user = MagicMock()
        user.id = 1
        user.email = "buyer@trioscs.com"
        user.access_token = "tok"
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = None
        mock_db.query.return_value.filter.return_value.all.return_value = []

        gc_mock = MagicMock()
        gc_mock.delta_query = AsyncMock(
            return_value=(
                [
                    {"companyName": "A", "emailAddresses": [], "businessPhones": []},  # too short
                    {"companyName": "", "emailAddresses": [], "businessPhones": []},  # empty
                ],
                None,
            )
        )

        with patch("app.utils.graph_client.GraphClient", return_value=gc_mock):
            await _sync_user_contacts(user, mock_db)
            mock_db.commit.assert_called()


# ── _job_scan_sent_folders ───────────────────────────────────────────


class TestJobScanSentFolders:
    @pytest.mark.asyncio
    async def test_scan_no_users(self):
        from app.jobs.email_jobs import _job_scan_sent_folders

        with patch("app.jobs.email_jobs.SessionLocal") as MockSL:
            mock_db = MagicMock()
            mock_db.query.return_value.filter.return_value.all.return_value = []
            MockSL.return_value = mock_db
            await _job_scan_sent_folders()

    @pytest.mark.asyncio
    async def test_scan_with_users(self):
        from app.jobs.email_jobs import _job_scan_sent_folders

        user = MagicMock()
        user.id = 1
        user.access_token = "tok"
        user.m365_connected = True

        with (
            patch("app.jobs.email_jobs.SessionLocal") as MockSL,
            patch("app.jobs.email_jobs.scan_sent_folder", new_callable=AsyncMock) as mock_scan,
        ):
            list_db = MagicMock()
            list_db.query.return_value.filter.return_value.all.return_value = [user]
            scan_db = MagicMock()
            scan_db.get.return_value = user
            MockSL.side_effect = [list_db, scan_db]
            await _job_scan_sent_folders()
            mock_scan.assert_called_once()

    @pytest.mark.asyncio
    async def test_scan_timeout(self):
        from app.jobs.email_jobs import _job_scan_sent_folders

        user = MagicMock()
        user.id = 1
        user.access_token = "tok"
        user.m365_connected = True

        with (
            patch("app.jobs.email_jobs.SessionLocal") as MockSL,
            patch("app.jobs.email_jobs.scan_sent_folder", new_callable=AsyncMock, side_effect=asyncio.TimeoutError),
        ):
            list_db = MagicMock()
            list_db.query.return_value.filter.return_value.all.return_value = [user]
            scan_db = MagicMock()
            scan_db.get.return_value = user
            MockSL.side_effect = [list_db, scan_db]
            # Should handle timeout gracefully
            await _job_scan_sent_folders()


# ── scan_sent_folder ─────────────────────────────────────────────────


class TestScanSentFolder:
    @pytest.mark.asyncio
    async def test_scan_no_token(self):
        from app.jobs.email_jobs import scan_sent_folder

        user = MagicMock()
        user.email = "buyer@trioscs.com"
        mock_db = MagicMock()

        with patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value=None):
            result = await scan_sent_folder(user, mock_db)
            assert result == []

    @pytest.mark.asyncio
    async def test_scan_with_messages(self):
        from app.jobs.email_jobs import scan_sent_folder

        user = MagicMock()
        user.id = 1
        user.email = "buyer@trioscs.com"
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = None  # no sync state

        # No existing log
        mock_db.query.return_value.filter.return_value.first.side_effect = [None, None]

        gc_mock = MagicMock()
        gc_mock.delta_query = AsyncMock(
            return_value=(
                [
                    {
                        "id": "msg-001",
                        "subject": "[AVAIL-42] RFQ for LM317T",
                        "sentDateTime": "2026-03-01T10:00:00Z",
                        "toRecipients": [{"emailAddress": {"address": "vendor@arrow.com"}}],
                        "hasAttachments": False,
                    }
                ],
                "new-delta-token",
            )
        )

        with (
            patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="tok"),
            patch("app.utils.graph_client.GraphClient", return_value=gc_mock),
        ):
            result = await scan_sent_folder(user, mock_db)
            assert len(result) == 1

    @pytest.mark.asyncio
    async def test_scan_delta_expired(self):
        from app.jobs.email_jobs import scan_sent_folder
        from app.utils.graph_client import GraphSyncStateExpired

        user = MagicMock()
        user.id = 1
        user.email = "buyer@trioscs.com"
        sync_state = MagicMock()
        sync_state.delta_token = "old"
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = sync_state

        gc_mock = MagicMock()
        gc_mock.delta_query = AsyncMock(side_effect=[GraphSyncStateExpired("expired"), ([], "new-token")])

        with (
            patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="tok"),
            patch("app.utils.graph_client.GraphClient", return_value=gc_mock),
        ):
            result = await scan_sent_folder(user, mock_db)
            assert sync_state.delta_token is None or sync_state.delta_token == "new-token"

    @pytest.mark.asyncio
    async def test_scan_with_attachments(self):
        from app.jobs.email_jobs import scan_sent_folder

        user = MagicMock()
        user.id = 1
        user.email = "buyer@trioscs.com"
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = None

        gc_mock = MagicMock()
        gc_mock.delta_query = AsyncMock(
            return_value=(
                [
                    {
                        "id": "msg-att-001",
                        "subject": "Stock list",
                        "sentDateTime": "2026-03-01T10:00:00Z",
                        "toRecipients": [],
                        "hasAttachments": True,
                    }
                ],
                None,
            )
        )

        with (
            patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="tok"),
            patch("app.utils.graph_client.GraphClient", return_value=gc_mock),
            patch(
                "app.jobs.email_jobs.detect_attachments",
                new_callable=AsyncMock,
                return_value=[{"name": "stock.xlsx", "content_type": "application/xlsx", "size": 1024}],
            ),
        ):
            result = await scan_sent_folder(user, mock_db)
            assert len(result) >= 1


# ── detect_attachments ───────────────────────────────────────────────


class TestDetectAttachments:
    @pytest.mark.asyncio
    async def test_detect_file_attachments(self):
        from app.jobs.email_jobs import detect_attachments

        gc = MagicMock()
        gc.get_json = AsyncMock(
            return_value={
                "value": [
                    {"name": "stock.xlsx", "contentType": "application/xlsx", "size": 1024, "isInline": False},
                    {"name": "logo.png", "contentType": "image/png", "size": 512, "isInline": True},  # inline img
                    {"name": "quote.pdf", "contentType": "application/pdf", "size": 2048, "isInline": False},
                ]
            }
        )
        result = await detect_attachments(gc, "msg-001")
        assert len(result) == 2  # stock.xlsx + quote.pdf (logo.png is inline image)
        assert result[0]["name"] == "stock.xlsx"
        assert result[1]["name"] == "quote.pdf"

    @pytest.mark.asyncio
    async def test_detect_empty(self):
        from app.jobs.email_jobs import detect_attachments

        gc = MagicMock()
        gc.get_json = AsyncMock(return_value={"value": []})
        result = await detect_attachments(gc, "msg-001")
        assert result == []

    @pytest.mark.asyncio
    async def test_detect_error(self):
        from app.jobs.email_jobs import detect_attachments

        gc = MagicMock()
        gc.get_json = AsyncMock(side_effect=Exception("API error"))
        result = await detect_attachments(gc, "msg-001")
        assert result == []

    @pytest.mark.asyncio
    async def test_detect_non_inline_image(self):
        from app.jobs.email_jobs import detect_attachments

        gc = MagicMock()
        gc.get_json = AsyncMock(
            return_value={
                "value": [
                    {"name": "photo.jpg", "contentType": "image/jpeg", "size": 4096, "isInline": False},
                ]
            }
        )
        result = await detect_attachments(gc, "msg-001")
        # Non-inline image IS a real attachment
        assert len(result) == 1
        assert result[0]["name"] == "photo.jpg"


# ── Regex patterns ───────────────────────────────────────────────────


class TestRegexPatterns:
    def test_avail_tag_re(self):
        from app.jobs.email_jobs import _AVAIL_TAG_RE

        m = _AVAIL_TAG_RE.search("Re: [AVAIL-42] RFQ for parts")
        assert m is not None
        assert m.group(1) == "42"

        assert _AVAIL_TAG_RE.search("No tag here") is None

    def test_excess_bid_re(self):
        from app.jobs.email_jobs import _EXCESS_BID_RE

        m = _EXCESS_BID_RE.search("Re: [EXCESS-BID-123] Your bid request")
        assert m is not None
        assert m.group(1) == "123"

        assert _EXCESS_BID_RE.search("No bid tag") is None
