"""test_jobs_email.py — Tests for email-related background jobs

Covers: _scan_user_inbox, _mine_vendor_contacts, _scan_outbound_rfqs,
_sync_user_contacts, _job_contacts_sync,
_job_contact_scoring, _job_contact_status_compute, _job_email_reverification,
_job_email_health_update, _job_calendar_scan, _job_ownership_sweep,
_job_site_ownership_sweep, _compute_vendor_scores_job.

All jobs use SessionLocal() internally, so we patch app.database.SessionLocal
to return the test DB session with close() disabled.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models import ActivityLog
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


# ── _job_contacts_sync() ─────────────────────────────────────────────


def test_contacts_sync_syncs_eligible_user(scheduler_db, test_user):
    """Users with no prior sync get synced."""
    test_user.refresh_token = "rt_contacts"
    test_user.access_token = "at_contacts"
    test_user.m365_connected = True
    test_user.last_contacts_sync = None
    scheduler_db.commit()

    with patch("app.jobs.email_jobs._sync_user_contacts", new_callable=AsyncMock) as mock_sync:
        from app.jobs.email_jobs import _job_contacts_sync

        asyncio.run(_job_contacts_sync())
        mock_sync.assert_called_once()


def test_contacts_sync_skips_recently_synced(scheduler_db, test_user):
    """Users synced within 24 hours are skipped."""
    test_user.refresh_token = "rt_contacts"
    test_user.access_token = "at_contacts"
    test_user.m365_connected = True
    test_user.last_contacts_sync = datetime.now(timezone.utc) - timedelta(hours=12)
    scheduler_db.commit()

    with patch("app.jobs.email_jobs._sync_user_contacts", new_callable=AsyncMock) as mock_sync:
        from app.jobs.email_jobs import _job_contacts_sync

        asyncio.run(_job_contacts_sync())
        mock_sync.assert_not_called()


def test_contacts_sync_skips_disconnected_user(scheduler_db, test_user):
    """Disconnected users are skipped."""
    test_user.refresh_token = "rt_contacts"
    test_user.access_token = "at_contacts"
    test_user.m365_connected = False
    test_user.last_contacts_sync = None
    scheduler_db.commit()

    with patch("app.jobs.email_jobs._sync_user_contacts", new_callable=AsyncMock) as mock_sync:
        from app.jobs.email_jobs import _job_contacts_sync

        asyncio.run(_job_contacts_sync())
        mock_sync.assert_not_called()


def test_contacts_sync_handles_per_user_error(scheduler_db, test_user):
    """Errors during per-user sync do not crash the job."""
    test_user.refresh_token = "rt_contacts"
    test_user.access_token = "at_contacts"
    test_user.m365_connected = True
    test_user.last_contacts_sync = None
    scheduler_db.commit()

    with patch("app.jobs.email_jobs._sync_user_contacts", new_callable=AsyncMock) as mock_sync:
        mock_sync.side_effect = Exception("Graph API down")
        from app.jobs.email_jobs import _job_contacts_sync

        # Should not raise
        asyncio.run(_job_contacts_sync())


def test_contacts_sync_syncs_stale_user(scheduler_db, test_user):
    """Users last synced >24h ago get synced."""
    test_user.refresh_token = "rt_contacts"
    test_user.access_token = "at_contacts"
    test_user.m365_connected = True
    test_user.last_contacts_sync = datetime.now(timezone.utc) - timedelta(hours=30)
    scheduler_db.commit()

    with patch("app.jobs.email_jobs._sync_user_contacts", new_callable=AsyncMock) as mock_sync:
        from app.jobs.email_jobs import _job_contacts_sync

        asyncio.run(_job_contacts_sync())
        mock_sync.assert_called_once()


def test_contacts_sync_error_in_user_gathering(scheduler_db):
    """Error during user-gathering returns early."""
    with patch.object(scheduler_db, "query", side_effect=Exception("DB error")):
        from app.jobs.email_jobs import _job_contacts_sync

        # Should not raise
        asyncio.run(_job_contacts_sync())


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
        patch("app.jobs.email_jobs._sync_user_contacts", new_callable=AsyncMock),
    ):
        from app.jobs.email_jobs import _job_contacts_sync

        # Should not raise
        asyncio.run(_job_contacts_sync())


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
        if get_count[0] >= 1:
            return None
        return original_get(model, id_)

    with patch("app.jobs.email_jobs._sync_user_contacts", new_callable=AsyncMock) as mock_sync:
        scheduler_db.get = _get_none_on_second
        from app.jobs.email_jobs import _job_contacts_sync

        asyncio.run(_job_contacts_sync())
        scheduler_db.get = original_get

        mock_sync.assert_not_called()


# ── _scan_user_inbox ──────────────────────────────────────────────────


def test_scan_user_inbox_first_time_backfill(scheduler_db, test_user):
    """First-time scan (last_inbox_scan=None) triggers backfill."""
    test_user.access_token = "at_scan"
    test_user.last_inbox_scan = None
    scheduler_db.commit()

    with (
        patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.email_service.poll_inbox", new_callable=AsyncMock, return_value=["resp1"]),
        patch("app.jobs.inventory_jobs._scan_stock_list_attachments", new_callable=AsyncMock) as mock_stock,
        patch("app.jobs.email_jobs._mine_vendor_contacts", new_callable=AsyncMock) as mock_mine,
        patch("app.jobs.email_jobs._scan_outbound_rfqs", new_callable=AsyncMock) as mock_outbound,
        patch("app.config.settings") as mock_settings,
    ):
        mock_settings.inbox_backfill_days = 180

        from app.jobs.email_jobs import _scan_user_inbox

        asyncio.run(_scan_user_inbox(test_user, scheduler_db))

        mock_stock.assert_called_once()
        mock_mine.assert_called_once()
        mock_outbound.assert_called_once()
        assert mock_stock.call_args[0][2] is True
        assert test_user.last_inbox_scan is not None


def test_scan_user_inbox_not_backfill(scheduler_db, test_user):
    """Non-first scan sets is_backfill=False."""
    test_user.access_token = "at_scan"
    test_user.last_inbox_scan = datetime.now(timezone.utc) - timedelta(hours=1)
    scheduler_db.commit()

    with (
        patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.email_service.poll_inbox", new_callable=AsyncMock, return_value=[]),
        patch("app.jobs.inventory_jobs._scan_stock_list_attachments", new_callable=AsyncMock) as mock_stock,
        patch("app.jobs.email_jobs._mine_vendor_contacts", new_callable=AsyncMock),
        patch("app.jobs.email_jobs._scan_outbound_rfqs", new_callable=AsyncMock),
        patch("app.config.settings") as mock_settings,
    ):
        mock_settings.inbox_backfill_days = 180

        from app.jobs.email_jobs import _scan_user_inbox

        asyncio.run(_scan_user_inbox(test_user, scheduler_db))

        assert mock_stock.call_args[0][2] is False


def test_scan_user_inbox_no_valid_token(scheduler_db, test_user):
    """Inbox scan is skipped when no valid token is available."""
    test_user.access_token = "at_scan"
    test_user.last_inbox_scan = None
    scheduler_db.commit()

    with (
        patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value=None),
        patch("app.email_service.poll_inbox", new_callable=AsyncMock) as mock_poll,
        patch("app.jobs.inventory_jobs._scan_stock_list_attachments", new_callable=AsyncMock),
        patch("app.jobs.email_jobs._mine_vendor_contacts", new_callable=AsyncMock),
        patch("app.jobs.email_jobs._scan_outbound_rfqs", new_callable=AsyncMock),
        patch("app.config.settings") as mock_settings,
    ):
        mock_settings.inbox_backfill_days = 180

        from app.jobs.email_jobs import _scan_user_inbox

        asyncio.run(_scan_user_inbox(test_user, scheduler_db))

        mock_poll.assert_not_called()


def test_scan_user_inbox_poll_exception(scheduler_db, test_user):
    """Exception in poll_inbox is caught and sub-operations still run."""
    test_user.access_token = "at_scan"
    test_user.last_inbox_scan = datetime.now(timezone.utc) - timedelta(hours=1)
    scheduler_db.commit()

    with (
        patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.email_service.poll_inbox", new_callable=AsyncMock, side_effect=Exception("poll failed")),
        patch("app.jobs.inventory_jobs._scan_stock_list_attachments", new_callable=AsyncMock) as mock_stock,
        patch("app.jobs.email_jobs._mine_vendor_contacts", new_callable=AsyncMock) as mock_mine,
        patch("app.jobs.email_jobs._scan_outbound_rfqs", new_callable=AsyncMock) as mock_outbound,
        patch("app.config.settings") as mock_settings,
    ):
        mock_settings.inbox_backfill_days = 180

        from app.jobs.email_jobs import _scan_user_inbox

        asyncio.run(_scan_user_inbox(test_user, scheduler_db))

        mock_stock.assert_called_once()
        mock_mine.assert_called_once()
        mock_outbound.assert_called_once()


def test_scan_user_inbox_sub_operation_exceptions(scheduler_db, test_user):
    """Exceptions in sub-operations are caught individually."""
    test_user.access_token = "at_scan"
    test_user.last_inbox_scan = datetime.now(timezone.utc) - timedelta(hours=1)
    scheduler_db.commit()

    with (
        patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.email_service.poll_inbox", new_callable=AsyncMock, return_value=[]),
        patch(
            "app.jobs.inventory_jobs._scan_stock_list_attachments",
            new_callable=AsyncMock,
            side_effect=Exception("stock error"),
        ),
        patch("app.jobs.email_jobs._mine_vendor_contacts", new_callable=AsyncMock, side_effect=Exception("mine error")),
        patch(
            "app.jobs.email_jobs._scan_outbound_rfqs", new_callable=AsyncMock, side_effect=Exception("outbound error")
        ),
        patch("app.config.settings") as mock_settings,
    ):
        mock_settings.inbox_backfill_days = 180

        from app.jobs.email_jobs import _scan_user_inbox

        asyncio.run(_scan_user_inbox(test_user, scheduler_db))


# ── _mine_vendor_contacts ─────────────────────────────────────────────


def test_mine_vendor_contacts_no_contacts(scheduler_db, test_user):
    """No contacts found returns early."""
    test_user.access_token = "at_mine"
    scheduler_db.commit()

    mock_miner = MagicMock()
    mock_miner.scan_inbox = AsyncMock(return_value={"contacts_enriched": []})

    with (
        patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.connectors.email_mining.EmailMiner", return_value=mock_miner),
        patch("app.config.settings") as mock_settings,
    ):
        mock_settings.inbox_backfill_days = 180

        from app.jobs.email_jobs import _mine_vendor_contacts

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
        patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.connectors.email_mining.EmailMiner", return_value=mock_miner),
        patch("app.vendor_utils.normalize_vendor_name", return_value="new vendor co"),
        patch("app.vendor_utils.merge_emails_into_card", return_value=1) as mock_merge_emails,
        patch("app.vendor_utils.merge_phones_into_card") as mock_merge_phones,
        patch("app.config.settings") as mock_settings,
    ):
        mock_settings.inbox_backfill_days = 180

        from app.jobs.email_jobs import _mine_vendor_contacts

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
        patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.connectors.email_mining.EmailMiner", return_value=mock_miner),
        patch("app.vendor_utils.normalize_vendor_name", return_value="arrow electronics"),
        patch("app.vendor_utils.merge_emails_into_card", return_value=1) as mock_merge_emails,
        patch("app.vendor_utils.merge_phones_into_card") as mock_merge_phones,
        patch("app.config.settings") as mock_settings,
    ):
        mock_settings.inbox_backfill_days = 180

        from app.jobs.email_jobs import _mine_vendor_contacts

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
        patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.connectors.email_mining.EmailMiner", return_value=mock_miner),
        patch("app.vendor_utils.merge_emails_into_card") as mock_merge,
        patch("app.config.settings") as mock_settings,
    ):
        mock_settings.inbox_backfill_days = 180

        from app.jobs.email_jobs import _mine_vendor_contacts

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
        patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.connectors.email_mining.EmailMiner", return_value=mock_miner),
        patch("app.vendor_utils.normalize_vendor_name", return_value="test vendor"),
        patch("app.vendor_utils.merge_emails_into_card", return_value=1),
        patch("app.vendor_utils.merge_phones_into_card"),
        patch("app.config.settings") as mock_settings,
    ):
        mock_settings.inbox_backfill_days = 180

        scheduler_db.commit = _failing_commit
        from app.jobs.email_jobs import _mine_vendor_contacts

        asyncio.run(_mine_vendor_contacts(test_user, scheduler_db, is_backfill=False))
        scheduler_db.commit = original_commit


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
        patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.connectors.email_mining.EmailMiner", return_value=mock_miner),
        patch("app.vendor_utils.normalize_vendor_name", return_value="conflict vendor"),
        patch("app.config.settings") as mock_settings,
    ):
        mock_settings.inbox_backfill_days = 180

        scheduler_db.flush = _failing_flush
        from app.jobs.email_jobs import _mine_vendor_contacts

        asyncio.run(_mine_vendor_contacts(test_user, scheduler_db, is_backfill=False))
        scheduler_db.flush = original_flush


def test_mine_vendor_contacts_final_commit_error(scheduler_db, test_user):
    """Final commit error in _mine_vendor_contacts is handled."""
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

    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.all.return_value = []
    mock_db.flush = MagicMock()
    mock_db.add = MagicMock()
    mock_db.commit = MagicMock(side_effect=Exception("final commit failed"))
    mock_db.rollback = MagicMock()

    mock_user = MagicMock()
    mock_user.access_token = "at_mine"

    with (
        patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.connectors.email_mining.EmailMiner", return_value=mock_miner),
        patch("app.vendor_utils.normalize_vendor_name", return_value="commit error vendor"),
        patch("app.vendor_utils.merge_emails_into_card", return_value=1),
        patch("app.vendor_utils.merge_phones_into_card"),
        patch("app.config.settings") as mock_settings,
    ):
        mock_settings.inbox_backfill_days = 180

        from app.jobs.email_jobs import _mine_vendor_contacts

        asyncio.run(_mine_vendor_contacts(mock_user, mock_db, is_backfill=False))
        mock_db.rollback.assert_called()


# ── _scan_outbound_rfqs ───────────────────────────────────────────────


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
        patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.connectors.email_mining.EmailMiner", return_value=mock_miner),
        patch("app.config.settings") as mock_settings,
    ):
        mock_settings.inbox_backfill_days = 180

        from app.jobs.email_jobs import _scan_outbound_rfqs

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
        patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.connectors.email_mining.EmailMiner", return_value=mock_miner),
        patch("app.config.settings") as mock_settings,
    ):
        mock_settings.inbox_backfill_days = 180

        from app.jobs.email_jobs import _scan_outbound_rfqs

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
        patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.connectors.email_mining.EmailMiner", return_value=mock_miner),
        patch("app.config.settings") as mock_settings,
    ):
        mock_settings.inbox_backfill_days = 180

        from app.jobs.email_jobs import _scan_outbound_rfqs

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
        patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.connectors.email_mining.EmailMiner", return_value=mock_miner),
        patch("app.config.settings") as mock_settings,
    ):
        mock_settings.inbox_backfill_days = 180

        scheduler_db.commit = _failing_commit
        from app.jobs.email_jobs import _scan_outbound_rfqs

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
        patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.connectors.email_mining.EmailMiner", return_value=mock_miner),
        patch("app.config.settings") as mock_settings,
    ):
        mock_settings.inbox_backfill_days = 180

        from app.jobs.email_jobs import _scan_outbound_rfqs

        asyncio.run(_scan_outbound_rfqs(test_user, scheduler_db, is_backfill=False))

    scheduler_db.refresh(test_vendor_card)
    assert (test_vendor_card.total_outreach or 0) >= 1


def test_scan_outbound_rfqs_final_commit_error(scheduler_db, test_user, test_vendor_card):
    """Final commit error in _scan_outbound_rfqs is handled."""
    test_user.access_token = "at_out"
    scheduler_db.commit()

    mock_miner = MagicMock()
    mock_miner.scan_sent_items = AsyncMock(
        return_value={
            "rfqs_detected": 1,
            "vendors_contacted": {"arrow.com": 1},
        }
    )

    mock_card = MagicMock()
    mock_card.total_outreach = 5
    mock_card.last_contact_at = None
    mock_card.domain = "arrow.com"
    mock_card.normalized_name = "arrow"

    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.all.return_value = [mock_card]
    mock_db.commit = MagicMock(side_effect=Exception("final commit failed"))
    mock_db.rollback = MagicMock()

    mock_user = MagicMock()
    mock_user.access_token = "at_out"

    with (
        patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.connectors.email_mining.EmailMiner", return_value=mock_miner),
        patch("app.config.settings") as mock_settings,
    ):
        mock_settings.inbox_backfill_days = 180

        from app.jobs.email_jobs import _scan_outbound_rfqs

        asyncio.run(_scan_outbound_rfqs(mock_user, mock_db, is_backfill=False))
        mock_db.rollback.assert_called()


# ── _sync_user_contacts ───────────────────────────────────────────────


def test_sync_user_contacts_empty(scheduler_db, test_user):
    """No contacts from Graph API updates sync timestamp only."""
    test_user.access_token = "at_sync"
    test_user.last_contacts_sync = None
    scheduler_db.commit()

    mock_gc = MagicMock()
    mock_gc.delta_query = AsyncMock(return_value=([], "delta-token"))

    with patch("app.utils.graph_client.GraphClient", return_value=mock_gc):
        from app.jobs.email_jobs import _sync_user_contacts

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
        from app.jobs.email_jobs import _sync_user_contacts

        asyncio.run(_sync_user_contacts(test_user, scheduler_db))

        mock_merge_e.assert_called_once()
        mock_merge_p.assert_called_once()
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
        from app.jobs.email_jobs import _sync_user_contacts

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
        from app.jobs.email_jobs import _sync_user_contacts

        asyncio.run(_sync_user_contacts(test_user, scheduler_db))

        mock_merge.assert_not_called()


def test_sync_user_contacts_graph_error(scheduler_db, test_user):
    """Graph API error during contacts sync is handled."""
    test_user.access_token = "at_sync"
    scheduler_db.commit()

    mock_gc = MagicMock()
    mock_gc.delta_query = AsyncMock(side_effect=Exception("Graph API error"))

    with patch("app.utils.graph_client.GraphClient", return_value=mock_gc):
        from app.jobs.email_jobs import _sync_user_contacts

        asyncio.run(_sync_user_contacts(test_user, scheduler_db))

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
        from app.jobs.email_jobs import _sync_user_contacts

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
        from app.jobs.email_jobs import _sync_user_contacts

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
        from app.jobs.email_jobs import _sync_user_contacts

        asyncio.run(_sync_user_contacts(test_user, scheduler_db))

        mock_merge_e.assert_called_once()
        mock_merge_p.assert_called_once()


def test_sync_user_contacts_delta_token_update_existing(scheduler_db, test_user):
    """Existing sync_state gets delta_token updated."""
    from app.models.pipeline import SyncState

    test_user.access_token = "at_sync"
    scheduler_db.commit()

    ss = SyncState(user_id=test_user.id, folder="contacts_sync", delta_token="old-token")
    scheduler_db.add(ss)
    scheduler_db.commit()

    mock_gc = MagicMock()
    mock_gc.delta_query = AsyncMock(return_value=([], "new-delta-token"))

    with patch("app.utils.graph_client.GraphClient", return_value=mock_gc):
        from app.jobs.email_jobs import _sync_user_contacts

        asyncio.run(_sync_user_contacts(test_user, scheduler_db))

    scheduler_db.refresh(ss)
    assert ss.delta_token == "new-delta-token"


def test_sync_user_contacts_delta_expired_with_sync_state(scheduler_db, test_user):
    """GraphSyncStateExpired with existing sync_state clears delta_token."""
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
        from app.jobs.email_jobs import _sync_user_contacts

        asyncio.run(_sync_user_contacts(test_user, scheduler_db))

    scheduler_db.refresh(ss)
    assert ss.delta_token is None


def test_sync_user_contacts_delta_expired_full_resync_fails(scheduler_db, test_user):
    """GraphSyncStateExpired followed by full pull failure returns early."""
    from app.utils.graph_client import GraphSyncStateExpired

    test_user.access_token = "at_sync"
    test_user.last_contacts_sync = None
    scheduler_db.commit()

    mock_gc = MagicMock()
    mock_gc.delta_query = AsyncMock(side_effect=GraphSyncStateExpired("token expired"))
    mock_gc.get_all_pages = AsyncMock(side_effect=Exception("Full pull failed"))

    with patch("app.utils.graph_client.GraphClient", return_value=mock_gc):
        from app.jobs.email_jobs import _sync_user_contacts

        asyncio.run(_sync_user_contacts(test_user, scheduler_db))

    assert test_user.last_contacts_sync is None


def test_sync_user_contacts_vendor_card_flush_conflict(scheduler_db, test_user):
    """VendorCard flush conflict rolls back and continues."""
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

    original_flush = scheduler_db.flush
    flush_call_count = 0

    def flaky_flush(*args, **kwargs):
        nonlocal flush_call_count
        flush_call_count += 1
        if flush_call_count == 2:
            raise Exception("Uniqueness conflict")
        return original_flush(*args, **kwargs)

    with (
        patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
        patch.object(scheduler_db, "flush", side_effect=flaky_flush),
        patch("app.vendor_utils.normalize_vendor_name", return_value="conflict co"),
    ):
        from app.jobs.email_jobs import _sync_user_contacts

        asyncio.run(_sync_user_contacts(test_user, scheduler_db))


def test_sync_user_contacts_commit_error_final(scheduler_db, test_user):
    """Commit failure in final sync."""
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
        from app.jobs.email_jobs import _sync_user_contacts

        asyncio.run(_sync_user_contacts(test_user, scheduler_db))


# ── _job_ownership_sweep() ────────────────────────────────────────────


def test_ownership_sweep_delegates(scheduler_db):
    """Ownership sweep delegates to run_ownership_sweep."""
    with patch(
        "app.services.ownership_service.run_ownership_sweep",
        new_callable=AsyncMock,
    ) as mock_sweep:
        from app.jobs.email_jobs import _job_ownership_sweep

        asyncio.run(_job_ownership_sweep())
        mock_sweep.assert_called_once_with(scheduler_db)


def test_ownership_sweep_error_handling(scheduler_db):
    """Ownership sweep handles errors gracefully."""
    with patch(
        "app.services.ownership_service.run_ownership_sweep",
        new_callable=AsyncMock,
        side_effect=Exception("Sweep failed"),
    ):
        from app.jobs.email_jobs import _job_ownership_sweep

        asyncio.run(_job_ownership_sweep())


# ── _job_site_ownership_sweep() ───────────────────────────────────────


def test_site_ownership_sweep_delegates(scheduler_db):
    """Site ownership sweep delegates to run_site_ownership_sweep."""
    with patch("app.services.ownership_service.run_site_ownership_sweep") as mock_sweep:
        from app.jobs.email_jobs import _job_site_ownership_sweep

        asyncio.run(_job_site_ownership_sweep())
        mock_sweep.assert_called_once_with(scheduler_db)


def test_site_ownership_sweep_error_handling(scheduler_db):
    """Site ownership sweep handles errors gracefully."""
    with patch(
        "app.services.ownership_service.run_site_ownership_sweep",
        side_effect=Exception("Sweep failed"),
    ):
        from app.jobs.email_jobs import _job_site_ownership_sweep

        asyncio.run(_job_site_ownership_sweep())


# ── _compute_vendor_scores_job() ──────────────────────────────────────


def test_compute_engagement_scores_job_delegates(db_session):
    """Vendor scores job delegates to compute_all_vendor_scores."""
    with patch(
        "app.services.vendor_score.compute_all_vendor_scores",
        new_callable=AsyncMock,
    ) as mock_compute:
        mock_compute.return_value = {"updated": 10, "skipped": 2}
        from app.jobs.email_jobs import _compute_vendor_scores_job

        asyncio.run(_compute_vendor_scores_job(db_session))
        mock_compute.assert_called_once_with(db_session)


def test_compute_engagement_scores_job_handles_error(db_session):
    """Vendor scores job handles errors without propagating."""
    with patch(
        "app.services.vendor_score.compute_all_vendor_scores",
        new_callable=AsyncMock,
        side_effect=Exception("Scorer crashed"),
    ):
        from app.jobs.email_jobs import _compute_vendor_scores_job

        asyncio.run(_compute_vendor_scores_job(db_session))


# ── _job_contact_scoring() ────────────────────────────────────────────


def test_contact_scoring_runs_successfully(scheduler_db):
    """Contact scoring job delegates to compute_all_contact_scores."""
    with patch("app.services.contact_intelligence.compute_all_contact_scores") as mock_compute:
        mock_compute.return_value = {"updated": 10, "skipped": 0}
        from app.jobs.email_jobs import _job_contact_scoring

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
        from app.jobs.email_jobs import _job_contact_scoring

        asyncio.run(_job_contact_scoring())


def test_contact_scoring_general_error(scheduler_db):
    """Contact scoring handles general exceptions gracefully."""
    with patch(
        "app.services.contact_intelligence.compute_all_contact_scores",
        side_effect=Exception("Scoring crashed"),
    ):
        from app.jobs.email_jobs import _job_contact_scoring

        asyncio.run(_job_contact_scoring())


# ── _job_contact_status_compute() ─────────────────────────────────────


def test_contact_status_compute_7_to_30_day_window(scheduler_db, test_user, test_company, test_customer_site):
    """Contacts with last activity 7-30 days ago keep current status."""
    from app.models import SiteContact

    sc = SiteContact(
        customer_site_id=test_customer_site.id,
        full_name="Seven Day Contact",
        is_active=True,
        contact_status="active",
    )
    scheduler_db.add(sc)
    scheduler_db.flush()

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

    from app.jobs.email_jobs import _job_contact_status_compute

    asyncio.run(_job_contact_status_compute())

    scheduler_db.refresh(sc)
    assert sc.contact_status == "active"


def test_contact_status_compute_champion_not_downgraded(scheduler_db, test_user, test_company, test_customer_site):
    """Champion contacts are never downgraded."""
    from app.models import SiteContact

    sc = SiteContact(
        customer_site_id=test_customer_site.id,
        full_name="Champion Contact",
        is_active=True,
        contact_status="champion",
    )
    scheduler_db.add(sc)
    scheduler_db.commit()

    from app.jobs.email_jobs import _job_contact_status_compute

    asyncio.run(_job_contact_status_compute())

    scheduler_db.refresh(sc)
    assert sc.contact_status == "champion"


def test_contact_status_compute_active_recent(scheduler_db, test_user, test_company, test_customer_site):
    """Contact with activity <= 7 days ago becomes active."""
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

    from app.jobs.email_jobs import _job_contact_status_compute

    asyncio.run(_job_contact_status_compute())

    scheduler_db.refresh(sc)
    assert sc.contact_status == "active"


def test_contact_status_compute_quiet_and_inactive(scheduler_db, test_user, test_company, test_customer_site):
    """30-90 days -> quiet, >90 days -> inactive."""
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

    quiet_activity = ActivityLog(
        user_id=test_user.id,
        activity_type="email_sent",
        channel="outlook",
        site_contact_id=quiet_sc.id,
        auto_logged=True,
        occurred_at=datetime.now(timezone.utc) - timedelta(days=60),
        created_at=datetime.now(timezone.utc) - timedelta(days=60),
    )
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

    from app.jobs.email_jobs import _job_contact_status_compute

    asyncio.run(_job_contact_status_compute())

    scheduler_db.refresh(quiet_sc)
    scheduler_db.refresh(inactive_sc)
    assert quiet_sc.contact_status == "quiet"
    assert inactive_sc.contact_status == "inactive"


def test_contact_status_compute_no_activity_old_created(scheduler_db, test_user, test_company, test_customer_site):
    """No activity + created >90 days ago -> inactive; recent created -> keep 'new'."""
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

    from app.jobs.email_jobs import _job_contact_status_compute

    asyncio.run(_job_contact_status_compute())

    scheduler_db.refresh(old_sc)
    scheduler_db.refresh(new_sc)
    assert old_sc.contact_status == "inactive"
    assert new_sc.contact_status == "new"


def test_contact_status_compute_error_handler(scheduler_db):
    """Exception in _job_contact_status_compute is caught and rolled back."""
    with patch.object(scheduler_db, "query", side_effect=Exception("DB crash")):
        from app.jobs.email_jobs import _job_contact_status_compute

        asyncio.run(_job_contact_status_compute())


# ── _job_email_reverification() ───────────────────────────────────────


def test_email_reverification_success(scheduler_db):
    """_job_email_reverification happy path."""
    mock_reverify = AsyncMock(return_value={"processed": 20, "invalidated": 3})
    with patch("app.services.customer_enrichment_batch.run_email_reverification", mock_reverify):
        from app.jobs.email_jobs import _job_email_reverification

        asyncio.run(_job_email_reverification())
    mock_reverify.assert_called_once()


def test_email_reverification_error(scheduler_db):
    """Exception rolls back."""
    mock_reverify = AsyncMock(side_effect=Exception("Reverify failed"))
    with patch("app.services.customer_enrichment_batch.run_email_reverification", mock_reverify):
        from app.jobs.email_jobs import _job_email_reverification

        asyncio.run(_job_email_reverification())


# ── _job_email_health_update() ────────────────────────────────────────


def test_email_health_update_timeout(scheduler_db):
    """asyncio.TimeoutError rolls back."""
    with patch("app.services.response_analytics.batch_update_email_health", side_effect=Exception("slow")):
        with patch("asyncio.wait_for", new_callable=AsyncMock, side_effect=asyncio.TimeoutError):
            from app.jobs.email_jobs import _job_email_health_update

            asyncio.run(_job_email_health_update())


def test_email_health_update_success(scheduler_db):
    """Happy path logs result."""
    mock_health = MagicMock(return_value={"updated": 15})
    with patch("app.services.response_analytics.batch_update_email_health", mock_health):
        from app.jobs.email_jobs import _job_email_health_update

        asyncio.run(_job_email_health_update())
    mock_health.assert_called_once()


def test_email_health_update_generic_error(scheduler_db):
    """Generic exception rolls back."""
    mock_health = MagicMock(side_effect=RuntimeError("DB error"))
    with patch("app.services.response_analytics.batch_update_email_health", mock_health):
        from app.jobs.email_jobs import _job_email_health_update

        asyncio.run(_job_email_health_update())


# ── _job_calendar_scan() ──────────────────────────────────────────────


def test_calendar_scan_user_query_error(scheduler_db):
    """Exception in user query causes early return."""
    with patch.object(scheduler_db, "query", side_effect=Exception("DB error")):
        from app.jobs.email_jobs import _job_calendar_scan

        asyncio.run(_job_calendar_scan())


def test_calendar_scan_user_not_found(scheduler_db, test_user):
    """User not found in scan_db returns early."""
    test_user.access_token = "at_cal"
    test_user.refresh_token = "rt_cal"
    test_user.m365_connected = True
    scheduler_db.commit()

    mock_scan_db = MagicMock()
    mock_scan_db.get.return_value = None

    call_count = 0
    original_session_local = None

    def _session_factory():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return scheduler_db
        return mock_scan_db

    with patch("app.database.SessionLocal", side_effect=_session_factory):
        from app.jobs.email_jobs import _job_calendar_scan

        asyncio.run(_job_calendar_scan())


def test_calendar_scan_timeout(scheduler_db, test_user):
    """asyncio.TimeoutError in _safe_cal_scan."""
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
        patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch(
            "app.services.calendar_intelligence.scan_calendar_events",
            new_callable=AsyncMock,
            side_effect=asyncio.TimeoutError,
        ),
    ):
        from app.jobs.email_jobs import _job_calendar_scan

        asyncio.run(_job_calendar_scan())

    mock_scan_db.rollback.assert_called()


def test_calendar_scan_generic_error(scheduler_db, test_user):
    """Generic exception in _safe_cal_scan."""
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
        patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch(
            "app.services.calendar_intelligence.scan_calendar_events",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ),
    ):
        from app.jobs.email_jobs import _job_calendar_scan

        asyncio.run(_job_calendar_scan())

    mock_scan_db.rollback.assert_called()


def test_calendar_scan_no_token(scheduler_db, test_user):
    """get_valid_token returns None for a user."""
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
        patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value=None),
    ):
        from app.jobs.email_jobs import _job_calendar_scan

        asyncio.run(_job_calendar_scan())


def test_calendar_scan_success(scheduler_db, test_user):
    """Successful calendar scan logs events."""
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
        patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="valid-token"),
        patch(
            "app.services.calendar_intelligence.scan_calendar_events",
            new_callable=AsyncMock,
            return_value={"events_found": 5},
        ),
    ):
        from app.jobs.email_jobs import _job_calendar_scan

        asyncio.run(_job_calendar_scan())
