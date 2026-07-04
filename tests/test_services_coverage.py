"""Tests for small service modules with 0% coverage.

Covers: health_service, teams_notifications, mailbox_intelligence,
customer_analysis_service, customer_enrichment_batch,
calendar_intelligence, customer_enrichment_service.

Called by: pytest
Depends on: tests/conftest.py fixtures
"""

import os

os.environ["TESTING"] = "1"

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _patch_graph_client(*, pages_return=None, pages_side_effect=None):
    """Patch GraphClient with a mock whose get_all_pages is stubbed.

    Returns the patch context manager so callers can use it in a `with` block.
    """
    mock_gc = MagicMock()
    if pages_side_effect is not None:
        mock_gc.get_all_pages = AsyncMock(side_effect=pages_side_effect)
    else:
        mock_gc.get_all_pages = AsyncMock(return_value=pages_return)
    return patch("app.utils.graph_client.GraphClient", return_value=mock_gc)


def _add_requirement(db_session, customer_site_id, *, req_name, mpn, brand, qty):
    """Create a Requisition (linked to a customer site) plus one Requirement.

    Returns the flushed Requirement so callers can attach sightings, etc.
    """
    from app.models import Requirement, Requisition

    req = Requisition(
        name=req_name,
        customer_name="Acme",
        customer_site_id=customer_site_id,
        status="open",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.flush()
    item = Requirement(
        requisition_id=req.id,
        primary_mpn=mpn,
        brand=brand,
        target_qty=qty,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(item)
    db_session.flush()
    return item


# ── Health Service ──────────────────────────────────────────────────────


class TestHealthService:
    """Tests for app/services/health_service.py."""

    def test_check_backup_freshness_file_missing(self, db_session):
        from app.services.health_service import check_backup_freshness

        with patch("pathlib.Path.exists", return_value=False):
            result = check_backup_freshness()
        assert result == "unknown"

    def test_check_backup_freshness_ok(self, db_session):
        from app.services.health_service import check_backup_freshness

        recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        with (
            patch("pathlib.Path.exists", return_value=True),
            patch("pathlib.Path.read_text", return_value=recent),
        ):
            result = check_backup_freshness()
        assert result == "ok"

    def test_check_backup_freshness_stale(self, db_session):
        from app.services.health_service import check_backup_freshness

        old = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
        with (
            patch("pathlib.Path.exists", return_value=True),
            patch("pathlib.Path.read_text", return_value=old),
        ):
            result = check_backup_freshness()
        assert result == "stale"

    def test_check_backup_freshness_z_suffix(self, db_session):
        from app.services.health_service import check_backup_freshness

        recent = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        with (
            patch("pathlib.Path.exists", return_value=True),
            patch("pathlib.Path.read_text", return_value=recent),
        ):
            result = check_backup_freshness()
        assert result == "ok"

    def test_check_backup_freshness_naive_timestamp(self, db_session):
        from app.services.health_service import check_backup_freshness

        naive = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
        with (
            patch("pathlib.Path.exists", return_value=True),
            patch("pathlib.Path.read_text", return_value=naive),
        ):
            result = check_backup_freshness()
        assert result == "ok"

    def test_check_backup_freshness_parse_error(self, db_session):
        from app.services.health_service import check_backup_freshness

        with (
            patch("pathlib.Path.exists", return_value=True),
            patch("pathlib.Path.read_text", return_value="not-a-date"),
        ):
            result = check_backup_freshness()
        assert result == "unknown"

    def test_check_backup_freshness_os_error(self, db_session):
        from app.services.health_service import check_backup_freshness

        with (
            patch("pathlib.Path.exists", return_value=True),
            patch("pathlib.Path.read_text", side_effect=OSError("disk error")),
        ):
            result = check_backup_freshness()
        assert result == "unknown"


# ── Teams Notifications ─────────────────────────────────────────────────


class TestTeamsNotifications:
    """Tests for app/services/teams_notifications.py."""

    @pytest.mark.asyncio
    async def test_post_teams_channel_no_webhook(self, db_session):
        from app.services.teams_notifications import post_teams_channel

        with patch("app.services.teams_notifications.get_credential_cached", return_value=None):
            await post_teams_channel("Hello")
        # Should silently return

    @pytest.mark.asyncio
    async def test_post_teams_channel_success(self, db_session):
        from app.services.teams_notifications import post_teams_channel

        mock_resp = MagicMock(status_code=200)
        with (
            patch("app.services.teams_notifications.get_credential_cached", return_value="https://webhook.test"),
            patch("app.services.teams_notifications.http") as mock_http,
        ):
            mock_http.post = AsyncMock(return_value=mock_resp)
            await post_teams_channel("Test message")
            mock_http.post.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_post_teams_channel_non_200(self, db_session):
        from app.services.teams_notifications import post_teams_channel

        mock_resp = MagicMock(status_code=400, text="Bad Request")
        with (
            patch("app.services.teams_notifications.get_credential_cached", return_value="https://webhook.test"),
            patch("app.services.teams_notifications.http") as mock_http,
        ):
            mock_http.post = AsyncMock(return_value=mock_resp)
            await post_teams_channel("Test message")

    @pytest.mark.asyncio
    async def test_post_teams_channel_exception(self, db_session):
        from app.services.teams_notifications import post_teams_channel

        with (
            patch("app.services.teams_notifications.get_credential_cached", return_value="https://webhook.test"),
            patch("app.services.teams_notifications.http") as mock_http,
        ):
            mock_http.post = AsyncMock(side_effect=Exception("network error"))
            await post_teams_channel("Test message")

    @pytest.mark.asyncio
    async def test_send_teams_dm_no_token_no_db(self, db_session):
        from app.services.teams_notifications import send_teams_dm

        user = SimpleNamespace(email="user@test.com", access_token=None)
        await send_teams_dm(user, "Hello")
        # Should skip silently

    @pytest.mark.asyncio
    async def test_send_teams_dm_with_access_token(self, db_session):
        from app.services.teams_notifications import send_teams_dm

        user = SimpleNamespace(email="user@test.com", access_token="token123")
        mock_gc = MagicMock()
        mock_gc.post_json = AsyncMock(side_effect=[{"id": "chat123"}, {}])

        with patch("app.utils.graph_client.GraphClient", return_value=mock_gc):
            await send_teams_dm(user, "Hello")
        assert mock_gc.post_json.await_count == 2

    @pytest.mark.asyncio
    async def test_send_teams_dm_with_db_token_refresh(self, db_session):
        from app.services.teams_notifications import send_teams_dm

        user = SimpleNamespace(email="user@test.com", access_token=None)
        mock_gc = MagicMock()
        mock_gc.post_json = AsyncMock(side_effect=[{"id": "chat123"}, {}])

        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="refreshed-token"),
        ):
            await send_teams_dm(user, "Hello", db=db_session)
        assert mock_gc.post_json.await_count == 2

    @pytest.mark.asyncio
    async def test_send_teams_dm_no_valid_token_after_refresh(self, db_session):
        from app.services.teams_notifications import send_teams_dm

        user = SimpleNamespace(email="user@test.com", access_token=None)
        with patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value=None):
            await send_teams_dm(user, "Hello", db=db_session)

    @pytest.mark.asyncio
    async def test_send_teams_dm_no_chat_id(self, db_session):
        from app.services.teams_notifications import send_teams_dm

        user = SimpleNamespace(email="user@test.com", access_token="token123")
        mock_gc = MagicMock()
        mock_gc.post_json = AsyncMock(return_value={})  # no "id" key

        with patch("app.utils.graph_client.GraphClient", return_value=mock_gc):
            await send_teams_dm(user, "Hello")
        # Only the chat creation call, no message call
        assert mock_gc.post_json.await_count == 1

    @pytest.mark.asyncio
    async def test_send_teams_dm_exception(self, db_session):
        from app.services.teams_notifications import send_teams_dm

        user = SimpleNamespace(email="user@test.com", access_token="token123")
        with patch("app.utils.graph_client.GraphClient", side_effect=Exception("graph error")):
            await send_teams_dm(user, "Hello")
        # Should not raise


# ── Mailbox Intelligence ────────────────────────────────────────────────


class TestMailboxIntelligence:
    """Tests for app/services/mailbox_intelligence.py."""

    @pytest.mark.asyncio
    async def test_fetch_and_store_success(self, db_session):
        from app.services.mailbox_intelligence import fetch_and_store_mailbox_settings

        user = SimpleNamespace(
            email="user@test.com",
            timezone=None,
            working_hours_start=None,
            working_hours_end=None,
        )
        mock_gc = MagicMock()
        mock_gc.get_json = AsyncMock(
            return_value={
                "timeZone": "America/New_York",
                "workingHours": {
                    "startTime": "08:00:00.0000000",
                    "endTime": "17:00:00.0000000",
                },
                "automaticRepliesSetting": {"status": "alwaysEnabled"},
            }
        )

        with patch("app.utils.graph_client.GraphClient", return_value=mock_gc):
            result = await fetch_and_store_mailbox_settings("token", user, db_session)

        assert result is not None
        assert result["timezone"] == "America/New_York"
        assert result["working_hours_start"] == "08:00"
        assert result["working_hours_end"] == "17:00"
        assert result["auto_reply_status"] == "alwaysEnabled"
        assert user.timezone == "America/New_York"

    @pytest.mark.asyncio
    async def test_fetch_and_store_api_error(self, db_session):
        from app.services.mailbox_intelligence import fetch_and_store_mailbox_settings

        user = SimpleNamespace(email="user@test.com")
        mock_gc = MagicMock()
        mock_gc.get_json = AsyncMock(side_effect=Exception("API error"))

        with patch("app.utils.graph_client.GraphClient", return_value=mock_gc):
            result = await fetch_and_store_mailbox_settings("token", user, db_session)
        assert result is None

    @pytest.mark.asyncio
    async def test_fetch_and_store_empty_data(self, db_session):
        from app.services.mailbox_intelligence import fetch_and_store_mailbox_settings

        user = SimpleNamespace(email="user@test.com")
        mock_gc = MagicMock()
        mock_gc.get_json = AsyncMock(return_value=None)

        with patch("app.utils.graph_client.GraphClient", return_value=mock_gc):
            result = await fetch_and_store_mailbox_settings("token", user, db_session)
        assert result is None

    @pytest.mark.asyncio
    async def test_fetch_and_store_error_in_data(self, db_session):
        from app.services.mailbox_intelligence import fetch_and_store_mailbox_settings

        user = SimpleNamespace(email="user@test.com")
        mock_gc = MagicMock()
        mock_gc.get_json = AsyncMock(return_value={"error": {"code": "MailboxNotFound"}})

        with patch("app.utils.graph_client.GraphClient", return_value=mock_gc):
            result = await fetch_and_store_mailbox_settings("token", user, db_session)
        assert result is None

    @pytest.mark.asyncio
    async def test_fetch_and_store_partial_data(self, db_session):
        from app.services.mailbox_intelligence import fetch_and_store_mailbox_settings

        user = SimpleNamespace(
            email="user@test.com",
            timezone=None,
            working_hours_start=None,
            working_hours_end=None,
        )
        mock_gc = MagicMock()
        mock_gc.get_json = AsyncMock(return_value={"timeZone": "UTC"})

        with patch("app.utils.graph_client.GraphClient", return_value=mock_gc):
            result = await fetch_and_store_mailbox_settings("token", user, db_session)

        assert result is not None
        assert result["timezone"] == "UTC"
        assert result["auto_reply_status"] == "disabled"

    @pytest.mark.parametrize(
        "start, end, hour, expected",
        [
            ("08:00", "17:00", 10, True),  # within
            ("08:00", "17:00", 20, False),  # after hours
            (None, None, 10, True),  # no config
            ("08:00", "17:00", 8, True),  # start boundary inclusive
            ("08:00", "17:00", 17, False),  # end boundary exclusive
            ("invalid", "also-bad", 10, True),  # bad format
        ],
    )
    def test_is_within_working_hours(self, db_session, start, end, hour, expected):
        from app.services.mailbox_intelligence import is_within_working_hours

        user = SimpleNamespace(working_hours_start=start, working_hours_end=end)
        assert is_within_working_hours(user, hour) is expected


# ── Customer Analysis Service ───────────────────────────────────────────


class TestCustomerAnalysisService:
    """Tests for app/services/customer_analysis_service.py."""

    @pytest.mark.asyncio
    async def test_analyze_no_company(self, db_session):
        from app.services.customer_analysis_service import analyze_customer_materials

        result = await analyze_customer_materials(99999, db_session=db_session)
        assert result is None

    @pytest.mark.asyncio
    async def test_analyze_no_sites(self, db_session, test_company):
        from app.services.customer_analysis_service import analyze_customer_materials

        # test_company has no sites → should return early
        result = await analyze_customer_materials(test_company.id, db_session=db_session)
        assert result is None

    @pytest.mark.asyncio
    async def test_analyze_no_parts(self, db_session, test_company, test_customer_site):
        from app.services.customer_analysis_service import analyze_customer_materials

        # Site exists but no requirements or sightings
        result = await analyze_customer_materials(test_company.id, db_session=db_session)
        assert result is None

    @pytest.mark.asyncio
    async def test_analyze_with_parts(self, db_session, test_company, test_customer_site):
        from app.services.customer_analysis_service import analyze_customer_materials

        _add_requirement(
            db_session,
            test_customer_site.id,
            req_name="REQ-ANALYSIS",
            mpn="LM317T",
            brand="Texas Instruments",
            qty=100,
        )

        mock_result = {"brands": ["Texas Instruments"], "commodities": ["Voltage Regulators"]}
        with patch("app.utils.claude_client.claude_json", new_callable=AsyncMock, return_value=mock_result):
            await analyze_customer_materials(test_company.id, db_session=db_session)

        db_session.refresh(test_company)
        assert "Texas Instruments" in test_company.brand_tags
        assert "Voltage Regulators" in test_company.commodity_tags

    @pytest.mark.asyncio
    async def test_analyze_with_sightings(self, db_session, test_company, test_customer_site):
        from app.models import Sighting
        from app.services.customer_analysis_service import analyze_customer_materials

        item = _add_requirement(
            db_session,
            test_customer_site.id,
            req_name="REQ-SIGHTING",
            mpn="IC-CHIP-001",
            brand="Intel",
            qty=50,
        )
        # Add a sighting with a different MPN to exercise the sighting loop
        sighting = Sighting(
            requirement_id=item.id,
            vendor_name="Test Vendor",
            mpn_matched="MEM-MODULE-X",
            manufacturer="Samsung",
            qty_available=100,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(sighting)
        db_session.flush()

        mock_result = {"brands": ["Intel", "Samsung"], "commodities": ["Processors", "Memory"]}
        with patch("app.utils.claude_client.claude_json", new_callable=AsyncMock, return_value=mock_result):
            await analyze_customer_materials(test_company.id, db_session=db_session)

        db_session.refresh(test_company)
        assert "Samsung" in test_company.brand_tags

    @pytest.mark.asyncio
    async def test_analyze_claude_returns_none(self, db_session, test_company, test_customer_site):
        from app.services.customer_analysis_service import analyze_customer_materials

        _add_requirement(
            db_session,
            test_customer_site.id,
            req_name="REQ-ANALYSIS2",
            mpn="LM317T",
            brand="TI",
            qty=100,
        )

        with patch("app.utils.claude_client.claude_json", new_callable=AsyncMock, return_value=None):
            result = await analyze_customer_materials(test_company.id, db_session=db_session)
        assert result is None

    @pytest.mark.asyncio
    async def test_analyze_own_session(self, db_session, test_company):
        from app.services.customer_analysis_service import analyze_customer_materials

        with patch("app.database.SessionLocal", return_value=db_session):
            result = await analyze_customer_materials(test_company.id, db_session=None)
        assert result is None  # No sites → returns None

    @pytest.mark.asyncio
    async def test_analyze_exception_own_session(self, db_session):
        from app.services.customer_analysis_service import analyze_customer_materials

        mock_db = MagicMock()
        mock_db.get.side_effect = Exception("DB error")
        mock_db.rollback = MagicMock()
        mock_db.close = MagicMock()

        with patch("app.database.SessionLocal", return_value=mock_db):
            await analyze_customer_materials(1, db_session=None)
        mock_db.rollback.assert_called_once()
        mock_db.close.assert_called_once()


# ── Customer Enrichment Batch ───────────────────────────────────────────


class TestCustomerEnrichmentBatch:
    """Tests for app/services/customer_enrichment_batch.py."""

    def test_can_use_credits_stub(self, db_session):
        from app.services.customer_enrichment_batch import can_use_credits

        assert can_use_credits(db_session, "explorium") is True

    @pytest.mark.asyncio
    async def test_batch_disabled(self, db_session):
        from app.services.customer_enrichment_batch import run_customer_enrichment_batch

        with patch("app.services.customer_enrichment_batch.settings") as mock_settings:
            mock_settings.customer_enrichment_enabled = False
            result = await run_customer_enrichment_batch(db_session)
        assert result["status"] == "disabled"
        assert result["processed"] == 0

    @pytest.mark.asyncio
    async def test_batch_no_gaps(self, db_session):
        from app.services.customer_enrichment_batch import run_customer_enrichment_batch

        with (
            patch("app.services.customer_enrichment_batch.settings") as mock_settings,
            patch("app.services.customer_enrichment_batch.get_enrichment_gaps", return_value=[]),
        ):
            mock_settings.customer_enrichment_enabled = True
            result = await run_customer_enrichment_batch(db_session)
        assert result["status"] == "no_gaps"

    @pytest.mark.asyncio
    async def test_batch_assigned_only_filter(self, db_session):
        from app.services.customer_enrichment_batch import run_customer_enrichment_batch

        gaps = [
            {"company_id": 1, "account_owner_id": 10},
            {"company_id": 2, "account_owner_id": None},
        ]
        with (
            patch("app.services.customer_enrichment_batch.settings") as mock_settings,
            patch("app.services.customer_enrichment_batch.get_enrichment_gaps", return_value=gaps),
            patch(
                "app.services.customer_enrichment_batch.enrich_customer_account",
                new_callable=AsyncMock,
                return_value={"ok": True, "contacts_added": 2},
            ),
        ):
            mock_settings.customer_enrichment_enabled = True
            result = await run_customer_enrichment_batch(db_session, assigned_only=True)
        assert result["processed"] == 1
        assert result["enriched"] == 1

    @pytest.mark.asyncio
    async def test_batch_enrichment_error(self, db_session):
        from app.services.customer_enrichment_batch import run_customer_enrichment_batch

        gaps = [{"company_id": 1, "account_owner_id": None}]
        with (
            patch("app.services.customer_enrichment_batch.settings") as mock_settings,
            patch("app.services.customer_enrichment_batch.get_enrichment_gaps", return_value=gaps),
            patch(
                "app.services.customer_enrichment_batch.enrich_customer_account",
                new_callable=AsyncMock,
                side_effect=Exception("enrichment failed"),
            ),
        ):
            mock_settings.customer_enrichment_enabled = True
            result = await run_customer_enrichment_batch(db_session)
        assert result["errors"] == 1
        assert result["processed"] == 1

    @pytest.mark.asyncio
    async def test_batch_credits_exhausted(self, db_session):
        from app.services.customer_enrichment_batch import run_customer_enrichment_batch

        gaps = [{"company_id": 1, "account_owner_id": None}]
        with (
            patch("app.services.customer_enrichment_batch.settings") as mock_settings,
            patch("app.services.customer_enrichment_batch.get_enrichment_gaps", return_value=gaps),
            patch("app.services.customer_enrichment_batch.can_use_credits", return_value=False),
        ):
            mock_settings.customer_enrichment_enabled = True
            result = await run_customer_enrichment_batch(db_session)
        assert result["processed"] == 0

    @pytest.mark.asyncio
    async def test_batch_zero_contacts_added(self, db_session):
        from app.services.customer_enrichment_batch import run_customer_enrichment_batch

        gaps = [{"company_id": 1, "account_owner_id": None}]
        with (
            patch("app.services.customer_enrichment_batch.settings") as mock_settings,
            patch("app.services.customer_enrichment_batch.get_enrichment_gaps", return_value=gaps),
            patch(
                "app.services.customer_enrichment_batch.enrich_customer_account",
                new_callable=AsyncMock,
                return_value={"ok": True, "contacts_added": 0},
            ),
        ):
            mock_settings.customer_enrichment_enabled = True
            result = await run_customer_enrichment_batch(db_session)
        assert result["enriched"] == 0
        assert result["processed"] == 1

    @pytest.mark.asyncio
    async def test_email_reverification_stub(self, db_session):
        from app.services.customer_enrichment_batch import run_email_reverification

        result = await run_email_reverification(db_session)
        assert result["status"] == "no_provider"
        assert result["processed"] == 0


# ── Calendar Intelligence ───────────────────────────────────────────────


class TestCalendarIntelligence:
    """Tests for app/services/calendar_intelligence.py."""

    @pytest.mark.asyncio
    async def test_scan_calendar_api_error(self, db_session, test_user):
        from app.services.calendar_intelligence import scan_calendar_events

        with _patch_graph_client(pages_side_effect=Exception("Graph API error")):
            result = await scan_calendar_events("token", test_user.id, db_session)
        assert result["events_scanned"] == 0

    @pytest.mark.asyncio
    async def test_scan_calendar_no_events(self, db_session, test_user):
        from app.services.calendar_intelligence import scan_calendar_events

        with _patch_graph_client(pages_return=[]):
            result = await scan_calendar_events("token", test_user.id, db_session)
        assert result["events_scanned"] == 0
        assert result["vendor_meetings"] == 0
        assert result["trade_shows"] == 0

    @pytest.mark.asyncio
    async def test_scan_calendar_vendor_meeting(self, db_session, test_user):
        from app.services.calendar_intelligence import scan_calendar_events

        events = [
            {
                "subject": "Quarterly Review",
                "attendees": [
                    {
                        "emailAddress": {
                            "address": "vendor@external.com",
                            "name": "Vendor Contact",
                        }
                    }
                ],
                "start": {"dateTime": "2026-03-20T10:00:00"},
                "end": {"dateTime": "2026-03-20T11:00:00"},
                "location": {"displayName": "Teams"},
            }
        ]
        with _patch_graph_client(pages_return=events):
            result = await scan_calendar_events("token", test_user.id, db_session)
        assert result["events_scanned"] == 1
        assert result["vendor_meetings"] == 1
        assert result["activities_logged"] == 1

    @pytest.mark.asyncio
    async def test_scan_calendar_trade_show(self, db_session, test_user):
        from app.services.calendar_intelligence import scan_calendar_events

        events = [
            {
                "subject": "Electronica 2026 Conference",
                "attendees": [],
                "start": {"dateTime": "2026-11-10T09:00:00"},
                "end": {"dateTime": "2026-11-13T17:00:00"},
                "location": {"displayName": "Munich"},
            }
        ]
        with _patch_graph_client(pages_return=events):
            result = await scan_calendar_events("token", test_user.id, db_session)
        assert result["trade_shows"] == 1
        assert result["activities_logged"] == 1

    @pytest.mark.asyncio
    async def test_scan_calendar_internal_only(self, db_session, test_user):
        from app.services.calendar_intelligence import scan_calendar_events

        events = [
            {
                "subject": "Team Standup",
                "attendees": [
                    {
                        "emailAddress": {
                            "address": "colleague@trioscs.com",
                            "name": "Colleague",
                        }
                    }
                ],
                "start": {"dateTime": "2026-03-20T09:00:00"},
                "end": {"dateTime": "2026-03-20T09:15:00"},
                "location": {},
            }
        ]
        with _patch_graph_client(pages_return=events):
            result = await scan_calendar_events("token", test_user.id, db_session)
        assert result["vendor_meetings"] == 0
        assert result["activities_logged"] == 0

    @pytest.mark.asyncio
    async def test_scan_calendar_dedup(self, db_session, test_user):
        from app.services.calendar_intelligence import scan_calendar_events

        events = [
            {
                "subject": "Vendor Sync",
                "attendees": [{"emailAddress": {"address": "v@ext.com", "name": "V"}}],
                "start": {"dateTime": "2026-03-20T10:00:00"},
                "end": {"dateTime": "2026-03-20T11:00:00"},
                "location": {},
            }
        ]
        with _patch_graph_client(pages_return=events):
            # First scan
            r1 = await scan_calendar_events("token", test_user.id, db_session)
            assert r1["activities_logged"] == 1
            # Second scan — should dedup
            r2 = await scan_calendar_events("token", test_user.id, db_session)
            assert r2["activities_logged"] == 0

    @pytest.mark.asyncio
    async def test_scan_calendar_commit_error(self, db_session, test_user):
        from app.services.calendar_intelligence import scan_calendar_events

        events = [
            {
                "subject": "Vendor Call",
                "attendees": [{"emailAddress": {"address": "v@ext.com", "name": "V"}}],
                "start": {"dateTime": "2026-03-20T10:00:00"},
                "end": {"dateTime": "2026-03-20T11:00:00"},
                "location": {},
            }
        ]
        # Use a mock db that raises on commit
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = None
        mock_db.add = MagicMock()
        mock_db.commit.side_effect = Exception("commit error")
        mock_db.rollback = MagicMock()

        with _patch_graph_client(pages_return=events):
            result = await scan_calendar_events("token", test_user.id, mock_db)
        mock_db.rollback.assert_called_once()

    @pytest.mark.asyncio
    async def test_scan_calendar_attendee_no_email(self, db_session, test_user):
        from app.services.calendar_intelligence import scan_calendar_events

        events = [
            {
                "subject": "Meeting",
                "attendees": [
                    {"emailAddress": {"address": "", "name": "No Email"}},
                    {"emailAddress": {}},
                ],
                "start": {"dateTime": "2026-03-20T10:00:00"},
                "end": {"dateTime": "2026-03-20T11:00:00"},
                "location": {},
            }
        ]
        with _patch_graph_client(pages_return=events):
            result = await scan_calendar_events("token", test_user.id, db_session)
        assert result["vendor_meetings"] == 0


# ── Customer Enrichment Service ─────────────────────────────────────────


class TestCustomerEnrichmentService:
    """Tests for app/services/customer_enrichment_service.py."""

    @pytest.mark.parametrize(
        "title, expected",
        [
            ("VP of Sales", "decision_maker"),
            ("Director", "decision_maker"),
            ("CEO", "decision_maker"),
            ("Chief Operating Officer", "decision_maker"),
            ("Senior Buyer", "buyer"),
            ("Procurement Manager", "buyer"),
            ("Supply Chain Lead", "buyer"),
            ("Design Engineer", "technical"),
            ("Quality Manager", "technical"),
            ("Office Manager", "operations"),
            (None, "unknown"),
            ("", "unknown"),
        ],
    )
    def test_classify_contact_role(self, db_session, title, expected):
        from app.services.customer_enrichment_service import _classify_contact_role

        assert _classify_contact_role(title) == expected

    @pytest.mark.parametrize(
        "phone_type, expected",
        [
            ("direct_dial", True),
            ("mobile", True),
            ("switchboard", False),
            (None, False),
        ],
    )
    def test_is_direct_dial(self, db_session, phone_type, expected):
        from app.services.customer_enrichment_service import _is_direct_dial

        assert _is_direct_dial(phone_type) is expected

    def test_contacts_needed(self, db_session, test_company, test_customer_site):
        from app.services.customer_enrichment_service import _contacts_needed

        # No contacts yet → full target needed
        needed = _contacts_needed(db_session, test_company.id, 5)
        assert needed == 5

    def test_contacts_needed_no_sites(self, db_session):
        from app.services.customer_enrichment_service import _contacts_needed

        needed = _contacts_needed(db_session, 99999, 5)
        assert needed == 5

    @pytest.mark.parametrize(
        "domain, website, expected",
        [
            ("example.com", None, "example.com"),
            (None, "https://www.example.com/about", "example.com"),
            (None, None, None),
            ("", "", None),
        ],
    )
    def test_get_company_domain(self, db_session, domain, website, expected):
        from app.services.customer_enrichment_service import _get_company_domain

        co = SimpleNamespace(domain=domain, website=website)
        assert _get_company_domain(co) == expected

    @pytest.mark.asyncio
    async def test_enrich_disabled(self, db_session):
        from app.services.customer_enrichment_service import enrich_customer_account

        with patch("app.services.customer_enrichment_service.settings") as mock_settings:
            mock_settings.customer_enrichment_enabled = False
            result = await enrich_customer_account(1, db_session)
        assert result["error"] == "Customer enrichment is disabled"

    @pytest.mark.asyncio
    async def test_enrich_company_not_found(self, db_session):
        from app.services.customer_enrichment_service import enrich_customer_account

        with patch("app.services.customer_enrichment_service.settings") as mock_settings:
            mock_settings.customer_enrichment_enabled = True
            result = await enrich_customer_account(99999, db_session)
        assert result["error"] == "Company not found"

    @pytest.mark.asyncio
    async def test_enrich_cooldown_active(self, db_session, test_company):
        from app.services.customer_enrichment_service import enrich_customer_account

        test_company.customer_enrichment_at = datetime.now(timezone.utc) - timedelta(days=1)
        db_session.flush()

        with patch("app.services.customer_enrichment_service.settings") as mock_settings:
            mock_settings.customer_enrichment_enabled = True
            mock_settings.customer_enrichment_cooldown_days = 90
            result = await enrich_customer_account(test_company.id, db_session)
        assert "Cooldown active" in result["error"]

    @pytest.mark.asyncio
    async def test_enrich_no_domain(self, db_session, test_company):
        from app.services.customer_enrichment_service import enrich_customer_account

        test_company.website = None
        test_company.domain = None
        db_session.flush()

        with patch("app.services.customer_enrichment_service.settings") as mock_settings:
            mock_settings.customer_enrichment_enabled = True
            mock_settings.customer_enrichment_cooldown_days = 90
            result = await enrich_customer_account(test_company.id, db_session)
        assert result["error"] == "No domain available"

    @pytest.mark.asyncio
    async def test_enrich_already_complete(self, db_session, test_company, test_customer_site):
        from app.models.crm import SiteContact
        from app.services.customer_enrichment_service import enrich_customer_account

        # Add enough contacts to meet target
        for i in range(5):
            db_session.add(
                SiteContact(
                    customer_site_id=test_customer_site.id,
                    full_name=f"Contact {i}",
                    is_active=True,
                )
            )
        db_session.flush()

        with patch("app.services.customer_enrichment_service.settings") as mock_settings:
            mock_settings.customer_enrichment_enabled = True
            mock_settings.customer_enrichment_cooldown_days = 90
            mock_settings.customer_enrichment_contacts_per_account = 5
            result = await enrich_customer_account(test_company.id, db_session)
        assert result["status"] == "already_complete"

    @pytest.mark.asyncio
    async def test_enrich_no_providers(self, db_session, test_company, test_customer_site):
        from app.services.customer_enrichment_service import enrich_customer_account

        with patch("app.services.customer_enrichment_service.settings") as mock_settings:
            mock_settings.customer_enrichment_enabled = True
            mock_settings.customer_enrichment_cooldown_days = 90
            mock_settings.customer_enrichment_contacts_per_account = 5
            result = await enrich_customer_account(test_company.id, db_session)
        assert result["status"] == "no_providers"

    def test_get_enrichment_gaps(self, db_session, test_company, test_customer_site):
        from app.services.customer_enrichment_service import get_enrichment_gaps

        gaps = get_enrichment_gaps(db_session, limit=10)
        # test_company has 0 contacts so it should appear
        company_ids = [g["company_id"] for g in gaps]
        assert test_company.id in company_ids

    def test_get_enrichment_gaps_empty(self, db_session):
        from app.services.customer_enrichment_service import get_enrichment_gaps

        gaps = get_enrichment_gaps(db_session, limit=10)
        assert gaps == []
