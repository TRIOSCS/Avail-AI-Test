"""Tests for app/jobs/email_jobs.py — Email, contacts, and calendar background jobs.

Covers: register_email_jobs, ownership sweeps, calendar scan (parked),
inbox scanning helpers, contact mining, outbound RFQ scanning,
sent folder scanning, attachment detection.

Called by: pytest
Depends on: conftest fixtures (db_session, test_user)
"""

import asyncio
from datetime import UTC, datetime, timedelta
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
        created_at=datetime.now(UTC),
        **kw,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


# ── register_email_jobs ──────────────────────────────────────────────


def _make_settings(
    *,
    contacts_sync=False,
    activity_tracking=False,
    ownership_sweep=False,
    contact_scoring=False,
    customer_enrichment=False,
) -> MagicMock:
    settings = MagicMock()
    settings.contacts_sync_enabled = contacts_sync
    settings.activity_tracking_enabled = activity_tracking
    settings.ownership_sweep_enabled = ownership_sweep
    settings.contact_scoring_enabled = contact_scoring
    settings.customer_enrichment_enabled = customer_enrichment
    return settings


class TestRegisterEmailJobs:
    def test_register_all_enabled(self):
        from app.jobs.email_jobs import register_email_jobs

        scheduler = MagicMock()
        settings = _make_settings(
            contacts_sync=True,
            activity_tracking=True,
            ownership_sweep=True,
            contact_scoring=True,
            customer_enrichment=True,
        )
        register_email_jobs(scheduler, settings)
        # W1 disposition: only ownership_sweep, site_ownership_sweep, and
        # scan_sent_folders remain registrable; deleted/parked jobs never register.
        job_ids = [call.kwargs.get("id") for call in scheduler.add_job.call_args_list]
        assert sorted(job_ids) == ["ownership_sweep", "scan_sent_folders", "site_ownership_sweep"]

    def test_register_minimal(self):
        from app.jobs.email_jobs import register_email_jobs

        scheduler = MagicMock()
        settings = _make_settings()
        register_email_jobs(scheduler, settings)
        # Always registered: scan_sent_folders only (kernel job).
        # calendar_scan is parked (W1) — never registered.
        job_ids = [call.kwargs.get("id") for call in scheduler.add_job.call_args_list]
        assert job_ids == ["scan_sent_folders"]

    def test_register_activity_without_ownership(self):
        from app.jobs.email_jobs import register_email_jobs

        scheduler = MagicMock()
        settings = _make_settings(activity_tracking=True)
        register_email_jobs(scheduler, settings)
        # No ownership_sweep or site_ownership_sweep, but logs info
        job_ids = [call.kwargs.get("id") or call.args[2] for call in scheduler.add_job.call_args_list]
        assert "ownership_sweep" not in job_ids


# ── _job_ownership_sweep ─────────────────────────────────────────────


class TestJobOwnershipSweep:
    @pytest.mark.asyncio
    async def test_ownership_sweep(self):
        from app.jobs.email_jobs import _job_ownership_sweep

        with (
            patch("app.database.SessionLocal") as MockSL,
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
            patch("app.database.SessionLocal") as MockSL,
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
            patch("app.database.SessionLocal") as MockSL,
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
            patch("app.database.SessionLocal") as MockSL,
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


# ── _job_calendar_scan ───────────────────────────────────────────────


class TestJobCalendarScan:
    @pytest.mark.asyncio
    async def test_calendar_scan_no_users(self):
        from app.jobs.email_jobs import _job_calendar_scan

        with patch("app.database.SessionLocal") as MockSL:
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
            patch("app.database.SessionLocal") as MockSL,
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


# ── _scan_user_inbox ─────────────────────────────────────────────────


class TestScanUserInbox:
    @pytest.mark.asyncio
    async def test_scan_inbox_basic(self):
        from app.jobs.email_jobs import _scan_user_inbox

        user = MagicMock()
        user.email = "buyer@trioscs.com"
        user.last_inbox_scan = datetime.now(UTC) - timedelta(hours=1)
        user.access_token = "tok"
        mock_db = MagicMock()

        with (
            patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="valid-tok"),
            patch("app.email_service.poll_inbox", new_callable=AsyncMock, return_value=[{"id": 1}]),
            patch("app.jobs.inventory_jobs._scan_stock_list_attachments", new_callable=AsyncMock),
            patch("app.jobs.email_jobs._mine_vendor_contacts", new_callable=AsyncMock),
            patch("app.jobs.email_jobs._scan_outbound_rfqs", new_callable=AsyncMock),
        ):
            await _scan_user_inbox(user, mock_db)
            mock_db.commit.assert_called()

    @pytest.mark.asyncio
    async def test_scan_inbox_no_token(self):
        from app.jobs.email_jobs import _scan_user_inbox

        user = MagicMock()
        user.email = "buyer@trioscs.com"
        user.last_inbox_scan = datetime.now(UTC)
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
        user.last_inbox_scan = datetime.now(UTC)
        mock_db = MagicMock()

        with (
            patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="tok"),
            patch("app.email_service.poll_inbox", new_callable=AsyncMock, side_effect=Exception("Graph error")),
            patch("app.jobs.inventory_jobs._scan_stock_list_attachments", new_callable=AsyncMock),
            patch("app.jobs.email_jobs._mine_vendor_contacts", new_callable=AsyncMock),
            patch("app.jobs.email_jobs._scan_outbound_rfqs", new_callable=AsyncMock),
        ):
            await _scan_user_inbox(user, mock_db)
            # poll failed, so last_inbox_scan should NOT update
            mock_db.commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_scan_inbox_sub_op_failure(self):
        from app.jobs.email_jobs import _scan_user_inbox

        user = MagicMock()
        user.email = "buyer@trioscs.com"
        user.last_inbox_scan = datetime.now(UTC)
        mock_db = MagicMock()

        with (
            patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="tok"),
            patch("app.email_service.poll_inbox", new_callable=AsyncMock, return_value=[]),
            patch(
                "app.jobs.inventory_jobs._scan_stock_list_attachments",
                new_callable=AsyncMock,
                side_effect=Exception("stock err"),
            ),
            patch("app.jobs.email_jobs._mine_vendor_contacts", new_callable=AsyncMock),
            patch("app.jobs.email_jobs._scan_outbound_rfqs", new_callable=AsyncMock),
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
            patch("app.jobs.inventory_jobs._scan_stock_list_attachments", new_callable=AsyncMock) as mock_stock,
            patch("app.jobs.email_jobs._mine_vendor_contacts", new_callable=AsyncMock) as mock_mine,
            patch("app.jobs.email_jobs._scan_outbound_rfqs", new_callable=AsyncMock) as mock_rfq,
        ):
            await _scan_user_inbox(user, mock_db)
            # Verify is_backfill=True passed to sub-ops
            mock_stock.assert_called_once()
            args = mock_stock.call_args
            assert args[0][2] is True  # is_backfill


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


# ── _job_scan_sent_folders ───────────────────────────────────────────


class TestJobScanSentFolders:
    @pytest.mark.asyncio
    async def test_scan_no_users(self):
        from app.jobs.email_jobs import _job_scan_sent_folders

        with patch("app.database.SessionLocal") as MockSL:
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
            patch("app.database.SessionLocal") as MockSL,
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
            patch("app.database.SessionLocal") as MockSL,
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
        # Requisition existence filter: token 42 is live
        mock_db.query.return_value.filter.return_value.all.return_value = [(42,)]

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
    async def test_scan_initial_sync_bounded_by_backfill_lookback(self):
        """The initial SentItems delta round (and the post-410 resync) must be bounded
        to the backfill window — the resumable-nextLink contract would otherwise drain
        the entire folder history across runs."""
        from app.config import settings
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
            await scan_sent_folder(user, mock_db)

        for call in gc_mock.delta_query.call_args_list:
            assert call.kwargs["initial_lookback_days"] == settings.inbox_backfill_days

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

    @pytest.mark.asyncio
    async def test_scan_multi_token_subject_attributes_all_requisitions(self):
        """A sent message tagged with TWO [ref:] tokens (cross-requisition RFQ) creates
        one ActivityLog per token requisition."""
        from app.jobs.email_jobs import scan_sent_folder

        user = MagicMock()
        user.id = 1
        user.email = "buyer@trioscs.com"
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = None
        # Requisition existence filter: both tokens are live
        mock_db.query.return_value.filter.return_value.all.return_value = [(12,), (34,)]

        gc_mock = MagicMock()
        gc_mock.delta_query = AsyncMock(
            return_value=(
                [
                    {
                        "id": "msg-multi-001",
                        "subject": "RFQ — 2 parts [ref:12] [ref:34]",
                        "sentDateTime": "2026-06-01T10:00:00Z",
                        "toRecipients": [{"emailAddress": {"address": "vendor@arrow.com"}}],
                        "hasAttachments": False,
                    }
                ],
                None,
            )
        )

        with (
            patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="tok"),
            patch("app.utils.graph_client.GraphClient", return_value=gc_mock),
        ):
            result = await scan_sent_folder(user, mock_db)

        assert len(result) == 2
        assert sorted(log.requisition_id for log in result) == [12, 34]
        # Both rows are the SAME message — shared external_id, same recipient
        assert {log.external_id for log in result} == {"msg-multi-001"}
        assert {log.contact_email for log in result} == {"vendor@arrow.com"}

    @pytest.mark.asyncio
    async def test_scan_stale_token_not_attributed(self, db_session, test_user, test_requisition):
        """F5: a [ref:] token pointing at a deleted requisition must not be written
        to ActivityLog.requisition_id (FK violation on PG → whole batch + delta
        token rolled back). The stale-token message is logged UNLINKED; the live
        token still attributes."""
        from app.jobs.email_jobs import scan_sent_folder
        from app.models import ActivityLog

        gc_mock = MagicMock()
        gc_mock.delta_query = AsyncMock(
            return_value=(
                [
                    {
                        "id": "msg-live-1",
                        "subject": f"RFQ [ref:{test_requisition.id}]",
                        "sentDateTime": "2026-06-01T10:00:00Z",
                        "toRecipients": [{"emailAddress": {"address": "vendor@arrow.com"}}],
                        "hasAttachments": False,
                    },
                    {
                        "id": "msg-stale-1",
                        "subject": "RFQ [ref:99999]",
                        "sentDateTime": "2026-06-01T10:01:00Z",
                        "toRecipients": [{"emailAddress": {"address": "vendor@avnet.com"}}],
                        "hasAttachments": False,
                    },
                ],
                "delta-after-stale",
            )
        )

        with (
            patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="tok"),
            patch("app.utils.graph_client.GraphClient", return_value=gc_mock),
        ):
            result = await scan_sent_folder(test_user, db_session)

        assert len(result) == 2
        by_msg = {log.external_id: log for log in result}
        assert by_msg["msg-live-1"].requisition_id == test_requisition.id
        assert by_msg["msg-stale-1"].requisition_id is None  # stale token dropped, row kept
        # Everything committed — no FK crash rolled the batch back.
        assert db_session.query(ActivityLog).filter(ActivityLog.external_id == "msg-stale-1").count() == 1

    @pytest.mark.asyncio
    async def test_scan_one_bad_message_does_not_poison_batch(self, db_session, test_user, test_requisition):
        """F5: per-message savepoints — one malformed message is skipped (logged),
        the rest of the batch and the delta token survive."""
        from app.jobs.email_jobs import scan_sent_folder
        from app.models import SyncState

        gc_mock = MagicMock()
        gc_mock.delta_query = AsyncMock(
            return_value=(
                [
                    {
                        "id": "msg-bad-1",
                        "subject": "RFQ broken",
                        "sentDateTime": "2026-06-01T10:00:00Z",
                        "toRecipients": [None],  # malformed Graph payload → raises in-loop
                        "hasAttachments": False,
                    },
                    {
                        "id": "msg-good-1",
                        "subject": f"RFQ [ref:{test_requisition.id}]",
                        "sentDateTime": "2026-06-01T10:02:00Z",
                        "toRecipients": [{"emailAddress": {"address": "vendor@arrow.com"}}],
                        "hasAttachments": False,
                    },
                ],
                "delta-after-bad",
            )
        )

        with (
            patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="tok"),
            patch("app.utils.graph_client.GraphClient", return_value=gc_mock),
        ):
            result = await scan_sent_folder(test_user, db_session)

        assert [log.external_id for log in result] == ["msg-good-1"]
        assert result[0].requisition_id == test_requisition.id
        sync = (
            db_session.query(SyncState)
            .filter(SyncState.user_id == test_user.id, SyncState.folder == "sent_items_scan")
            .one()
        )
        assert sync.delta_token == "delta-after-bad"


# ── detect_attachments ───────────────────────────────────────────────


class TestDetectAttachments:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("value", "expected_names"),
        [
            pytest.param(
                [
                    {"name": "stock.xlsx", "contentType": "application/xlsx", "size": 1024, "isInline": False},
                    {"name": "logo.png", "contentType": "image/png", "size": 512, "isInline": True},  # inline img
                    {"name": "quote.pdf", "contentType": "application/pdf", "size": 2048, "isInline": False},
                ],
                ["stock.xlsx", "quote.pdf"],  # logo.png is inline image, dropped
                id="file_attachments",
            ),
            pytest.param([], [], id="empty"),
            pytest.param(
                [{"name": "photo.jpg", "contentType": "image/jpeg", "size": 4096, "isInline": False}],
                ["photo.jpg"],  # non-inline image IS a real attachment
                id="non_inline_image",
            ),
        ],
    )
    async def test_detect_attachments(self, value, expected_names):
        from app.jobs.email_jobs import detect_attachments

        gc = MagicMock()
        gc.get_json = AsyncMock(return_value={"value": value})
        result = await detect_attachments(gc, "msg-001")
        assert [att["name"] for att in result] == expected_names

    @pytest.mark.asyncio
    async def test_detect_error(self):
        from app.jobs.email_jobs import detect_attachments

        gc = MagicMock()
        gc.get_json = AsyncMock(side_effect=Exception("API error"))
        result = await detect_attachments(gc, "msg-001")
        assert result == []


# ── Regex patterns ───────────────────────────────────────────────────


class TestRegexPatterns:
    def test_avail_tag_re(self):
        from app.shared_constants import RFQ_SUBJECT_TAG_RE

        m = RFQ_SUBJECT_TAG_RE.search("Re: [AVAIL-42] RFQ for parts")
        assert m is not None
        assert m.group(1) == "42"

        assert RFQ_SUBJECT_TAG_RE.search("No tag here") is None
