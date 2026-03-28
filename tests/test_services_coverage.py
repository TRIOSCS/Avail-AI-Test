"""Tests for small service modules with 0% coverage.

Covers: health_service, teams_notifications, mailbox_intelligence,
prospect_contacts, customer_analysis_service, customer_enrichment_batch,
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

from app.services.prospect_contacts import (
    _is_new_hire,
    _is_personal_email,
    classify_contact_seniority,
    mask_email,
)

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

    def test_is_within_working_hours_true(self, db_session):
        from app.services.mailbox_intelligence import is_within_working_hours

        user = SimpleNamespace(working_hours_start="08:00", working_hours_end="17:00")
        assert is_within_working_hours(user, 10) is True

    def test_is_within_working_hours_false(self, db_session):
        from app.services.mailbox_intelligence import is_within_working_hours

        user = SimpleNamespace(working_hours_start="08:00", working_hours_end="17:00")
        assert is_within_working_hours(user, 20) is False

    def test_is_within_working_hours_no_config(self, db_session):
        from app.services.mailbox_intelligence import is_within_working_hours

        user = SimpleNamespace(working_hours_start=None, working_hours_end=None)
        assert is_within_working_hours(user, 10) is True

    def test_is_within_working_hours_boundary(self, db_session):
        from app.services.mailbox_intelligence import is_within_working_hours

        user = SimpleNamespace(working_hours_start="08:00", working_hours_end="17:00")
        assert is_within_working_hours(user, 8) is True
        assert is_within_working_hours(user, 17) is False

    def test_is_within_working_hours_bad_format(self, db_session):
        from app.services.mailbox_intelligence import is_within_working_hours

        user = SimpleNamespace(working_hours_start="invalid", working_hours_end="also-bad")
        assert is_within_working_hours(user, 10) is True


# ── Prospect Contacts ───────────────────────────────────────────────────


class TestProspectContacts:
    """Tests for app/services/prospect_contacts.py."""

    def test_classify_decision_maker(self, db_session):
        assert classify_contact_seniority("VP of Sales") == "decision_maker"
        assert classify_contact_seniority("Director of Engineering") == "decision_maker"
        assert classify_contact_seniority("Chief Technology Officer") == "decision_maker"
        assert classify_contact_seniority("CEO") == "decision_maker"
        assert classify_contact_seniority("Head of Procurement") == "decision_maker"
        assert classify_contact_seniority("SVP Operations") == "decision_maker"
        assert classify_contact_seniority("EVP Sales") == "decision_maker"

    def test_classify_influencer(self, db_session):
        assert classify_contact_seniority("Senior Engineer") == "influencer"
        assert classify_contact_seniority("Project Manager") == "influencer"
        assert classify_contact_seniority("Team Lead") == "influencer"
        assert classify_contact_seniority("Commodity Manager") == "influencer"
        assert classify_contact_seniority("Principal Architect") == "influencer"

    def test_classify_executor(self, db_session):
        assert classify_contact_seniority("Buyer") == "executor"
        assert classify_contact_seniority("Purchasing Agent") == "executor"
        assert classify_contact_seniority("Procurement Coordinator") == "executor"
        assert classify_contact_seniority("Supply Chain Analyst") == "executor"
        assert classify_contact_seniority("Planning Specialist") == "executor"
        assert classify_contact_seniority("Planner") == "executor"
        assert classify_contact_seniority("Assistant Buyer") == "executor"

    def test_classify_other(self, db_session):
        assert classify_contact_seniority("Receptionist") == "other"
        assert classify_contact_seniority("") == "other"
        assert classify_contact_seniority(None) == "other"

    def test_mask_email_standard(self, db_session):
        assert mask_email("john.smith@company.com") == "j***@comp..."

    def test_mask_email_short_domain(self, db_session):
        assert mask_email("a@b.co") == "a***@b.co"

    def test_mask_email_empty(self, db_session):
        assert mask_email("") == ""
        assert mask_email(None) == ""

    def test_mask_email_no_at(self, db_session):
        assert mask_email("invalid") == ""

    def test_is_personal_email_true(self, db_session):
        assert _is_personal_email("user@gmail.com") is True
        assert _is_personal_email("user@yahoo.com") is True
        assert _is_personal_email("user@hotmail.com") is True
        assert _is_personal_email("user@protonmail.com") is True

    def test_is_personal_email_false(self, db_session):
        assert _is_personal_email("user@company.com") is False

    def test_is_personal_email_edge(self, db_session):
        assert _is_personal_email("") is False
        assert _is_personal_email(None) is False
        assert _is_personal_email("no-at-sign") is False

    def test_is_new_hire_recent(self, db_session):
        recent = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        assert _is_new_hire(recent) is True

    def test_is_new_hire_old(self, db_session):
        old = (datetime.now(timezone.utc) - timedelta(days=365)).isoformat()
        assert _is_new_hire(old) is False

    def test_is_new_hire_none(self, db_session):
        assert _is_new_hire(None) is False

    def test_is_new_hire_bad_format(self, db_session):
        assert _is_new_hire("not-a-date") is False

    def test_is_new_hire_z_suffix(self, db_session):
        recent = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        assert _is_new_hire(recent) is True


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
        from app.models import Requirement, Requisition
        from app.services.customer_analysis_service import analyze_customer_materials

        # Create a requisition linked to customer site
        req = Requisition(
            name="REQ-ANALYSIS",
            customer_name="Acme",
            customer_site_id=test_customer_site.id,
            status="active",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.flush()
        item = Requirement(
            requisition_id=req.id,
            primary_mpn="LM317T",
            brand="Texas Instruments",
            target_qty=100,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(item)
        db_session.flush()

        mock_result = {"brands": ["Texas Instruments"], "commodities": ["Voltage Regulators"]}
        with patch("app.utils.claude_client.claude_json", new_callable=AsyncMock, return_value=mock_result):
            await analyze_customer_materials(test_company.id, db_session=db_session)

        db_session.refresh(test_company)
        assert "Texas Instruments" in test_company.brand_tags
        assert "Voltage Regulators" in test_company.commodity_tags

    @pytest.mark.asyncio
    async def test_analyze_with_sightings(self, db_session, test_company, test_customer_site):
        from app.models import Requirement, Requisition, Sighting
        from app.services.customer_analysis_service import analyze_customer_materials

        req = Requisition(
            name="REQ-SIGHTING",
            customer_name="Acme",
            customer_site_id=test_customer_site.id,
            status="active",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.flush()
        item = Requirement(
            requisition_id=req.id,
            primary_mpn="IC-CHIP-001",
            brand="Intel",
            target_qty=50,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(item)
        db_session.flush()
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
        from app.models import Requirement, Requisition
        from app.services.customer_analysis_service import analyze_customer_materials

        req = Requisition(
            name="REQ-ANALYSIS2",
            customer_name="Acme",
            customer_site_id=test_customer_site.id,
            status="active",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.flush()
        item = Requirement(
            requisition_id=req.id,
            primary_mpn="LM317T",
            brand="TI",
            target_qty=100,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(item)
        db_session.flush()

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

        mock_gc = MagicMock()
        mock_gc.get_all_pages = AsyncMock(side_effect=Exception("Graph API error"))

        with patch("app.utils.graph_client.GraphClient", return_value=mock_gc):
            result = await scan_calendar_events("token", test_user.id, db_session)
        assert result["events_scanned"] == 0

    @pytest.mark.asyncio
    async def test_scan_calendar_no_events(self, db_session, test_user):
        from app.services.calendar_intelligence import scan_calendar_events

        mock_gc = MagicMock()
        mock_gc.get_all_pages = AsyncMock(return_value=[])

        with patch("app.utils.graph_client.GraphClient", return_value=mock_gc):
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
        mock_gc = MagicMock()
        mock_gc.get_all_pages = AsyncMock(return_value=events)

        with patch("app.utils.graph_client.GraphClient", return_value=mock_gc):
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
        mock_gc = MagicMock()
        mock_gc.get_all_pages = AsyncMock(return_value=events)

        with patch("app.utils.graph_client.GraphClient", return_value=mock_gc):
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
        mock_gc = MagicMock()
        mock_gc.get_all_pages = AsyncMock(return_value=events)

        with patch("app.utils.graph_client.GraphClient", return_value=mock_gc):
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
        mock_gc = MagicMock()
        mock_gc.get_all_pages = AsyncMock(return_value=events)

        with patch("app.utils.graph_client.GraphClient", return_value=mock_gc):
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
        mock_gc = MagicMock()
        mock_gc.get_all_pages = AsyncMock(return_value=events)

        # Use a mock db that raises on commit
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = None
        mock_db.add = MagicMock()
        mock_db.commit.side_effect = Exception("commit error")
        mock_db.rollback = MagicMock()

        with patch("app.utils.graph_client.GraphClient", return_value=mock_gc):
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
        mock_gc = MagicMock()
        mock_gc.get_all_pages = AsyncMock(return_value=events)

        with patch("app.utils.graph_client.GraphClient", return_value=mock_gc):
            result = await scan_calendar_events("token", test_user.id, db_session)
        assert result["vendor_meetings"] == 0


# ── Customer Enrichment Service ─────────────────────────────────────────


class TestCustomerEnrichmentService:
    """Tests for app/services/customer_enrichment_service.py."""

    def test_classify_contact_role_decision_maker(self, db_session):
        from app.services.customer_enrichment_service import _classify_contact_role

        assert _classify_contact_role("VP of Sales") == "decision_maker"
        assert _classify_contact_role("Director") == "decision_maker"
        assert _classify_contact_role("CEO") == "decision_maker"
        assert _classify_contact_role("Chief Operating Officer") == "decision_maker"

    def test_classify_contact_role_buyer(self, db_session):
        from app.services.customer_enrichment_service import _classify_contact_role

        assert _classify_contact_role("Senior Buyer") == "buyer"
        assert _classify_contact_role("Procurement Manager") == "buyer"
        assert _classify_contact_role("Supply Chain Lead") == "buyer"

    def test_classify_contact_role_technical(self, db_session):
        from app.services.customer_enrichment_service import _classify_contact_role

        assert _classify_contact_role("Design Engineer") == "technical"
        assert _classify_contact_role("Quality Manager") == "technical"

    def test_classify_contact_role_operations(self, db_session):
        from app.services.customer_enrichment_service import _classify_contact_role

        assert _classify_contact_role("Office Manager") == "operations"

    def test_classify_contact_role_unknown(self, db_session):
        from app.services.customer_enrichment_service import _classify_contact_role

        assert _classify_contact_role(None) == "unknown"
        assert _classify_contact_role("") == "unknown"

    def test_is_direct_dial(self, db_session):
        from app.services.customer_enrichment_service import _is_direct_dial

        assert _is_direct_dial("direct_dial") is True
        assert _is_direct_dial("mobile") is True
        assert _is_direct_dial("switchboard") is False
        assert _is_direct_dial(None) is False

    def test_contacts_needed(self, db_session, test_company, test_customer_site):
        from app.services.customer_enrichment_service import _contacts_needed

        # No contacts yet → full target needed
        needed = _contacts_needed(db_session, test_company.id, 5)
        assert needed == 5

    def test_contacts_needed_no_sites(self, db_session):
        from app.services.customer_enrichment_service import _contacts_needed

        needed = _contacts_needed(db_session, 99999, 5)
        assert needed == 5

    def test_get_company_domain(self, db_session):
        from app.services.customer_enrichment_service import _get_company_domain

        co = SimpleNamespace(domain="example.com", website=None)
        assert _get_company_domain(co) == "example.com"

        co2 = SimpleNamespace(domain=None, website="https://www.example.com/about")
        assert _get_company_domain(co2) == "example.com"

        co3 = SimpleNamespace(domain=None, website=None)
        assert _get_company_domain(co3) is None

        co4 = SimpleNamespace(domain="", website="")
        assert _get_company_domain(co4) is None

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
