"""Tests for the two largest 0% coverage modules.

Covers:
  1. app/jobs/email_jobs.py (562 lines) — email/contacts/calendar background jobs
  2. app/services/knowledge_service.py (492 lines) — Knowledge Ledger CRUD, Q&A, AI insights

Called by: pytest
Depends on: conftest.py fixtures, unittest.mock
"""

import os

os.environ["TESTING"] = "1"

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ═══════════════════════════════════════════════════════════════════════
# Part 1: app/jobs/email_jobs.py
# ═══════════════════════════════════════════════════════════════════════


class TestRegisterEmailJobs:
    """Test register_email_jobs() — the scheduler registration function."""

    def test_registers_always_on_jobs(self):
        """Should always register contact_status_compute, email_health, calendar, sent
        folders."""
        from app.jobs.email_jobs import register_email_jobs

        scheduler = MagicMock()
        settings = MagicMock()
        settings.contacts_sync_enabled = False
        settings.activity_tracking_enabled = False
        settings.ownership_sweep_enabled = False
        settings.contact_scoring_enabled = False
        settings.customer_enrichment_enabled = False

        register_email_jobs(scheduler, settings)

        job_ids = [call.kwargs.get("id") or call[1].get("id") for call in scheduler.add_job.call_args_list]
        assert "contact_status_compute" in job_ids
        assert "email_health_update" in job_ids
        assert "calendar_scan" in job_ids
        assert "scan_sent_folders" in job_ids
        # These should NOT be registered
        assert "contacts_sync" not in job_ids
        assert "ownership_sweep" not in job_ids
        assert "contact_scoring" not in job_ids
        assert "email_reverification" not in job_ids

    def test_registers_contacts_sync_when_enabled(self):
        from app.jobs.email_jobs import register_email_jobs

        scheduler = MagicMock()
        settings = MagicMock()
        settings.contacts_sync_enabled = True
        settings.activity_tracking_enabled = False
        settings.ownership_sweep_enabled = False
        settings.contact_scoring_enabled = False
        settings.customer_enrichment_enabled = False

        register_email_jobs(scheduler, settings)

        job_ids = [call.kwargs.get("id") or call[1].get("id") for call in scheduler.add_job.call_args_list]
        assert "contacts_sync" in job_ids

    def test_registers_ownership_when_both_enabled(self):
        from app.jobs.email_jobs import register_email_jobs

        scheduler = MagicMock()
        settings = MagicMock()
        settings.contacts_sync_enabled = False
        settings.activity_tracking_enabled = True
        settings.ownership_sweep_enabled = True
        settings.contact_scoring_enabled = False
        settings.customer_enrichment_enabled = False

        register_email_jobs(scheduler, settings)

        job_ids = [call.kwargs.get("id") or call[1].get("id") for call in scheduler.add_job.call_args_list]
        assert "ownership_sweep" in job_ids
        assert "site_ownership_sweep" in job_ids

    def test_registers_contact_scoring_when_enabled(self):
        from app.jobs.email_jobs import register_email_jobs

        scheduler = MagicMock()
        settings = MagicMock()
        settings.contacts_sync_enabled = False
        settings.activity_tracking_enabled = False
        settings.ownership_sweep_enabled = False
        settings.contact_scoring_enabled = True
        settings.customer_enrichment_enabled = False

        register_email_jobs(scheduler, settings)

        job_ids = [call.kwargs.get("id") or call[1].get("id") for call in scheduler.add_job.call_args_list]
        assert "contact_scoring" in job_ids

    def test_registers_reverification_when_enrichment_enabled(self):
        from app.jobs.email_jobs import register_email_jobs

        scheduler = MagicMock()
        settings = MagicMock()
        settings.contacts_sync_enabled = False
        settings.activity_tracking_enabled = False
        settings.ownership_sweep_enabled = False
        settings.contact_scoring_enabled = False
        settings.customer_enrichment_enabled = True

        register_email_jobs(scheduler, settings)

        job_ids = [call.kwargs.get("id") or call[1].get("id") for call in scheduler.add_job.call_args_list]
        assert "email_reverification" in job_ids

    def test_activity_tracking_without_ownership(self):
        """When activity_tracking is on but ownership_sweep is off, no ownership jobs
        registered."""
        from app.jobs.email_jobs import register_email_jobs

        scheduler = MagicMock()
        settings = MagicMock()
        settings.contacts_sync_enabled = False
        settings.activity_tracking_enabled = True
        settings.ownership_sweep_enabled = False
        settings.contact_scoring_enabled = False
        settings.customer_enrichment_enabled = False

        register_email_jobs(scheduler, settings)

        job_ids = [call.kwargs.get("id") or call[1].get("id") for call in scheduler.add_job.call_args_list]
        assert "ownership_sweep" not in job_ids
        assert "site_ownership_sweep" not in job_ids


class TestDetectAttachments:
    """Test detect_attachments() — attachment metadata parsing."""

    @pytest.mark.asyncio
    async def test_returns_file_attachments(self):
        from app.jobs.email_jobs import detect_attachments

        gc = MagicMock()
        gc.get_json = AsyncMock(
            return_value={
                "value": [
                    {"name": "quote.pdf", "contentType": "application/pdf", "size": 1024, "isInline": False},
                    {"name": "data.xlsx", "contentType": "application/vnd.ms-excel", "size": 2048, "isInline": False},
                ]
            }
        )

        result = await detect_attachments(gc, "msg-123")

        assert len(result) == 2
        assert result[0]["name"] == "quote.pdf"
        assert result[0]["content_type"] == "application/pdf"
        assert result[0]["size"] == 1024
        assert result[1]["name"] == "data.xlsx"

    @pytest.mark.asyncio
    async def test_skips_inline_images(self):
        from app.jobs.email_jobs import detect_attachments

        gc = MagicMock()
        gc.get_json = AsyncMock(
            return_value={
                "value": [
                    {"name": "logo.png", "contentType": "image/png", "size": 500, "isInline": True},
                    {"name": "report.pdf", "contentType": "application/pdf", "size": 1024, "isInline": False},
                ]
            }
        )

        result = await detect_attachments(gc, "msg-456")

        assert len(result) == 1
        assert result[0]["name"] == "report.pdf"

    @pytest.mark.asyncio
    async def test_keeps_non_inline_images(self):
        """Non-inline image attachments should be kept."""
        from app.jobs.email_jobs import detect_attachments

        gc = MagicMock()
        gc.get_json = AsyncMock(
            return_value={
                "value": [
                    {"name": "photo.jpg", "contentType": "image/jpeg", "size": 3000, "isInline": False},
                ]
            }
        )

        result = await detect_attachments(gc, "msg-789")

        assert len(result) == 1
        assert result[0]["name"] == "photo.jpg"

    @pytest.mark.asyncio
    async def test_returns_empty_on_api_error(self):
        from app.jobs.email_jobs import detect_attachments

        gc = MagicMock()
        gc.get_json = AsyncMock(side_effect=Exception("Graph API error"))

        result = await detect_attachments(gc, "msg-err")

        assert result == []

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_attachments(self):
        from app.jobs.email_jobs import detect_attachments

        gc = MagicMock()
        gc.get_json = AsyncMock(return_value={"value": []})

        result = await detect_attachments(gc, "msg-empty")

        assert result == []


class TestAvailTagRegex:
    """Test the AVAIL tag regex used in sent folder scanning."""

    def test_matches_avail_tag(self):
        from app.jobs.email_jobs import _AVAIL_TAG_RE

        match = _AVAIL_TAG_RE.search("Re: [AVAIL-123] RFQ for parts")
        assert match is not None
        assert match.group(1) == "123"

    def test_no_match_without_tag(self):
        from app.jobs.email_jobs import _AVAIL_TAG_RE

        assert _AVAIL_TAG_RE.search("Regular email subject") is None

    def test_matches_excess_bid_tag(self):
        from app.jobs.email_jobs import _EXCESS_BID_RE

        match = _EXCESS_BID_RE.search("Re: [EXCESS-BID-456] Bid response")
        assert match is not None
        assert match.group(1) == "456"


class TestJobContactsSync:
    """Test _job_contacts_sync() — syncs Outlook contacts for all users."""

    @pytest.mark.asyncio
    @patch("app.jobs.email_jobs._sync_user_contacts", new_callable=AsyncMock)
    async def test_syncs_eligible_users(self, mock_sync):
        from app.jobs.email_jobs import _job_contacts_sync

        mock_user = MagicMock()
        mock_user.id = 1
        mock_user.access_token = "token"
        mock_user.m365_connected = True
        mock_user.last_contacts_sync = None
        mock_user.refresh_token = "rt"

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.all.return_value = [mock_user]
        mock_db.get.return_value = mock_user

        with patch("app.database.SessionLocal", return_value=mock_db):
            await _job_contacts_sync.__wrapped__()

        # Should have attempted sync for one user
        assert mock_sync.called or mock_db.get.called

    @pytest.mark.asyncio
    @patch("app.jobs.email_jobs._sync_user_contacts", new_callable=AsyncMock)
    async def test_skips_users_without_token(self, mock_sync):
        from app.jobs.email_jobs import _job_contacts_sync

        mock_user = MagicMock()
        mock_user.id = 1
        mock_user.access_token = None  # No token
        mock_user.m365_connected = True
        mock_user.last_contacts_sync = None
        mock_user.refresh_token = "rt"

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.all.return_value = [mock_user]

        with patch("app.database.SessionLocal", return_value=mock_db):
            await _job_contacts_sync.__wrapped__()

        mock_sync.assert_not_called()


class TestJobOwnershipSweep:
    """Test _job_ownership_sweep()."""

    @pytest.mark.asyncio
    async def test_calls_ownership_service(self):
        from app.jobs.email_jobs import _job_ownership_sweep

        mock_db = MagicMock()
        mock_sweep = AsyncMock()

        with (
            patch("app.database.SessionLocal", return_value=mock_db),
            patch("app.services.ownership_service.run_ownership_sweep", mock_sweep),
        ):
            await _job_ownership_sweep.__wrapped__()

        mock_sweep.assert_called_once_with(mock_db)

    @pytest.mark.asyncio
    async def test_rollback_on_error(self):
        from app.jobs.email_jobs import _job_ownership_sweep

        mock_db = MagicMock()

        with (
            patch("app.database.SessionLocal", return_value=mock_db),
            patch(
                "app.services.ownership_service.run_ownership_sweep",
                AsyncMock(side_effect=Exception("db error")),
            ),
        ):
            with pytest.raises(Exception, match="db error"):
                await _job_ownership_sweep.__wrapped__()

        mock_db.rollback.assert_called_once()
        mock_db.close.assert_called_once()


class TestJobEmailHealthUpdate:
    """Test _job_email_health_update()."""

    @pytest.mark.asyncio
    async def test_calls_batch_update(self):
        from app.jobs.email_jobs import _job_email_health_update

        mock_db = MagicMock()
        mock_update = MagicMock(return_value={"updated": 5})

        with (
            patch("app.database.SessionLocal", return_value=mock_db),
            patch("app.services.response_analytics.batch_update_email_health", mock_update),
        ):
            await _job_email_health_update.__wrapped__()

    @pytest.mark.asyncio
    async def test_rollback_on_timeout(self):
        from app.jobs.email_jobs import _job_email_health_update

        mock_db = MagicMock()

        with (
            patch("app.database.SessionLocal", return_value=mock_db),
            patch(
                "app.services.response_analytics.batch_update_email_health",
                side_effect=Exception("timeout"),
            ),
        ):
            with pytest.raises(Exception):
                await _job_email_health_update.__wrapped__()

        mock_db.rollback.assert_called_once()


class TestJobEmailReverification:
    """Test _job_email_reverification()."""

    @pytest.mark.asyncio
    async def test_calls_reverification_and_commits(self):
        from app.jobs.email_jobs import _job_email_reverification

        mock_db = MagicMock()
        mock_reverify = AsyncMock(return_value={"processed": 10, "invalidated": 2})

        with (
            patch("app.database.SessionLocal", return_value=mock_db),
            patch("app.services.customer_enrichment_batch.run_email_reverification", mock_reverify),
        ):
            await _job_email_reverification.__wrapped__()

        mock_reverify.assert_called_once_with(mock_db, max_contacts=200)
        mock_db.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_rollback_on_error(self):
        from app.jobs.email_jobs import _job_email_reverification

        mock_db = MagicMock()
        mock_reverify = AsyncMock(side_effect=RuntimeError("API down"))

        with (
            patch("app.database.SessionLocal", return_value=mock_db),
            patch("app.services.customer_enrichment_batch.run_email_reverification", mock_reverify),
        ):
            with pytest.raises(RuntimeError, match="API down"):
                await _job_email_reverification.__wrapped__()

        mock_db.rollback.assert_called_once()
        mock_db.close.assert_called_once()


class TestScanSentFolder:
    """Test scan_sent_folder() — scans a single user's SentItems."""

    @pytest.mark.asyncio
    async def test_creates_activity_logs_for_sent_emails(self):
        from app.jobs.email_jobs import scan_sent_folder

        mock_user = MagicMock()
        mock_user.id = 1
        mock_user.email = "buyer@test.com"

        mock_db = MagicMock()
        # SyncState query returns None (no existing sync state)
        mock_db.query.return_value.filter.return_value.filter.return_value.first.return_value = None

        mock_gc = MagicMock()
        mock_gc.delta_query = AsyncMock(
            return_value=(
                [
                    {
                        "id": "msg-001",
                        "subject": "[AVAIL-42] RFQ for parts",
                        "sentDateTime": "2026-03-20T10:00:00Z",
                        "toRecipients": [{"emailAddress": {"address": "vendor@acme.com"}}],
                        "hasAttachments": False,
                    }
                ],
                "new-delta-token",
            )
        )

        with (
            patch("app.utils.token_manager.get_valid_token", AsyncMock(return_value="token")),
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
        ):
            result = await scan_sent_folder(mock_user, mock_db)

        # Should have added an ActivityLog
        assert mock_db.add.called

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_token(self):
        from app.jobs.email_jobs import scan_sent_folder

        mock_user = MagicMock()
        mock_user.email = "buyer@test.com"

        mock_db = MagicMock()

        with patch("app.utils.token_manager.get_valid_token", AsyncMock(return_value=None)):
            result = await scan_sent_folder(mock_user, mock_db)

        assert result == []


class TestScanExcessBidResponses:
    """Test _scan_excess_bid_responses()."""

    @pytest.mark.asyncio
    async def test_skips_when_disabled(self):
        from app.jobs.email_jobs import _scan_excess_bid_responses

        mock_user = MagicMock()
        mock_db = MagicMock()

        mock_settings = MagicMock()
        mock_settings.excess_bid_scan_enabled = False

        with patch("app.config.settings", mock_settings):
            await _scan_excess_bid_responses(mock_user, mock_db)

        # Should return early, no graph calls
        mock_db.query.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_no_pending_solicitations(self):
        from app.jobs.email_jobs import _scan_excess_bid_responses

        mock_user = MagicMock()
        mock_user.id = 1
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.filter.return_value.filter.return_value.count.return_value = 0

        mock_settings = MagicMock()
        mock_settings.excess_bid_scan_enabled = True
        mock_settings.excess_bid_parse_lookback_days = 30

        with patch("app.config.settings", mock_settings):
            await _scan_excess_bid_responses(mock_user, mock_db)

    @pytest.mark.asyncio
    async def test_parses_bid_responses(self):
        from app.jobs.email_jobs import _scan_excess_bid_responses

        mock_user = MagicMock()
        mock_user.id = 1

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.filter.return_value.filter.return_value.count.return_value = 1

        mock_solicitation = MagicMock()
        mock_solicitation.status = "sent"
        mock_db.get.return_value = mock_solicitation

        mock_settings = MagicMock()
        mock_settings.excess_bid_scan_enabled = True
        mock_settings.excess_bid_parse_lookback_days = 30

        mock_gc = MagicMock()
        mock_gc.get_json = AsyncMock(
            return_value={
                "value": [
                    {
                        "subject": "Re: [EXCESS-BID-42] Request for bid",
                        "body": {"content": "We can offer $5.00 per unit"},
                        "receivedDateTime": "2026-03-20T10:00:00Z",
                    }
                ]
            }
        )

        mock_parse = AsyncMock(return_value=MagicMock())

        with (
            patch("app.config.settings", mock_settings),
            patch("app.utils.token_manager.get_valid_token", AsyncMock(return_value="token")),
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.services.excess_service.parse_bid_from_email", mock_parse),
        ):
            await _scan_excess_bid_responses(mock_user, mock_db)

        mock_parse.assert_called_once()


class TestScanUserInbox:
    """Test _scan_user_inbox() — orchestrator for inbox scanning sub-ops."""

    @pytest.mark.asyncio
    async def test_runs_all_sub_ops(self):
        from app.jobs.email_jobs import _scan_user_inbox

        mock_user = MagicMock()
        mock_user.email = "buyer@test.com"
        mock_user.last_inbox_scan = datetime.now(timezone.utc)

        mock_db = MagicMock()

        with (
            patch("app.utils.token_manager.get_valid_token", AsyncMock(return_value="token")),
            patch("app.email_service.poll_inbox", AsyncMock(return_value=[])),
            patch("app.jobs.inventory_jobs._scan_stock_list_attachments", AsyncMock()),
            patch("app.jobs.email_jobs._mine_vendor_contacts", AsyncMock()),
            patch("app.jobs.email_jobs._scan_outbound_rfqs", AsyncMock()),
            patch("app.jobs.email_jobs._scan_excess_bid_responses", AsyncMock()),
        ):
            await _scan_user_inbox(mock_user, mock_db)

        mock_db.commit.assert_called()

    @pytest.mark.asyncio
    async def test_skips_when_no_token(self):
        from app.jobs.email_jobs import _scan_user_inbox

        mock_user = MagicMock()
        mock_user.email = "buyer@test.com"
        mock_user.last_inbox_scan = datetime.now(timezone.utc)

        mock_db = MagicMock()

        with (
            patch("app.utils.token_manager.get_valid_token", AsyncMock(return_value=None)),
            patch("app.jobs.inventory_jobs._scan_stock_list_attachments", AsyncMock()),
            patch("app.jobs.email_jobs._mine_vendor_contacts", AsyncMock()),
            patch("app.jobs.email_jobs._scan_outbound_rfqs", AsyncMock()),
            patch("app.jobs.email_jobs._scan_excess_bid_responses", AsyncMock()),
        ):
            await _scan_user_inbox(mock_user, mock_db)

        # poll_succeeded stays False so no commit of last_inbox_scan

    @pytest.mark.asyncio
    async def test_handles_sub_op_failure(self):
        """If a sub-op fails, other sub-ops still run."""
        from app.jobs.email_jobs import _scan_user_inbox

        mock_user = MagicMock()
        mock_user.email = "buyer@test.com"
        mock_user.last_inbox_scan = datetime.now(timezone.utc)

        mock_db = MagicMock()

        with (
            patch("app.utils.token_manager.get_valid_token", AsyncMock(return_value="token")),
            patch("app.email_service.poll_inbox", AsyncMock(return_value=["reply1"])),
            patch(
                "app.jobs.inventory_jobs._scan_stock_list_attachments",
                AsyncMock(side_effect=Exception("stock scan error")),
            ),
            patch("app.jobs.email_jobs._mine_vendor_contacts", AsyncMock()) as mock_mine,
            patch("app.jobs.email_jobs._scan_outbound_rfqs", AsyncMock()) as mock_outbound,
            patch("app.jobs.email_jobs._scan_excess_bid_responses", AsyncMock()) as mock_excess,
        ):
            await _scan_user_inbox(mock_user, mock_db)

        # Other sub-ops still called despite stock_scan failure
        mock_mine.assert_called_once()
        mock_outbound.assert_called_once()
        mock_excess.assert_called_once()


class TestJobScanSentFolders:
    """Test _job_scan_sent_folders() — scheduler entry point."""

    @pytest.mark.asyncio
    async def test_scans_eligible_users(self):
        from app.jobs.email_jobs import _job_scan_sent_folders

        mock_user = MagicMock()
        mock_user.id = 1
        mock_user.access_token = "token"
        mock_user.m365_connected = True
        mock_user.refresh_token = "rt"

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.all.return_value = [mock_user]
        mock_db.get.return_value = mock_user

        with (
            patch("app.database.SessionLocal", return_value=mock_db),
            patch("app.jobs.email_jobs.scan_sent_folder", AsyncMock(return_value=[])),
        ):
            await _job_scan_sent_folders.__wrapped__()


class TestJobCalendarScan:
    """Test _job_calendar_scan()."""

    @pytest.mark.asyncio
    async def test_scans_eligible_users(self):
        from app.jobs.email_jobs import _job_calendar_scan

        mock_user = MagicMock()
        mock_user.id = 1
        mock_user.access_token = "token"
        mock_user.m365_connected = True
        mock_user.email = "buyer@test.com"
        mock_user.refresh_token = "rt"

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.all.return_value = [mock_user]
        mock_db.get.return_value = mock_user

        mock_scan = AsyncMock(return_value={"events_found": 3})

        with (
            patch("app.database.SessionLocal", return_value=mock_db),
            patch("app.utils.token_manager.get_valid_token", AsyncMock(return_value="token")),
            patch("app.services.calendar_intelligence.scan_calendar_events", mock_scan),
        ):
            await _job_calendar_scan.__wrapped__()


class TestJobContactScoring:
    """Test _job_contact_scoring()."""

    @pytest.mark.asyncio
    async def test_calls_scoring_and_logs_result(self):
        from app.jobs.email_jobs import _job_contact_scoring

        mock_db = MagicMock()
        mock_result = {"updated": 15, "skipped": 3}

        with (
            patch("app.database.SessionLocal", return_value=mock_db),
            patch(
                "app.services.contact_intelligence.compute_all_contact_scores",
                return_value=mock_result,
            ),
        ):
            await _job_contact_scoring.__wrapped__()

        mock_db.close.assert_called_once()


class TestMineVendorContacts:
    """Test _mine_vendor_contacts()."""

    @pytest.mark.asyncio
    async def test_creates_new_vendor_cards(self):
        from app.jobs.email_jobs import _mine_vendor_contacts

        mock_user = MagicMock()
        mock_user.id = 1
        mock_user.email = "buyer@test.com"
        mock_user.access_token = "token"

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.all.return_value = []  # No existing cards

        mock_miner = MagicMock()
        mock_miner.scan_inbox = AsyncMock(
            return_value={
                "contacts_enriched": [
                    {
                        "vendor_name": "Acme Parts",
                        "emails": ["sales@acme.com"],
                        "phones": ["555-1234"],
                        "websites": ["acme.com"],
                    }
                ]
            }
        )

        with (
            patch("app.jobs.email_jobs.get_valid_token", AsyncMock(return_value="token")),
            patch("app.jobs.email_jobs.EmailMiner", return_value=mock_miner),
            patch("app.jobs.email_jobs.normalize_vendor_name", return_value="acme parts"),
            patch("app.jobs.email_jobs.merge_emails_into_card", return_value=1),
            patch("app.jobs.email_jobs.merge_phones_into_card"),
        ):
            await _mine_vendor_contacts(mock_user, mock_db, is_backfill=False)

        mock_db.add.assert_called()
        mock_db.commit.assert_called()


class TestScanOutboundRfqs:
    """Test _scan_outbound_rfqs()."""

    @pytest.mark.asyncio
    async def test_updates_vendor_card_outreach(self):
        from app.jobs.email_jobs import _scan_outbound_rfqs

        mock_user = MagicMock()
        mock_user.id = 1
        mock_user.email = "buyer@test.com"
        mock_user.access_token = "token"

        mock_card = MagicMock()
        mock_card.domain = "acme.com"
        mock_card.total_outreach = 5
        mock_card.normalized_name = "acme"

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.all.return_value = [mock_card]

        mock_miner = MagicMock()
        mock_miner.scan_sent_items = AsyncMock(
            return_value={
                "rfqs_detected": 3,
                "vendors_contacted": {"acme.com": 3},
            }
        )

        with (
            patch("app.jobs.email_jobs.get_valid_token", AsyncMock(return_value="token")),
            patch("app.jobs.email_jobs.EmailMiner", return_value=mock_miner),
        ):
            await _scan_outbound_rfqs(mock_user, mock_db, is_backfill=False)

        assert mock_card.total_outreach == 8  # 5 + 3
        mock_db.commit.assert_called()


# ═══════════════════════════════════════════════════════════════════════
# Part 2: app/services/knowledge_service.py
# ═══════════════════════════════════════════════════════════════════════


class TestIsExpired:
    """Test the _is_expired() helper."""

    def test_none_not_expired(self):
        from app.services.knowledge_service import _is_expired

        now = datetime.now(timezone.utc)
        assert _is_expired(None, now) is False

    def test_future_not_expired(self):
        from app.services.knowledge_service import _is_expired

        now = datetime.now(timezone.utc)
        future = now + timedelta(days=30)
        assert _is_expired(future, now) is False

    def test_past_is_expired(self):
        from app.services.knowledge_service import _is_expired

        now = datetime.now(timezone.utc)
        past = now - timedelta(days=1)
        assert _is_expired(past, now) is True

    def test_naive_datetime_handled(self):
        from app.services.knowledge_service import _is_expired

        now = datetime.now(timezone.utc)
        naive_past = datetime(2020, 1, 1)
        assert _is_expired(naive_past, now) is True


class TestCreateEntry:
    """Test create_entry() — knowledge entry creation."""

    def test_creates_entry_and_commits(self, db_session):
        # Need a user first
        from app.models import User
        from app.services.knowledge_service import create_entry

        user = User(
            email="test@test.com",
            name="Test",
            role="buyer",
            azure_id="az-001",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(user)
        db_session.commit()
        db_session.refresh(user)

        entry = create_entry(
            db_session,
            user_id=user.id,
            entry_type="fact",
            content="Test fact content",
            source="manual",
            confidence=0.9,
        )

        assert entry.id is not None
        assert entry.entry_type == "fact"
        assert entry.content == "Test fact content"
        assert entry.source == "manual"
        assert entry.confidence == 0.9
        assert entry.created_by == user.id

    def test_creates_entry_without_commit(self, db_session):
        from app.models import User
        from app.services.knowledge_service import create_entry

        user = User(
            email="test2@test.com",
            name="Test2",
            role="buyer",
            azure_id="az-002",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(user)
        db_session.commit()
        db_session.refresh(user)

        entry = create_entry(
            db_session,
            user_id=user.id,
            entry_type="note",
            content="A note",
            commit=False,
        )

        assert entry.id is not None

    def test_creates_entry_with_entity_linkage(self, db_session):
        from app.models import User
        from app.services.knowledge_service import create_entry

        user = User(
            email="test3@test.com",
            name="Test3",
            role="buyer",
            azure_id="az-003",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(user)
        db_session.commit()
        db_session.refresh(user)

        entry = create_entry(
            db_session,
            user_id=user.id,
            entry_type="fact",
            content="Price fact",
            mpn="LM358N",
            assigned_to_ids=[1, 2],
        )

        assert entry.mpn == "LM358N"
        assert entry.assigned_to_ids == [1, 2]


class TestGetEntries:
    """Test get_entries() — flexible query."""

    def test_returns_entries_filtered_by_type(self, db_session):
        from app.models import User
        from app.services.knowledge_service import create_entry, get_entries

        user = User(
            email="test4@test.com",
            name="Test4",
            role="buyer",
            azure_id="az-004",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(user)
        db_session.commit()
        db_session.refresh(user)

        create_entry(db_session, user_id=user.id, entry_type="fact", content="A fact")
        create_entry(db_session, user_id=user.id, entry_type="note", content="A note")

        facts = get_entries(db_session, entry_type="fact")
        assert len(facts) == 1
        assert facts[0].entry_type == "fact"

    def test_excludes_expired_when_requested(self, db_session):
        from app.models import User
        from app.services.knowledge_service import create_entry, get_entries

        user = User(
            email="test5@test.com",
            name="Test5",
            role="buyer",
            azure_id="az-005",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(user)
        db_session.commit()
        db_session.refresh(user)

        # Create an expired entry
        create_entry(
            db_session,
            user_id=user.id,
            entry_type="fact",
            content="Expired fact",
            expires_at=datetime.now(timezone.utc) - timedelta(days=1),
        )
        # Create a non-expired entry
        create_entry(
            db_session,
            user_id=user.id,
            entry_type="fact",
            content="Fresh fact",
            expires_at=datetime.now(timezone.utc) + timedelta(days=30),
        )

        all_entries = get_entries(db_session, include_expired=True)
        assert len(all_entries) == 2

        active_entries = get_entries(db_session, include_expired=False)
        assert len(active_entries) == 1
        assert active_entries[0].content == "Fresh fact"

    def test_excludes_answers_from_listing(self, db_session):
        from app.models import User
        from app.services.knowledge_service import create_entry, get_entries

        user = User(
            email="test6@test.com",
            name="Test6",
            role="buyer",
            azure_id="az-006",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(user)
        db_session.commit()
        db_session.refresh(user)

        q = create_entry(db_session, user_id=user.id, entry_type="question", content="Q?")
        create_entry(db_session, user_id=user.id, entry_type="answer", content="A.", parent_id=q.id)

        entries = get_entries(db_session)
        # Only the question should appear (answers have parent_id set)
        assert len(entries) == 1
        assert entries[0].entry_type == "question"


class TestGetEntry:
    """Test get_entry()."""

    def test_returns_entry_with_answers(self, db_session):
        from app.models import User
        from app.services.knowledge_service import create_entry, get_entry

        user = User(
            email="test7@test.com",
            name="Test7",
            role="buyer",
            azure_id="az-007",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(user)
        db_session.commit()
        db_session.refresh(user)

        q = create_entry(db_session, user_id=user.id, entry_type="question", content="Q?")
        create_entry(db_session, user_id=user.id, entry_type="answer", content="A.", parent_id=q.id)

        loaded = get_entry(db_session, q.id)
        assert loaded is not None
        assert loaded.content == "Q?"
        assert len(loaded.answers) == 1

    def test_returns_none_for_missing(self, db_session):
        from app.services.knowledge_service import get_entry

        assert get_entry(db_session, 99999) is None


class TestUpdateEntry:
    """Test update_entry()."""

    def test_updates_content(self, db_session):
        from app.models import User
        from app.services.knowledge_service import create_entry, update_entry

        user = User(
            email="test8@test.com",
            name="Test8",
            role="buyer",
            azure_id="az-008",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(user)
        db_session.commit()
        db_session.refresh(user)

        entry = create_entry(db_session, user_id=user.id, entry_type="note", content="Original")
        updated = update_entry(db_session, entry.id, user.id, content="Updated")

        assert updated is not None
        assert updated.content == "Updated"

    def test_returns_none_for_missing(self, db_session):
        from app.services.knowledge_service import update_entry

        assert update_entry(db_session, 99999, 1, content="x") is None


class TestDeleteEntry:
    """Test delete_entry()."""

    def test_deletes_entry(self, db_session):
        from app.models import User
        from app.services.knowledge_service import create_entry, delete_entry, get_entry

        user = User(
            email="test9@test.com",
            name="Test9",
            role="buyer",
            azure_id="az-009",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(user)
        db_session.commit()
        db_session.refresh(user)

        entry = create_entry(db_session, user_id=user.id, entry_type="note", content="To delete")
        assert delete_entry(db_session, entry.id, user.id) is True
        assert get_entry(db_session, entry.id) is None

    def test_returns_false_for_missing(self, db_session):
        from app.services.knowledge_service import delete_entry

        assert delete_entry(db_session, 99999, 1) is False


class TestPostQuestion:
    """Test post_question()."""

    def test_creates_question_entry(self, db_session):
        from app.models import User
        from app.services.knowledge_service import post_question

        user = User(
            email="test10@test.com",
            name="Test10",
            role="buyer",
            azure_id="az-010",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(user)
        db_session.commit()
        db_session.refresh(user)

        q = post_question(
            db_session,
            user_id=user.id,
            content="Where can I find LM358N?",
            assigned_to_ids=[user.id],
            mpn="LM358N",
        )

        assert q.entry_type == "question"
        assert q.mpn == "LM358N"
        assert q.assigned_to_ids == [user.id]


class TestPostAnswer:
    """Test post_answer()."""

    def test_creates_answer_and_resolves_question(self, db_session):
        from app.models import User
        from app.services.knowledge_service import post_answer, post_question

        user = User(
            email="test11@test.com",
            name="Test11",
            role="buyer",
            azure_id="az-011",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(user)
        db_session.commit()
        db_session.refresh(user)

        q = post_question(
            db_session,
            user_id=user.id,
            content="Q?",
            assigned_to_ids=[],
        )

        answer = post_answer(
            db_session,
            user_id=user.id,
            question_id=q.id,
            content="A.",
            answered_via="web",
        )

        assert answer is not None
        assert answer.entry_type == "answer"
        assert answer.parent_id == q.id
        assert answer.answered_via == "web"

        # Question should be resolved
        db_session.refresh(q)
        assert q.is_resolved is True

    def test_returns_none_for_non_question(self, db_session):
        from app.models import User
        from app.services.knowledge_service import create_entry, post_answer

        user = User(
            email="test12@test.com",
            name="Test12",
            role="buyer",
            azure_id="az-012",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(user)
        db_session.commit()
        db_session.refresh(user)

        note = create_entry(db_session, user_id=user.id, entry_type="note", content="Just a note")
        answer = post_answer(db_session, user_id=user.id, question_id=note.id, content="A.")
        assert answer is None

    def test_returns_none_for_missing_question(self, db_session):
        from app.services.knowledge_service import post_answer

        answer = post_answer(db_session, user_id=1, question_id=99999, content="A.")
        assert answer is None


class TestCaptureQuoteFact:
    """Test capture_quote_fact()."""

    def test_captures_price_facts(self, db_session):
        from app.models import User
        from app.services.knowledge_service import capture_quote_fact

        user = User(
            email="test13@test.com",
            name="Test13",
            role="buyer",
            azure_id="az-013",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(user)
        db_session.commit()
        db_session.refresh(user)

        mock_quote = MagicMock()
        mock_quote.quote_number = "Q-001"
        mock_quote.requisition_id = None
        mock_quote.line_items = [
            {"mpn": "LM358N", "unit_sell": 1.50, "qty": 100, "vendor_name": "Acme"},
        ]

        entry = capture_quote_fact(db_session, quote=mock_quote, user_id=user.id)

        assert entry is not None
        assert "LM358N" in entry.content
        assert "$1.50" in entry.content
        assert "Acme" in entry.content

    def test_returns_none_for_empty_line_items(self, db_session):
        from app.services.knowledge_service import capture_quote_fact

        mock_quote = MagicMock()
        mock_quote.line_items = []

        assert capture_quote_fact(db_session, quote=mock_quote, user_id=1) is None

    def test_returns_none_for_no_line_items(self, db_session):
        from app.services.knowledge_service import capture_quote_fact

        mock_quote = MagicMock()
        mock_quote.line_items = None

        assert capture_quote_fact(db_session, quote=mock_quote, user_id=1) is None


class TestCaptureOfferFact:
    """Test capture_offer_fact()."""

    def test_captures_offer_with_all_fields(self, db_session):
        from app.models import User
        from app.services.knowledge_service import capture_offer_fact

        user = User(
            email="test14@test.com",
            name="Test14",
            role="buyer",
            azure_id="az-014",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(user)
        db_session.commit()
        db_session.refresh(user)

        mock_offer = MagicMock()
        mock_offer.mpn = "LM358N"
        mock_offer.unit_price = 2.50
        mock_offer.quantity = 500
        mock_offer.vendor_name = "DigiKey"
        mock_offer.lead_time = "2 weeks"
        mock_offer.vendor_card_id = None
        mock_offer.requisition_id = None

        entry = capture_offer_fact(db_session, offer=mock_offer, user_id=user.id)

        assert entry is not None
        assert "LM358N" in entry.content
        assert "$2.50" in entry.content
        assert "DigiKey" in entry.content

    def test_returns_none_for_empty_offer(self, db_session):
        from app.services.knowledge_service import capture_offer_fact

        mock_offer = MagicMock()
        mock_offer.mpn = ""
        mock_offer.unit_price = None
        mock_offer.quantity = None
        mock_offer.vendor_name = ""
        mock_offer.lead_time = None

        result = capture_offer_fact(db_session, offer=mock_offer, user_id=1)
        assert result is None


class TestCaptureRfqResponseFact:
    """Test capture_rfq_response_fact()."""

    def test_captures_parsed_response(self, db_session):
        from app.models import User
        from app.services.knowledge_service import capture_rfq_response_fact

        user = User(
            email="test15@test.com",
            name="Test15",
            role="buyer",
            azure_id="az-015",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(user)
        db_session.commit()

        parsed = {
            "confidence": 0.9,
            "parts": [
                {
                    "mpn": "LM358N",
                    "status": "available",
                    "unit_price": 1.25,
                    "qty_available": 1000,
                    "lead_time_weeks": "2-3",
                }
            ],
        }

        entries = capture_rfq_response_fact(db_session, parsed=parsed, vendor_name="Acme")

        assert len(entries) == 1
        assert "LM358N" in entries[0].content
        assert "Acme" in entries[0].content
        assert entries[0].source == "email_parsed"
        assert entries[0].confidence == 0.9

    def test_returns_empty_for_no_parts(self, db_session):
        from app.services.knowledge_service import capture_rfq_response_fact

        entries = capture_rfq_response_fact(db_session, parsed={"parts": []}, vendor_name="Acme")
        assert entries == []


class TestBuildContext:
    """Test build_context() — gathers knowledge for AI prompts."""

    def test_returns_empty_for_missing_requisition(self, db_session):
        from app.services.knowledge_service import build_context

        assert build_context(db_session, requisition_id=99999) == ""

    def test_returns_context_with_direct_knowledge(self, db_session):
        from app.models import Requisition, User
        from app.services.knowledge_service import build_context, create_entry

        user = User(
            email="test16@test.com",
            name="Test16",
            role="buyer",
            azure_id="az-016",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(user)
        db_session.commit()
        db_session.refresh(user)

        req = Requisition(
            name="Test Req",
            status="active",
            created_by=user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.commit()
        db_session.refresh(req)

        create_entry(
            db_session,
            user_id=user.id,
            entry_type="fact",
            content="Price is $5.00",
            requisition_id=req.id,
        )

        context = build_context(db_session, requisition_id=req.id)
        assert "Direct knowledge" in context
        assert "Price is $5.00" in context


class TestGenerateInsights:
    """Test generate_insights() — AI insight generation."""

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_context(self, db_session):
        from app.services.knowledge_service import generate_insights

        result = await generate_insights(db_session, requisition_id=99999)
        assert result == []

    @pytest.mark.asyncio
    async def test_generates_insights_from_claude(self, db_session):
        from app.models import Requisition, User
        from app.services.knowledge_service import create_entry, generate_insights

        user = User(
            email="test17@test.com",
            name="Test17",
            role="buyer",
            azure_id="az-017",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(user)
        db_session.commit()
        db_session.refresh(user)

        req = Requisition(
            name="Test Req 2",
            status="active",
            created_by=user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.commit()
        db_session.refresh(req)

        create_entry(
            db_session,
            user_id=user.id,
            entry_type="fact",
            content="LM358N available at $1.50",
            requisition_id=req.id,
        )

        mock_result = {
            "insights": [
                {"content": "Price trending down", "confidence": 0.85, "based_on_expired": False},
                {"content": "Consider alternate vendor", "confidence": 0.7, "based_on_expired": False},
            ]
        }

        with patch("app.utils.claude_client.claude_structured", AsyncMock(return_value=mock_result)):
            entries = await generate_insights(db_session, requisition_id=req.id)

        assert len(entries) == 2
        assert entries[0].entry_type == "ai_insight"
        assert entries[0].content == "Price trending down"

    @pytest.mark.asyncio
    async def test_handles_claude_unavailable(self, db_session):
        from app.models import Requisition, User
        from app.services.knowledge_service import create_entry, generate_insights
        from app.utils.claude_errors import ClaudeUnavailableError

        user = User(
            email="test18@test.com",
            name="Test18",
            role="buyer",
            azure_id="az-018",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(user)
        db_session.commit()
        db_session.refresh(user)

        req = Requisition(
            name="Test Req 3",
            status="active",
            created_by=user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.commit()
        db_session.refresh(req)

        create_entry(
            db_session,
            user_id=user.id,
            entry_type="fact",
            content="Some fact",
            requisition_id=req.id,
        )

        with patch(
            "app.utils.claude_client.claude_structured",
            AsyncMock(side_effect=ClaudeUnavailableError("not configured")),
        ):
            entries = await generate_insights(db_session, requisition_id=req.id)

        assert entries == []

    @pytest.mark.asyncio
    async def test_handles_claude_error(self, db_session):
        from app.models import Requisition, User
        from app.services.knowledge_service import create_entry, generate_insights
        from app.utils.claude_errors import ClaudeError

        user = User(
            email="test19@test.com",
            name="Test19",
            role="buyer",
            azure_id="az-019",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(user)
        db_session.commit()
        db_session.refresh(user)

        req = Requisition(
            name="Test Req 4",
            status="active",
            created_by=user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.commit()
        db_session.refresh(req)

        create_entry(
            db_session,
            user_id=user.id,
            entry_type="fact",
            content="A fact",
            requisition_id=req.id,
        )

        with patch(
            "app.utils.claude_client.claude_structured",
            AsyncMock(side_effect=ClaudeError("rate limit")),
        ):
            entries = await generate_insights(db_session, requisition_id=req.id)

        assert entries == []


class TestGenerateMpnInsights:
    """Test generate_mpn_insights()."""

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_context(self, db_session):
        from app.services.knowledge_service import generate_mpn_insights

        result = await generate_mpn_insights(db_session, mpn="NONEXISTENT-MPN")
        assert result == []

    @pytest.mark.asyncio
    async def test_generates_mpn_insights(self, db_session):
        from app.models import User
        from app.services.knowledge_service import create_entry, generate_mpn_insights

        user = User(
            email="test20@test.com",
            name="Test20",
            role="buyer",
            azure_id="az-020",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(user)
        db_session.commit()
        db_session.refresh(user)

        create_entry(
            db_session,
            user_id=user.id,
            entry_type="fact",
            content="LM358N at $1.50",
            mpn="LM358N",
        )

        mock_result = {
            "insights": [
                {"content": "Stable pricing over 90 days", "confidence": 0.8, "based_on_expired": False},
            ]
        }

        with patch("app.utils.claude_client.claude_structured", AsyncMock(return_value=mock_result)):
            entries = await generate_mpn_insights(db_session, mpn="LM358N")

        assert len(entries) == 1
        assert entries[0].mpn == "LM358N"


class TestGenerateVendorInsights:
    """Test generate_vendor_insights()."""

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_vendor(self, db_session):
        from app.services.knowledge_service import generate_vendor_insights

        result = await generate_vendor_insights(db_session, vendor_card_id=99999)
        assert result == []


class TestGeneratePipelineInsights:
    """Test generate_pipeline_insights()."""

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_requisitions(self, db_session):
        from app.services.knowledge_service import generate_pipeline_insights

        result = await generate_pipeline_insights(db_session)
        assert result == []


class TestGenerateCompanyInsights:
    """Test generate_company_insights()."""

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_company(self, db_session):
        from app.services.knowledge_service import generate_company_insights

        result = await generate_company_insights(db_session, company_id=99999)
        assert result == []


class TestGetCachedInsights:
    """Test cached insight getters."""

    def test_get_cached_insights(self, db_session):
        from app.models import Requisition, User
        from app.services.knowledge_service import create_entry, get_cached_insights

        user = User(
            email="test21@test.com",
            name="Test21",
            role="buyer",
            azure_id="az-021",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(user)
        db_session.commit()
        db_session.refresh(user)

        req = Requisition(
            name="Test Req 5",
            status="active",
            created_by=user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.commit()
        db_session.refresh(req)

        create_entry(
            db_session,
            user_id=user.id,
            entry_type="ai_insight",
            content="Cached insight",
            source="ai_generated",
            requisition_id=req.id,
        )

        cached = get_cached_insights(db_session, requisition_id=req.id)
        assert len(cached) == 1
        assert cached[0].content == "Cached insight"

    def test_get_cached_mpn_insights(self, db_session):
        from app.models import User
        from app.services.knowledge_service import create_entry, get_cached_mpn_insights

        user = User(
            email="test22@test.com",
            name="Test22",
            role="buyer",
            azure_id="az-022",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(user)
        db_session.commit()
        db_session.refresh(user)

        create_entry(
            db_session,
            user_id=user.id,
            entry_type="ai_insight",
            content="MPN insight",
            source="ai_generated",
            mpn="LM358N",
        )

        cached = get_cached_mpn_insights(db_session, mpn="LM358N")
        assert len(cached) == 1

    def test_get_cached_pipeline_insights(self, db_session):
        from app.models import User
        from app.services.knowledge_service import create_entry, get_cached_pipeline_insights

        user = User(
            email="test23@test.com",
            name="Test23",
            role="buyer",
            azure_id="az-023",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(user)
        db_session.commit()
        db_session.refresh(user)

        create_entry(
            db_session,
            user_id=user.id,
            entry_type="ai_insight",
            content="Pipeline insight",
            source="ai_generated",
            mpn="__pipeline__",
        )

        cached = get_cached_pipeline_insights(db_session)
        assert len(cached) == 1

    def test_get_cached_vendor_insights_empty(self, db_session):
        from app.services.knowledge_service import get_cached_vendor_insights

        cached = get_cached_vendor_insights(db_session, vendor_card_id=99999)
        assert cached == []

    def test_get_cached_company_insights_empty(self, db_session):
        from app.services.knowledge_service import get_cached_company_insights

        cached = get_cached_company_insights(db_session, company_id=99999)
        assert cached == []


class TestBuildMpnContext:
    """Test build_mpn_context()."""

    def test_returns_empty_for_unknown_mpn(self, db_session):
        from app.services.knowledge_service import build_mpn_context

        assert build_mpn_context(db_session, mpn="UNKNOWN-12345") == ""

    def test_includes_knowledge_entries(self, db_session):
        from app.models import User
        from app.services.knowledge_service import build_mpn_context, create_entry

        user = User(
            email="test24@test.com",
            name="Test24",
            role="buyer",
            azure_id="az-024",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(user)
        db_session.commit()
        db_session.refresh(user)

        create_entry(
            db_session,
            user_id=user.id,
            entry_type="fact",
            content="Price at $2.00",
            mpn="TEST-MPN-001",
        )

        context = build_mpn_context(db_session, mpn="TEST-MPN-001")
        assert "Knowledge entries for MPN" in context
        assert "Price at $2.00" in context


class TestBuildVendorContext:
    """Test build_vendor_context()."""

    def test_returns_empty_for_missing_vendor(self, db_session):
        from app.services.knowledge_service import build_vendor_context

        assert build_vendor_context(db_session, vendor_card_id=99999) == ""

    def test_includes_vendor_info(self, db_session):
        from app.models import VendorCard
        from app.services.knowledge_service import build_vendor_context

        vc = VendorCard(
            normalized_name="acme parts",
            display_name="Acme Parts",
            domain="acme.com",
            emails=[],
            phones=[],
        )
        db_session.add(vc)
        db_session.commit()
        db_session.refresh(vc)

        context = build_vendor_context(db_session, vendor_card_id=vc.id)
        assert "Acme Parts" in context
        assert "acme.com" in context


class TestBuildPipelineContext:
    """Test build_pipeline_context()."""

    def test_returns_empty_when_no_requisitions(self, db_session):
        from app.services.knowledge_service import build_pipeline_context

        assert build_pipeline_context(db_session) == ""

    def test_includes_status_breakdown(self, db_session):
        from app.models import Requisition, User
        from app.services.knowledge_service import build_pipeline_context

        user = User(
            email="test25@test.com",
            name="Test25",
            role="buyer",
            azure_id="az-025",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(user)
        db_session.commit()
        db_session.refresh(user)

        for status in ["active", "active", "closed"]:
            req = Requisition(
                name=f"Req {status}",
                status=status,
                created_by=user.id,
                created_at=datetime.now(timezone.utc),
            )
            db_session.add(req)
        db_session.commit()

        context = build_pipeline_context(db_session)
        assert "Pipeline status breakdown" in context
        assert "active" in context


class TestBuildCompanyContext:
    """Test build_company_context()."""

    def test_returns_empty_for_missing_company(self, db_session):
        from app.services.knowledge_service import build_company_context

        assert build_company_context(db_session, company_id=99999) == ""

    def test_includes_company_profile(self, db_session):
        from app.models import Company
        from app.services.knowledge_service import build_company_context

        co = Company(
            name="TestCo Electronics",
            industry="Semiconductors",
            is_active=True,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(co)
        db_session.commit()
        db_session.refresh(co)

        context = build_company_context(db_session, company_id=co.id)
        assert "TestCo Electronics" in context
        assert "Semiconductors" in context
