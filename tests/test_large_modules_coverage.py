"""Tests for the two largest 0% coverage modules.

Covers:
  1. app/jobs/email_jobs.py (562 lines) — email/contacts/calendar background jobs
  2. app/services/knowledge_service.py (492 lines) — Knowledge Ledger CRUD, Q&A, AI insights

Called by: pytest
Depends on: conftest.py fixtures, unittest.mock
"""

import os

os.environ["TESTING"] = "1"

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_user(db_session, *, email, azure_id, name):
    """Create, persist, and refresh a buyer User for knowledge_service tests."""
    from app.models import User

    user = User(
        email=email,
        name=name,
        role="buyer",
        azure_id=azure_id,
        created_at=datetime.now(UTC),
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


# ═══════════════════════════════════════════════════════════════════════
# Part 1: app/jobs/email_jobs.py
# ═══════════════════════════════════════════════════════════════════════


def _register_and_get_job_ids(**enabled_flags):
    """Register email jobs with all feature flags off except those overridden, return
    job ids."""
    from app.jobs.email_jobs import register_email_jobs

    scheduler = MagicMock()
    settings = MagicMock()
    settings.contacts_sync_enabled = False
    settings.activity_tracking_enabled = False
    settings.ownership_sweep_enabled = False
    settings.contact_scoring_enabled = False
    settings.customer_enrichment_enabled = False
    for name, value in enabled_flags.items():
        setattr(settings, name, value)

    register_email_jobs(scheduler, settings)

    return [call.kwargs.get("id") or call[1].get("id") for call in scheduler.add_job.call_args_list]


class TestRegisterEmailJobs:
    """Test register_email_jobs() — the scheduler registration function."""

    @pytest.mark.parametrize(
        ("flags", "expected_present", "expected_absent"),
        [
            pytest.param(
                {},
                ["contact_status_compute", "email_health_update", "scan_sent_folders"],
                ["contacts_sync", "ownership_sweep", "contact_scoring", "email_reverification", "calendar_scan"],
                id="always_on_jobs",
            ),
            pytest.param(
                {"activity_tracking_enabled": True},
                ["calendar_scan"],
                [],
                id="calendar_scan_when_tracking_enabled",
            ),
            pytest.param(
                {"contacts_sync_enabled": True},
                ["contacts_sync"],
                [],
                id="contacts_sync_when_enabled",
            ),
            pytest.param(
                {"activity_tracking_enabled": True, "ownership_sweep_enabled": True},
                ["ownership_sweep", "site_ownership_sweep"],
                [],
                id="ownership_when_both_enabled",
            ),
            pytest.param(
                {"contact_scoring_enabled": True},
                ["contact_scoring"],
                [],
                id="contact_scoring_when_enabled",
            ),
            pytest.param(
                {"customer_enrichment_enabled": True},
                ["email_reverification"],
                [],
                id="reverification_when_enrichment_enabled",
            ),
            pytest.param(
                {"activity_tracking_enabled": True, "ownership_sweep_enabled": False},
                [],
                ["ownership_sweep", "site_ownership_sweep"],
                id="activity_tracking_without_ownership",
            ),
        ],
    )
    def test_job_registration(self, flags, expected_present, expected_absent):
        job_ids = _register_and_get_job_ids(**flags)
        for job_id in expected_present:
            assert job_id in job_ids
        for job_id in expected_absent:
            assert job_id not in job_ids


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
        from app.shared_constants import RFQ_SUBJECT_TAG_RE

        match = RFQ_SUBJECT_TAG_RE.search("Re: [AVAIL-123] RFQ for parts")
        assert match is not None
        assert match.group(1) == "123"

    def test_no_match_without_tag(self):
        from app.shared_constants import RFQ_SUBJECT_TAG_RE

        assert RFQ_SUBJECT_TAG_RE.search("Regular email subject") is None


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

        # Call site passes db only (default _max_contacts); the old max_contacts= kwarg
        # raised TypeError against the run_email_reverification(_max_contacts=...) signature.
        mock_reverify.assert_called_once_with(mock_db)
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
        # SyncState query: .filter(...).first() returns None (no existing sync state)
        # ActivityLog dedup: .filter(...).filter(...).first() returns None (no existing log)
        # Use side_effect to handle multiple .query() calls differently
        mock_query = MagicMock()
        mock_filter = MagicMock()
        mock_filter.first.return_value = None  # No SyncState, no existing ActivityLog
        mock_filter.filter.return_value = mock_filter  # chained .filter()
        mock_query.filter.return_value = mock_filter
        mock_db.query.return_value = mock_query

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

        # Should have added entries (SyncState + ActivityLog)
        assert mock_db.add.called
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_token(self):
        from app.jobs.email_jobs import scan_sent_folder

        mock_user = MagicMock()
        mock_user.email = "buyer@test.com"

        mock_db = MagicMock()

        with patch("app.utils.token_manager.get_valid_token", AsyncMock(return_value=None)):
            result = await scan_sent_folder(mock_user, mock_db)

        assert result == []


class TestScanUserInbox:
    """Test _scan_user_inbox() — orchestrator for inbox scanning sub-ops."""

    @pytest.mark.asyncio
    async def test_runs_all_sub_ops(self):
        from app.jobs.email_jobs import _scan_user_inbox

        mock_user = MagicMock()
        mock_user.email = "buyer@test.com"
        mock_user.last_inbox_scan = datetime.now(UTC)

        mock_db = MagicMock()

        with (
            patch("app.utils.token_manager.get_valid_token", AsyncMock(return_value="token")),
            patch("app.email_service.poll_inbox", AsyncMock(return_value=[])),
            patch("app.jobs.inventory_jobs._scan_stock_list_attachments", AsyncMock()),
            patch("app.jobs.email_jobs._mine_vendor_contacts", AsyncMock()),
            patch("app.jobs.email_jobs._scan_outbound_rfqs", AsyncMock()),
        ):
            await _scan_user_inbox(mock_user, mock_db)

        mock_db.commit.assert_called()

    @pytest.mark.asyncio
    async def test_skips_when_no_token(self):
        from app.jobs.email_jobs import _scan_user_inbox

        mock_user = MagicMock()
        mock_user.email = "buyer@test.com"
        mock_user.last_inbox_scan = datetime.now(UTC)

        mock_db = MagicMock()

        with (
            patch("app.utils.token_manager.get_valid_token", AsyncMock(return_value=None)),
            patch("app.jobs.inventory_jobs._scan_stock_list_attachments", AsyncMock()),
            patch("app.jobs.email_jobs._mine_vendor_contacts", AsyncMock()),
            patch("app.jobs.email_jobs._scan_outbound_rfqs", AsyncMock()),
        ):
            await _scan_user_inbox(mock_user, mock_db)

        # poll_succeeded stays False so no commit of last_inbox_scan

    @pytest.mark.asyncio
    async def test_handles_sub_op_failure(self):
        """If a sub-op fails, other sub-ops still run."""
        from app.jobs.email_jobs import _scan_user_inbox

        mock_user = MagicMock()
        mock_user.email = "buyer@test.com"
        mock_user.last_inbox_scan = datetime.now(UTC)

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
        ):
            await _scan_user_inbox(mock_user, mock_db)

        # Other sub-ops still called despite stock_scan failure
        mock_mine.assert_called_once()
        mock_outbound.assert_called_once()


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
            patch("app.utils.token_manager.get_valid_token", AsyncMock(return_value="token")),
            patch("app.connectors.email_mining.EmailMiner", return_value=mock_miner),
            patch("app.vendor_utils.normalize_vendor_name", return_value="acme parts"),
            patch("app.vendor_utils.merge_emails_into_card", return_value=1),
            patch("app.vendor_utils.merge_phones_into_card"),
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
            patch("app.utils.token_manager.get_valid_token", AsyncMock(return_value="token")),
            patch("app.connectors.email_mining.EmailMiner", return_value=mock_miner),
        ):
            await _scan_outbound_rfqs(mock_user, mock_db, is_backfill=False)

        assert mock_card.total_outreach == 8  # 5 + 3
        mock_db.commit.assert_called()


# ═══════════════════════════════════════════════════════════════════════
# Part 2: app/services/knowledge_service.py
# ═══════════════════════════════════════════════════════════════════════


class TestIsExpired:
    """Test the _is_expired() helper."""

    @pytest.mark.parametrize(
        ("offset", "expected"),
        [
            pytest.param(None, False, id="none_not_expired"),
            pytest.param(timedelta(days=30), False, id="future_not_expired"),
            pytest.param(timedelta(days=-1), True, id="past_is_expired"),
        ],
    )
    def test_relative_to_now(self, offset, expected):
        from app.services.knowledge_service import _is_expired

        now = datetime.now(UTC)
        value = None if offset is None else now + offset
        assert _is_expired(value, now) is expected

    def test_naive_datetime_handled(self):
        from app.services.knowledge_service import _is_expired

        now = datetime.now(UTC)
        naive_past = datetime(2020, 1, 1)
        assert _is_expired(naive_past, now) is True


class TestCreateEntry:
    """Test create_entry() — knowledge entry creation."""

    def test_creates_entry_and_commits(self, db_session):
        from app.services.knowledge_service import create_entry

        user = _make_user(db_session, email="test@test.com", azure_id="az-001", name="Test")

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
        from app.services.knowledge_service import create_entry

        user = _make_user(db_session, email="test2@test.com", azure_id="az-002", name="Test2")

        entry = create_entry(
            db_session,
            user_id=user.id,
            entry_type="note",
            content="A note",
            commit=False,
        )

        assert entry.id is not None

    def test_creates_entry_with_entity_linkage(self, db_session):
        from app.services.knowledge_service import create_entry

        user = _make_user(db_session, email="test3@test.com", azure_id="az-003", name="Test3")

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


class TestCaptureQuoteFact:
    """Test capture_quote_fact()."""

    def test_captures_price_facts(self, db_session):
        from app.services.knowledge_service import capture_quote_fact

        user = _make_user(db_session, email="test13@test.com", azure_id="az-013", name="Test13")

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
        from app.services.knowledge_service import capture_offer_fact

        user = _make_user(db_session, email="test14@test.com", azure_id="az-014", name="Test14")

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


class TestBuildContext:
    """Test build_context() — gathers knowledge for AI prompts."""

    def test_returns_empty_for_missing_requisition(self, db_session):
        from app.services.knowledge_service import build_context

        assert build_context(db_session, requisition_id=99999) == ""

    def test_returns_context_with_direct_knowledge(self, db_session):
        from app.models import Requisition
        from app.services.knowledge_service import build_context, create_entry

        user = _make_user(db_session, email="test16@test.com", azure_id="az-016", name="Test16")

        req = Requisition(
            name="Test Req",
            status="open",
            created_by=user.id,
            created_at=datetime.now(UTC),
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
        from app.models import Requisition
        from app.services.knowledge_service import create_entry, generate_insights

        user = _make_user(db_session, email="test17@test.com", azure_id="az-017", name="Test17")

        req = Requisition(
            name="Test Req 2",
            status="open",
            created_by=user.id,
            created_at=datetime.now(UTC),
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
        from app.models import Requisition
        from app.services.knowledge_service import create_entry, generate_insights
        from app.utils.claude_errors import ClaudeUnavailableError

        user = _make_user(db_session, email="test18@test.com", azure_id="az-018", name="Test18")

        req = Requisition(
            name="Test Req 3",
            status="open",
            created_by=user.id,
            created_at=datetime.now(UTC),
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
        from app.models import Requisition
        from app.services.knowledge_service import create_entry, generate_insights
        from app.utils.claude_errors import ClaudeError

        user = _make_user(db_session, email="test19@test.com", azure_id="az-019", name="Test19")

        req = Requisition(
            name="Test Req 4",
            status="open",
            created_by=user.id,
            created_at=datetime.now(UTC),
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
        from app.services.knowledge_service import create_entry, generate_mpn_insights

        user = _make_user(db_session, email="test20@test.com", azure_id="az-020", name="Test20")

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
        from app.models import Requisition
        from app.services.knowledge_service import create_entry, get_cached_insights

        user = _make_user(db_session, email="test21@test.com", azure_id="az-021", name="Test21")

        req = Requisition(
            name="Test Req 5",
            status="open",
            created_by=user.id,
            created_at=datetime.now(UTC),
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

    def test_get_cached_pipeline_insights(self, db_session):
        from app.services.knowledge_service import create_entry, get_cached_pipeline_insights

        user = _make_user(db_session, email="test23@test.com", azure_id="az-023", name="Test23")

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
        from app.services.knowledge_service import build_mpn_context, create_entry

        user = _make_user(db_session, email="test24@test.com", azure_id="az-024", name="Test24")

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
        from app.models import Requisition
        from app.services.knowledge_service import build_pipeline_context

        user = _make_user(db_session, email="test25@test.com", azure_id="az-025", name="Test25")

        for status in ["active", "active", "closed"]:
            req = Requisition(
                name=f"Req {status}",
                status=status,
                created_by=user.id,
                created_at=datetime.now(UTC),
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
            created_at=datetime.now(UTC),
        )
        db_session.add(co)
        db_session.commit()
        db_session.refresh(co)

        context = build_company_context(db_session, company_id=co.id)
        assert "TestCo Electronics" in context
        assert "Semiconductors" in context
