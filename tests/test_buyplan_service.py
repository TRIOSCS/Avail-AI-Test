"""
test_buyplan_service.py -- Unit tests for app/services/buyplan_service.py

Covers the service-layer functions that handle buy plan lifecycle:
- log_buyplan_activity: audit trail creation
- notify_buyplan_submitted: email + Teams + in-app for admins
- notify_buyplan_approved: email + Teams + in-app for buyers
- notify_buyplan_rejected: email + Teams + in-app for submitter
- notify_stock_sale_approved: stock sale notification path
- notify_buyplan_completed: completion notifications
- notify_buyplan_cancelled: cancellation notifications
- verify_po_sent: PO email verification via Graph API
- auto_complete_stock_sales: safety net for stuck stock sales
- run_buyplan_bg: fire-and-forget background helper

All external calls (Graph API, Teams webhooks) are mocked.
Uses in-memory SQLite via conftest fixtures.

Called by: pytest
Depends on: app/services/buyplan_service.py, conftest.py
"""

import secrets
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models import ActivityLog, BuyPlan, Offer, User
from app.services.buyplan_service import (
    _post_teams_channel,
    _send_teams_dm,
    auto_complete_stock_sales,
    log_buyplan_activity,
    notify_buyplan_approved,
    notify_buyplan_cancelled,
    notify_buyplan_completed,
    notify_buyplan_rejected,
    notify_buyplan_submitted,
    notify_stock_sale_approved,
    run_buyplan_bg,
    verify_po_sent,
)

# Patch targets — these are imported *inside* functions via deferred imports,
# so we patch at the source module, not in buyplan_service namespace.
_PATCH_TOKEN = "app.scheduler.get_valid_token"
_PATCH_GC = "app.utils.graph_client.GraphClient"
_PATCH_TEAMS_CH = "app.services.buyplan_service._post_teams_channel"
_PATCH_TEAMS_DM = "app.services.buyplan_service._send_teams_dm"
_PATCH_SETTINGS = "app.services.buyplan_service.settings"


# ── Helpers ──────────────────────────────────────────────────────────


def _create_plan(db: Session, **overrides) -> BuyPlan:
    """Insert a BuyPlan directly for test setup."""
    defaults = {
        "status": "pending_approval",
        "line_items": [
            {
                "offer_id": 1,
                "mpn": "LM317T",
                "vendor_name": "Arrow Electronics",
                "qty": 1000,
                "plan_qty": 1000,
                "cost_price": 0.50,
                "lead_time": "2 weeks",
                "condition": "new",
                "entered_by_id": None,
                "po_number": None,
                "po_entered_at": None,
                "po_sent_at": None,
                "po_recipient": None,
                "po_verified": False,
            }
        ],
        "approval_token": secrets.token_urlsafe(32),
        "submitted_at": datetime.now(timezone.utc),
    }
    defaults.update(overrides)
    plan = BuyPlan(**defaults)
    db.add(plan)
    db.commit()
    db.refresh(plan)
    return plan


def _count_activities(db: Session, activity_type: str) -> int:
    """Count ActivityLog rows with the given type."""
    return db.query(ActivityLog).filter(ActivityLog.activity_type == activity_type).count()


def _get_activities(db: Session, activity_type: str) -> list[ActivityLog]:
    """Retrieve all ActivityLog rows with the given type."""
    return db.query(ActivityLog).filter(ActivityLog.activity_type == activity_type).all()


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture()
def admin_in_settings(admin_user):
    """Ensure admin_user.email is in settings.admin_emails so notify functions find them."""
    with patch(_PATCH_SETTINGS) as mock_settings:
        mock_settings.admin_emails = [admin_user.email]
        mock_settings.app_url = "https://avail.test"
        mock_settings.stock_sale_notify_emails = ["logistics@test.com"]
        yield mock_settings


# ── 1. log_buyplan_activity ──────────────────────────────────────────


class TestLogBuyplanActivity:
    def test_creates_activity_log(self, db_session, test_user, test_requisition, test_quote):
        plan = _create_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=test_user.id,
        )
        log_buyplan_activity(db_session, test_user.id, plan, "buyplan_submitted", "submitted for approval")
        db_session.commit()

        logs = _get_activities(db_session, "buyplan_submitted")
        assert len(logs) == 1
        assert logs[0].user_id == test_user.id
        assert f"#{plan.id}" in logs[0].subject
        assert "submitted for approval" in logs[0].subject
        assert logs[0].channel == "system"
        assert f"plan_id={plan.id}" in logs[0].notes
        assert f"status={plan.status}" in logs[0].notes

    def test_empty_detail(self, db_session, test_user, test_requisition, test_quote):
        plan = _create_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=test_user.id,
        )
        log_buyplan_activity(db_session, test_user.id, plan, "buyplan_test")
        db_session.commit()

        logs = _get_activities(db_session, "buyplan_test")
        assert len(logs) == 1
        assert logs[0].subject == f"Buy plan #{plan.id}"

    def test_requisition_id_linked(self, db_session, test_user, test_requisition, test_quote):
        plan = _create_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=test_user.id,
        )
        log_buyplan_activity(db_session, test_user.id, plan, "buyplan_linked")
        db_session.commit()

        logs = _get_activities(db_session, "buyplan_linked")
        assert logs[0].requisition_id == test_requisition.id


# ── 2. notify_buyplan_submitted ──────────────────────────────────────


class TestNotifyBuyplanSubmitted:
    @pytest.mark.asyncio
    async def test_creates_in_app_notifications_for_admins(
        self, db_session, test_user, admin_user, test_requisition, test_quote, admin_in_settings,
    ):
        plan = _create_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=test_user.id,
        )

        with patch(_PATCH_TOKEN, new_callable=AsyncMock, return_value=None), \
             patch(_PATCH_TEAMS_CH, new_callable=AsyncMock), \
             patch(_PATCH_TEAMS_DM, new_callable=AsyncMock):
            await notify_buyplan_submitted(plan, db_session)

        logs = _get_activities(db_session, "buyplan_pending")
        assert len(logs) == 1
        assert logs[0].user_id == admin_user.id
        assert f"#{plan.id}" in logs[0].subject
        assert test_user.name in logs[0].subject

    @pytest.mark.asyncio
    async def test_sends_email_to_admin(
        self, db_session, test_user, admin_user, test_requisition, test_quote, admin_in_settings,
    ):
        plan = _create_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=test_user.id,
            salesperson_notes="Rush order",
        )

        mock_gc_instance = AsyncMock()
        with patch(_PATCH_TOKEN, new_callable=AsyncMock, return_value="tok-123"), \
             patch(_PATCH_GC, return_value=mock_gc_instance) as MockGC, \
             patch(_PATCH_TEAMS_CH, new_callable=AsyncMock), \
             patch(_PATCH_TEAMS_DM, new_callable=AsyncMock):
            await notify_buyplan_submitted(plan, db_session)

        MockGC.assert_called_with("tok-123")
        mock_gc_instance.post_json.assert_awaited_once()
        call_args = mock_gc_instance.post_json.call_args
        assert call_args[0][0] == "/me/sendMail"
        msg = call_args[0][1]["message"]
        assert admin_user.email in msg["toRecipients"][0]["emailAddress"]["address"]
        assert "Approval Required" in msg["subject"]

    @pytest.mark.asyncio
    async def test_posts_to_teams_channel(
        self, db_session, test_user, admin_user, test_requisition, test_quote, admin_in_settings,
    ):
        plan = _create_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=test_user.id,
        )

        mock_teams_channel = AsyncMock()
        mock_teams_dm = AsyncMock()
        with patch(_PATCH_TOKEN, new_callable=AsyncMock, return_value=None), \
             patch(_PATCH_TEAMS_CH, mock_teams_channel), \
             patch(_PATCH_TEAMS_DM, mock_teams_dm):
            await notify_buyplan_submitted(plan, db_session)

        mock_teams_channel.assert_awaited_once()
        channel_msg = mock_teams_channel.call_args[0][0]
        assert "Approval Required" in channel_msg
        assert str(plan.id) in channel_msg

        mock_teams_dm.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_handles_missing_submitter(
        self, db_session, admin_user, test_requisition, test_quote, admin_in_settings,
    ):
        """Plan with a null submitted_by_id should not crash."""
        plan = _create_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=None,
        )

        with patch(_PATCH_TOKEN, new_callable=AsyncMock, return_value=None), \
             patch(_PATCH_TEAMS_CH, new_callable=AsyncMock), \
             patch(_PATCH_TEAMS_DM, new_callable=AsyncMock):
            await notify_buyplan_submitted(plan, db_session)

        logs = _get_activities(db_session, "buyplan_pending")
        assert len(logs) == 1
        assert "Unknown" in logs[0].subject

    @pytest.mark.asyncio
    async def test_html_includes_line_items(
        self, db_session, test_user, admin_user, test_requisition, test_quote, admin_in_settings,
    ):
        plan = _create_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=test_user.id,
            line_items=[
                {"mpn": "ABC123", "vendor_name": "DigiKey", "plan_qty": 500, "cost_price": 1.25, "lead_time": "3 days"},
                {"mpn": "DEF456", "vendor_name": "Mouser", "plan_qty": 200, "cost_price": 2.50, "lead_time": "1 week"},
            ],
        )

        mock_gc = AsyncMock()
        with patch(_PATCH_TOKEN, new_callable=AsyncMock, return_value="tok"), \
             patch(_PATCH_GC, return_value=mock_gc), \
             patch(_PATCH_TEAMS_CH, new_callable=AsyncMock), \
             patch(_PATCH_TEAMS_DM, new_callable=AsyncMock):
            await notify_buyplan_submitted(plan, db_session)

        call_args = mock_gc.post_json.call_args[0][1]
        html_body = call_args["message"]["body"]["content"]
        assert "ABC123" in html_body
        assert "DEF456" in html_body
        assert "DigiKey" in html_body
        assert "Mouser" in html_body

    @pytest.mark.asyncio
    async def test_salesperson_notes_in_html(
        self, db_session, test_user, admin_user, test_requisition, test_quote, admin_in_settings,
    ):
        plan = _create_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=test_user.id,
            salesperson_notes="Critical timeline - customer needs by Friday",
        )

        mock_gc = AsyncMock()
        with patch(_PATCH_TOKEN, new_callable=AsyncMock, return_value="tok"), \
             patch(_PATCH_GC, return_value=mock_gc), \
             patch(_PATCH_TEAMS_CH, new_callable=AsyncMock), \
             patch(_PATCH_TEAMS_DM, new_callable=AsyncMock):
            await notify_buyplan_submitted(plan, db_session)

        html_body = mock_gc.post_json.call_args[0][1]["message"]["body"]["content"]
        assert "Critical timeline" in html_body
        assert "Salesperson Notes" in html_body


# ── 3. notify_buyplan_approved ───────────────────────────────────────


class TestNotifyBuyplanApproved:
    @pytest.mark.asyncio
    async def test_notifies_buyer(
        self, db_session, test_user, admin_user, test_requisition, test_quote,
    ):
        """Buyer who entered offers gets in-app notification."""
        plan = _create_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=test_user.id,
            approved_by_id=admin_user.id,
            status="approved",
            line_items=[
                {
                    "offer_id": 1, "mpn": "LM317T", "vendor_name": "Arrow",
                    "qty": 1000, "plan_qty": 1000, "cost_price": 0.50,
                    "entered_by_id": test_user.id,
                    "po_number": None, "po_verified": False,
                }
            ],
        )

        mock_gc = AsyncMock()
        with patch(_PATCH_SETTINGS) as mock_settings, \
             patch(_PATCH_TOKEN, new_callable=AsyncMock, return_value="tok"), \
             patch(_PATCH_GC, return_value=mock_gc), \
             patch(_PATCH_TEAMS_CH, new_callable=AsyncMock), \
             patch(_PATCH_TEAMS_DM, new_callable=AsyncMock):
            mock_settings.app_url = "https://avail.test"
            await notify_buyplan_approved(plan, db_session)

        logs = _get_activities(db_session, "buyplan_approved")
        assert len(logs) == 1
        assert logs[0].user_id == test_user.id
        assert "create POs" in logs[0].subject

    @pytest.mark.asyncio
    async def test_fallback_to_offer_entered_by(
        self, db_session, test_user, admin_user, test_requisition, test_quote, test_offer,
    ):
        """When line items lack entered_by_id, falls back to offer's entered_by_id."""
        plan = _create_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=test_user.id,
            approved_by_id=admin_user.id,
            status="approved",
            line_items=[
                {
                    "offer_id": test_offer.id, "mpn": "LM317T", "vendor_name": "Arrow",
                    "qty": 1000, "plan_qty": 1000, "cost_price": 0.50,
                    "po_number": None, "po_verified": False,
                }
            ],
        )

        mock_gc = AsyncMock()
        with patch(_PATCH_SETTINGS) as mock_settings, \
             patch(_PATCH_TOKEN, new_callable=AsyncMock, return_value="tok"), \
             patch(_PATCH_GC, return_value=mock_gc), \
             patch(_PATCH_TEAMS_CH, new_callable=AsyncMock), \
             patch(_PATCH_TEAMS_DM, new_callable=AsyncMock):
            mock_settings.app_url = "https://avail.test"
            await notify_buyplan_approved(plan, db_session)

        logs = _get_activities(db_session, "buyplan_approved")
        assert len(logs) == 1
        assert logs[0].user_id == test_user.id

    @pytest.mark.asyncio
    async def test_sends_email_to_buyer(
        self, db_session, test_user, admin_user, test_requisition, test_quote,
    ):
        plan = _create_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=admin_user.id,
            approved_by_id=admin_user.id,
            status="approved",
            line_items=[
                {
                    "offer_id": 1, "mpn": "LM317T", "vendor_name": "Arrow",
                    "qty": 1000, "plan_qty": 1000, "cost_price": 0.50,
                    "entered_by_id": test_user.id,
                    "po_number": None, "po_verified": False,
                }
            ],
        )

        mock_gc = AsyncMock()
        with patch(_PATCH_SETTINGS) as mock_settings, \
             patch(_PATCH_TOKEN, new_callable=AsyncMock, return_value="tok"), \
             patch(_PATCH_GC, return_value=mock_gc), \
             patch(_PATCH_TEAMS_CH, new_callable=AsyncMock), \
             patch(_PATCH_TEAMS_DM, new_callable=AsyncMock):
            mock_settings.app_url = "https://avail.test"
            await notify_buyplan_approved(plan, db_session)

        mock_gc.post_json.assert_awaited_once()
        call_args = mock_gc.post_json.call_args[0]
        assert call_args[0] == "/me/sendMail"
        msg = call_args[1]["message"]
        assert test_user.email in msg["toRecipients"][0]["emailAddress"]["address"]
        assert "PO Required" in msg["subject"]

    @pytest.mark.asyncio
    async def test_posts_teams_channel(
        self, db_session, test_user, admin_user, test_requisition, test_quote,
    ):
        plan = _create_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=admin_user.id,
            approved_by_id=admin_user.id,
            status="approved",
            line_items=[
                {
                    "offer_id": 1, "mpn": "LM317T", "vendor_name": "Arrow",
                    "qty": 1000, "plan_qty": 1000, "cost_price": 0.50,
                    "entered_by_id": test_user.id,
                    "po_number": None, "po_verified": False,
                }
            ],
        )

        mock_teams = AsyncMock()
        with patch(_PATCH_SETTINGS) as mock_settings, \
             patch(_PATCH_TOKEN, new_callable=AsyncMock, return_value=None), \
             patch(_PATCH_TEAMS_CH, mock_teams), \
             patch(_PATCH_TEAMS_DM, new_callable=AsyncMock):
            mock_settings.app_url = "https://avail.test"
            await notify_buyplan_approved(plan, db_session)

        mock_teams.assert_awaited_once()
        msg = mock_teams.call_args[0][0]
        assert "Approved" in msg

    @pytest.mark.asyncio
    async def test_manager_notes_in_email(
        self, db_session, test_user, admin_user, test_requisition, test_quote,
    ):
        plan = _create_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=admin_user.id,
            approved_by_id=admin_user.id,
            status="approved",
            manager_notes="Priority customer - expedite",
            line_items=[
                {
                    "offer_id": 1, "mpn": "LM317T", "vendor_name": "Arrow",
                    "qty": 1000, "plan_qty": 1000, "cost_price": 0.50,
                    "entered_by_id": test_user.id,
                    "po_number": None, "po_verified": False,
                }
            ],
        )

        mock_gc = AsyncMock()
        with patch(_PATCH_SETTINGS) as mock_settings, \
             patch(_PATCH_TOKEN, new_callable=AsyncMock, return_value="tok"), \
             patch(_PATCH_GC, return_value=mock_gc), \
             patch(_PATCH_TEAMS_CH, new_callable=AsyncMock), \
             patch(_PATCH_TEAMS_DM, new_callable=AsyncMock):
            mock_settings.app_url = "https://avail.test"
            await notify_buyplan_approved(plan, db_session)

        html_body = mock_gc.post_json.call_args[0][1]["message"]["body"]["content"]
        assert "Priority customer" in html_body
        assert "Manager Notes" in html_body

    @pytest.mark.asyncio
    async def test_no_buyers_found(
        self, db_session, admin_user, test_requisition, test_quote,
    ):
        """Plan with no identifiable buyers should not crash."""
        plan = _create_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=admin_user.id,
            approved_by_id=admin_user.id,
            status="approved",
            line_items=[
                {
                    "offer_id": 99999, "mpn": "LM317T", "vendor_name": "Arrow",
                    "qty": 1000, "plan_qty": 1000, "cost_price": 0.50,
                    "po_number": None, "po_verified": False,
                }
            ],
        )

        with patch(_PATCH_SETTINGS) as mock_settings, \
             patch(_PATCH_TOKEN, new_callable=AsyncMock, return_value=None), \
             patch(_PATCH_TEAMS_CH, new_callable=AsyncMock), \
             patch(_PATCH_TEAMS_DM, new_callable=AsyncMock):
            mock_settings.app_url = "https://avail.test"
            await notify_buyplan_approved(plan, db_session)

        logs = _get_activities(db_session, "buyplan_approved")
        assert len(logs) == 0


# ── 4. notify_buyplan_rejected ───────────────────────────────────────


class TestNotifyBuyplanRejected:
    @pytest.mark.asyncio
    async def test_creates_in_app_notification(
        self, db_session, test_user, admin_user, test_requisition, test_quote,
    ):
        plan = _create_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=test_user.id,
            approved_by_id=admin_user.id,
            status="rejected",
            rejection_reason="Price too high",
        )

        with patch(_PATCH_SETTINGS) as mock_settings, \
             patch(_PATCH_TOKEN, new_callable=AsyncMock, return_value=None), \
             patch(_PATCH_TEAMS_CH, new_callable=AsyncMock), \
             patch(_PATCH_TEAMS_DM, new_callable=AsyncMock):
            mock_settings.app_url = "https://avail.test"
            await notify_buyplan_rejected(plan, db_session)

        logs = _get_activities(db_session, "buyplan_rejected")
        assert len(logs) == 1
        assert logs[0].user_id == test_user.id
        assert "Price too high" in logs[0].subject

    @pytest.mark.asyncio
    async def test_sends_rejection_email(
        self, db_session, test_user, admin_user, test_requisition, test_quote,
    ):
        plan = _create_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=test_user.id,
            approved_by_id=admin_user.id,
            status="rejected",
            rejection_reason="Wrong vendor selected",
        )

        mock_gc = AsyncMock()
        with patch(_PATCH_SETTINGS) as mock_settings, \
             patch(_PATCH_TOKEN, new_callable=AsyncMock, return_value="tok"), \
             patch(_PATCH_GC, return_value=mock_gc), \
             patch(_PATCH_TEAMS_CH, new_callable=AsyncMock), \
             patch(_PATCH_TEAMS_DM, new_callable=AsyncMock):
            mock_settings.app_url = "https://avail.test"
            await notify_buyplan_rejected(plan, db_session)

        mock_gc.post_json.assert_awaited_once()
        msg = mock_gc.post_json.call_args[0][1]["message"]
        assert test_user.email in msg["toRecipients"][0]["emailAddress"]["address"]
        assert "Rejected" in msg["subject"]
        assert "Wrong vendor" in msg["body"]["content"]

    @pytest.mark.asyncio
    async def test_sends_teams_dm(
        self, db_session, test_user, admin_user, test_requisition, test_quote,
    ):
        plan = _create_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=test_user.id,
            approved_by_id=admin_user.id,
            status="rejected",
            rejection_reason="Budget exceeded",
        )

        mock_dm = AsyncMock()
        with patch(_PATCH_SETTINGS) as mock_settings, \
             patch(_PATCH_TOKEN, new_callable=AsyncMock, return_value=None), \
             patch(_PATCH_TEAMS_CH, new_callable=AsyncMock), \
             patch(_PATCH_TEAMS_DM, mock_dm):
            mock_settings.app_url = "https://avail.test"
            await notify_buyplan_rejected(plan, db_session)

        mock_dm.assert_awaited_once()
        dm_msg = mock_dm.call_args[0][1]
        assert "rejected" in dm_msg.lower()
        assert "Budget exceeded" in dm_msg

    @pytest.mark.asyncio
    async def test_no_reason_given(
        self, db_session, test_user, admin_user, test_requisition, test_quote,
    ):
        plan = _create_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=test_user.id,
            approved_by_id=admin_user.id,
            status="rejected",
            rejection_reason=None,
        )

        with patch(_PATCH_SETTINGS) as mock_settings, \
             patch(_PATCH_TOKEN, new_callable=AsyncMock, return_value=None), \
             patch(_PATCH_TEAMS_CH, new_callable=AsyncMock), \
             patch(_PATCH_TEAMS_DM, new_callable=AsyncMock):
            mock_settings.app_url = "https://avail.test"
            await notify_buyplan_rejected(plan, db_session)

        logs = _get_activities(db_session, "buyplan_rejected")
        assert len(logs) == 1
        assert "no reason given" in logs[0].subject

    @pytest.mark.asyncio
    async def test_missing_submitter_returns_early(
        self, db_session, admin_user, test_requisition, test_quote,
    ):
        """If submitter not found (null), function returns early without error."""
        plan = _create_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=None,
            approved_by_id=admin_user.id,
            status="rejected",
        )

        with patch(_PATCH_SETTINGS) as mock_settings, \
             patch(_PATCH_TOKEN, new_callable=AsyncMock) as mock_token, \
             patch(_PATCH_TEAMS_CH, new_callable=AsyncMock), \
             patch(_PATCH_TEAMS_DM, new_callable=AsyncMock):
            mock_settings.app_url = "https://avail.test"
            await notify_buyplan_rejected(plan, db_session)

        mock_token.assert_not_awaited()
        logs = _get_activities(db_session, "buyplan_rejected")
        assert len(logs) == 0


# ── 5. notify_stock_sale_approved ────────────────────────────────────


class TestNotifyStockSaleApproved:
    @pytest.mark.asyncio
    async def test_creates_submitter_notification(
        self, db_session, test_user, admin_user, test_requisition, test_quote, admin_in_settings,
    ):
        plan = _create_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=test_user.id,
            approved_by_id=admin_user.id,
            status="complete",
            is_stock_sale=True,
        )

        with patch(_PATCH_TOKEN, new_callable=AsyncMock, return_value=None), \
             patch(_PATCH_TEAMS_CH, new_callable=AsyncMock):
            await notify_stock_sale_approved(plan, db_session)

        logs = _get_activities(db_session, "buyplan_completed")
        assert len(logs) == 1
        assert logs[0].user_id == test_user.id
        assert "no PO required" in logs[0].subject

    @pytest.mark.asyncio
    async def test_sends_stock_sale_emails(
        self, db_session, test_user, admin_user, test_requisition, test_quote, admin_in_settings,
    ):
        admin_user.access_token = "existing-token"
        db_session.commit()

        plan = _create_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=test_user.id,
            approved_by_id=admin_user.id,
            status="complete",
            is_stock_sale=True,
        )

        mock_gc = AsyncMock()
        with patch(_PATCH_TOKEN, new_callable=AsyncMock, return_value="tok"), \
             patch(_PATCH_GC, return_value=mock_gc), \
             patch(_PATCH_TEAMS_CH, new_callable=AsyncMock):
            await notify_stock_sale_approved(plan, db_session)

        assert mock_gc.post_json.await_count == 1
        msg = mock_gc.post_json.call_args[0][1]["message"]
        assert "Stock Sale Approved" in msg["subject"]

    @pytest.mark.asyncio
    async def test_posts_teams_channel(
        self, db_session, test_user, admin_user, test_requisition, test_quote, admin_in_settings,
    ):
        plan = _create_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=test_user.id,
            approved_by_id=admin_user.id,
            status="complete",
            is_stock_sale=True,
        )

        mock_teams = AsyncMock()
        with patch(_PATCH_TOKEN, new_callable=AsyncMock, return_value=None), \
             patch(_PATCH_TEAMS_CH, mock_teams):
            await notify_stock_sale_approved(plan, db_session)

        mock_teams.assert_awaited_once()
        msg = mock_teams.call_args[0][0]
        assert "Stock Sale" in msg
        assert "No PO required" in msg

    @pytest.mark.asyncio
    async def test_missing_submitter_still_works(
        self, db_session, admin_user, test_requisition, test_quote, admin_in_settings,
    ):
        """When submitted_by_id is null, no crash and no in-app notification."""
        plan = _create_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=None,
            approved_by_id=admin_user.id,
            status="complete",
            is_stock_sale=True,
        )

        with patch(_PATCH_TOKEN, new_callable=AsyncMock, return_value=None), \
             patch(_PATCH_TEAMS_CH, new_callable=AsyncMock):
            await notify_stock_sale_approved(plan, db_session)

        logs = _get_activities(db_session, "buyplan_completed")
        assert len(logs) == 0


# ── 6. notify_buyplan_completed ──────────────────────────────────────


class TestNotifyBuyplanCompleted:
    @pytest.mark.asyncio
    async def test_creates_in_app_notification(
        self, db_session, test_user, test_requisition, test_quote,
    ):
        plan = _create_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=test_user.id,
            status="complete",
        )

        with patch(_PATCH_SETTINGS) as mock_settings, \
             patch(_PATCH_TOKEN, new_callable=AsyncMock, return_value=None), \
             patch(_PATCH_TEAMS_CH, new_callable=AsyncMock):
            mock_settings.app_url = "https://avail.test"
            await notify_buyplan_completed(plan, db_session, "Admin User")

        logs = _get_activities(db_session, "buyplan_completed")
        assert len(logs) == 1
        assert logs[0].user_id == test_user.id
        assert "completed" in logs[0].subject

    @pytest.mark.asyncio
    async def test_sends_completion_email(
        self, db_session, test_user, test_requisition, test_quote,
    ):
        plan = _create_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=test_user.id,
            status="complete",
            sales_order_number="SO-999",
        )

        mock_gc = AsyncMock()
        with patch(_PATCH_SETTINGS) as mock_settings, \
             patch(_PATCH_TOKEN, new_callable=AsyncMock, return_value="tok"), \
             patch(_PATCH_GC, return_value=mock_gc), \
             patch(_PATCH_TEAMS_CH, new_callable=AsyncMock):
            mock_settings.app_url = "https://avail.test"
            await notify_buyplan_completed(plan, db_session, "Manager Jones")

        mock_gc.post_json.assert_awaited_once()
        msg = mock_gc.post_json.call_args[0][1]["message"]
        assert "Complete" in msg["subject"]
        assert "Manager Jones" in msg["body"]["content"]
        assert "SO-999" in msg["body"]["content"]

    @pytest.mark.asyncio
    async def test_posts_teams_channel(
        self, db_session, test_user, test_requisition, test_quote,
    ):
        plan = _create_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=test_user.id,
            status="complete",
        )

        mock_teams = AsyncMock()
        with patch(_PATCH_SETTINGS) as mock_settings, \
             patch(_PATCH_TOKEN, new_callable=AsyncMock, return_value=None), \
             patch(_PATCH_TEAMS_CH, mock_teams):
            mock_settings.app_url = "https://avail.test"
            await notify_buyplan_completed(plan, db_session, "Admin User")

        mock_teams.assert_awaited_once()
        msg = mock_teams.call_args[0][0]
        assert "Complete" in msg
        assert "Admin User" in msg

    @pytest.mark.asyncio
    async def test_missing_submitter_returns_early(
        self, db_session, test_requisition, test_quote,
    ):
        """When submitted_by_id is null, notify_buyplan_completed returns early."""
        plan = _create_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=None,
            status="complete",
        )

        with patch(_PATCH_SETTINGS) as mock_settings, \
             patch(_PATCH_TOKEN, new_callable=AsyncMock) as mock_token, \
             patch(_PATCH_TEAMS_CH, new_callable=AsyncMock):
            mock_settings.app_url = "https://avail.test"
            await notify_buyplan_completed(plan, db_session, "Admin")

        mock_token.assert_not_awaited()
        assert _count_activities(db_session, "buyplan_completed") == 0


# ── 7. notify_buyplan_cancelled ──────────────────────────────────────


class TestNotifyBuyplanCancelled:
    @pytest.mark.asyncio
    async def test_submitter_cancels_notifies_admins(
        self, db_session, test_user, admin_user, test_requisition, test_quote, admin_in_settings,
    ):
        """When submitter cancels their own plan, admins are notified."""
        plan = _create_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=test_user.id,
            cancelled_by_id=test_user.id,
            status="cancelled",
            cancellation_reason="Customer backed out",
        )

        with patch(_PATCH_TEAMS_CH, new_callable=AsyncMock):
            await notify_buyplan_cancelled(plan, db_session)

        logs = _get_activities(db_session, "buyplan_cancelled")
        assert len(logs) == 1
        assert logs[0].user_id == admin_user.id
        assert "Customer backed out" in logs[0].subject
        assert test_user.name in logs[0].subject

    @pytest.mark.asyncio
    async def test_admin_cancels_notifies_submitter(
        self, db_session, test_user, admin_user, test_requisition, test_quote,
    ):
        """When admin cancels, the original submitter is notified."""
        plan = _create_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=test_user.id,
            cancelled_by_id=admin_user.id,
            status="cancelled",
            cancellation_reason="Vendor unreliable",
        )

        with patch(_PATCH_SETTINGS) as mock_settings, \
             patch(_PATCH_TEAMS_CH, new_callable=AsyncMock):
            mock_settings.admin_emails = [admin_user.email]
            mock_settings.app_url = "https://avail.test"
            await notify_buyplan_cancelled(plan, db_session)

        logs = _get_activities(db_session, "buyplan_cancelled")
        assert len(logs) == 1
        assert logs[0].user_id == test_user.id
        assert "Vendor unreliable" in logs[0].subject

    @pytest.mark.asyncio
    async def test_no_reason(
        self, db_session, test_user, admin_user, test_requisition, test_quote,
    ):
        plan = _create_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=test_user.id,
            cancelled_by_id=admin_user.id,
            status="cancelled",
            cancellation_reason=None,
        )

        with patch(_PATCH_SETTINGS) as mock_settings, \
             patch(_PATCH_TEAMS_CH, new_callable=AsyncMock):
            mock_settings.admin_emails = [admin_user.email]
            mock_settings.app_url = "https://avail.test"
            await notify_buyplan_cancelled(plan, db_session)

        logs = _get_activities(db_session, "buyplan_cancelled")
        assert len(logs) == 1
        assert logs[0].subject.endswith(admin_user.name)

    @pytest.mark.asyncio
    async def test_posts_teams_channel(
        self, db_session, test_user, admin_user, test_requisition, test_quote,
    ):
        plan = _create_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=test_user.id,
            cancelled_by_id=admin_user.id,
            status="cancelled",
            cancellation_reason="Order cancelled by customer",
        )

        mock_teams = AsyncMock()
        with patch(_PATCH_SETTINGS) as mock_settings, \
             patch(_PATCH_TEAMS_CH, mock_teams):
            mock_settings.admin_emails = [admin_user.email]
            mock_settings.app_url = "https://avail.test"
            await notify_buyplan_cancelled(plan, db_session)

        mock_teams.assert_awaited_once()
        msg = mock_teams.call_args[0][0]
        assert "Cancelled" in msg
        assert "Order cancelled by customer" in msg


# ── 8. verify_po_sent ────────────────────────────────────────────────


class TestVerifyPoSent:
    @pytest.mark.asyncio
    async def test_verifies_po_found(
        self, db_session, test_user, test_requisition, test_quote,
    ):
        plan = _create_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=test_user.id,
            status="po_entered",
            line_items=[
                {
                    "offer_id": 1, "mpn": "LM317T", "vendor_name": "Arrow",
                    "qty": 1000, "cost_price": 0.50,
                    "po_number": "PO-001",
                    "entered_by_id": test_user.id,
                    "po_verified": False,
                }
            ],
        )

        mock_gc = AsyncMock()
        mock_gc.get_json = AsyncMock(return_value={
            "value": [
                {
                    "subject": "PO-001 for Arrow Electronics",
                    "toRecipients": [{"emailAddress": {"address": "vendor@arrow.com"}}],
                    "sentDateTime": "2026-02-15T10:00:00Z",
                }
            ]
        })

        with patch(_PATCH_TOKEN, new_callable=AsyncMock, return_value="tok"), \
             patch(_PATCH_GC, return_value=mock_gc):
            results = await verify_po_sent(plan, db_session)

        assert results["PO-001"]["verified"] is True
        assert results["PO-001"]["recipient"] == "vendor@arrow.com"
        assert results["PO-001"]["sent_at"] == "2026-02-15T10:00:00Z"

        db_session.refresh(plan)
        assert plan.line_items[0]["po_verified"] is True
        assert plan.line_items[0]["po_recipient"] == "vendor@arrow.com"

    @pytest.mark.asyncio
    async def test_po_not_found(
        self, db_session, test_user, test_requisition, test_quote,
    ):
        plan = _create_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=test_user.id,
            status="po_entered",
            line_items=[
                {
                    "offer_id": 1, "mpn": "LM317T", "vendor_name": "Arrow",
                    "qty": 1000, "cost_price": 0.50,
                    "po_number": "PO-MISSING",
                    "entered_by_id": test_user.id,
                    "po_verified": False,
                }
            ],
        )

        mock_gc = AsyncMock()
        mock_gc.get_json = AsyncMock(return_value={"value": []})

        with patch(_PATCH_TOKEN, new_callable=AsyncMock, return_value="tok"), \
             patch(_PATCH_GC, return_value=mock_gc):
            results = await verify_po_sent(plan, db_session)

        assert results["PO-MISSING"]["verified"] is False
        assert results["PO-MISSING"]["reason"] == "not_found"

    @pytest.mark.asyncio
    async def test_no_token_for_buyer(
        self, db_session, test_user, test_requisition, test_quote,
    ):
        plan = _create_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=test_user.id,
            status="po_entered",
            line_items=[
                {
                    "offer_id": 1, "mpn": "LM317T", "vendor_name": "Arrow",
                    "qty": 1000, "cost_price": 0.50,
                    "po_number": "PO-NOTOKEN",
                    "entered_by_id": test_user.id,
                    "po_verified": False,
                }
            ],
        )

        with patch(_PATCH_TOKEN, new_callable=AsyncMock, return_value=None):
            results = await verify_po_sent(plan, db_session)

        assert results["PO-NOTOKEN"]["verified"] is False
        assert results["PO-NOTOKEN"]["reason"] == "no_token"

    @pytest.mark.asyncio
    async def test_skips_already_verified(
        self, db_session, test_user, test_requisition, test_quote,
    ):
        plan = _create_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=test_user.id,
            status="po_entered",
            line_items=[
                {
                    "offer_id": 1, "mpn": "LM317T", "vendor_name": "Arrow",
                    "qty": 1000, "cost_price": 0.50,
                    "po_number": "PO-DONE",
                    "entered_by_id": test_user.id,
                    "po_verified": True,
                }
            ],
        )

        with patch(_PATCH_TOKEN, new_callable=AsyncMock) as mock_token:
            results = await verify_po_sent(plan, db_session)

        mock_token.assert_not_awaited()
        assert results == {}

    @pytest.mark.asyncio
    async def test_skips_no_po_number(
        self, db_session, test_user, test_requisition, test_quote,
    ):
        plan = _create_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=test_user.id,
            status="po_entered",
            line_items=[
                {
                    "offer_id": 1, "mpn": "LM317T", "vendor_name": "Arrow",
                    "qty": 1000, "cost_price": 0.50,
                    "po_number": None,
                    "entered_by_id": test_user.id,
                    "po_verified": False,
                }
            ],
        )

        with patch(_PATCH_TOKEN, new_callable=AsyncMock) as mock_token:
            results = await verify_po_sent(plan, db_session)

        mock_token.assert_not_awaited()
        assert results == {}

    @pytest.mark.asyncio
    async def test_all_verified_transitions_status(
        self, db_session, test_user, test_requisition, test_quote,
    ):
        """When all POs verified, status transitions from po_entered to po_confirmed."""
        plan = _create_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=test_user.id,
            status="po_entered",
            line_items=[
                {
                    "offer_id": 1, "mpn": "LM317T", "vendor_name": "Arrow",
                    "qty": 1000, "cost_price": 0.50,
                    "po_number": "PO-A",
                    "entered_by_id": test_user.id,
                    "po_verified": False,
                }
            ],
        )

        mock_gc = AsyncMock()
        mock_gc.get_json = AsyncMock(return_value={
            "value": [
                {
                    "subject": "PO-A",
                    "toRecipients": [{"emailAddress": {"address": "v@arrow.com"}}],
                    "sentDateTime": "2026-02-15T10:00:00Z",
                }
            ]
        })

        with patch(_PATCH_TOKEN, new_callable=AsyncMock, return_value="tok"), \
             patch(_PATCH_GC, return_value=mock_gc):
            await verify_po_sent(plan, db_session)

        db_session.refresh(plan)
        assert plan.status == "po_confirmed"

    @pytest.mark.asyncio
    async def test_partial_verification_no_transition(
        self, db_session, test_user, test_requisition, test_quote,
    ):
        """When only some POs verified, status stays at po_entered."""
        plan = _create_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=test_user.id,
            status="po_entered",
            line_items=[
                {
                    "offer_id": 1, "mpn": "LM317T", "vendor_name": "Arrow",
                    "qty": 1000, "cost_price": 0.50,
                    "po_number": "PO-FOUND",
                    "entered_by_id": test_user.id,
                    "po_verified": False,
                },
                {
                    "offer_id": 2, "mpn": "NE555P", "vendor_name": "DigiKey",
                    "qty": 500, "cost_price": 0.25,
                    "po_number": "PO-NOTFOUND",
                    "entered_by_id": test_user.id,
                    "po_verified": False,
                },
            ],
        )

        mock_gc = AsyncMock()

        async def _mock_get(url, params=None):
            if params and "PO-FOUND" in params.get("$search", ""):
                return {
                    "value": [{
                        "subject": "PO-FOUND",
                        "toRecipients": [{"emailAddress": {"address": "v@arrow.com"}}],
                        "sentDateTime": "2026-02-15T10:00:00Z",
                    }]
                }
            return {"value": []}

        mock_gc.get_json = _mock_get

        with patch(_PATCH_TOKEN, new_callable=AsyncMock, return_value="tok"), \
             patch(_PATCH_GC, return_value=mock_gc):
            results = await verify_po_sent(plan, db_session)

        db_session.refresh(plan)
        assert plan.status == "po_entered"
        assert results["PO-FOUND"]["verified"] is True
        assert results["PO-NOTFOUND"]["verified"] is False

    @pytest.mark.asyncio
    async def test_graph_api_error_handled(
        self, db_session, test_user, test_requisition, test_quote,
    ):
        plan = _create_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=test_user.id,
            status="po_entered",
            line_items=[
                {
                    "offer_id": 1, "mpn": "LM317T", "vendor_name": "Arrow",
                    "qty": 1000, "cost_price": 0.50,
                    "po_number": "PO-ERR",
                    "entered_by_id": test_user.id,
                    "po_verified": False,
                }
            ],
        )

        mock_gc = AsyncMock()
        mock_gc.get_json = AsyncMock(side_effect=Exception("Graph API 503"))

        with patch(_PATCH_TOKEN, new_callable=AsyncMock, return_value="tok"), \
             patch(_PATCH_GC, return_value=mock_gc):
            results = await verify_po_sent(plan, db_session)

        assert results["PO-ERR"]["verified"] is False
        assert "Graph API 503" in results["PO-ERR"]["reason"]

    @pytest.mark.asyncio
    async def test_fallback_to_offer_entered_by(
        self, db_session, test_user, test_requisition, test_quote, test_offer,
    ):
        """When line item has no entered_by_id, falls back to offer.entered_by_id."""
        plan = _create_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=test_user.id,
            status="po_entered",
            line_items=[
                {
                    "offer_id": test_offer.id, "mpn": "LM317T", "vendor_name": "Arrow",
                    "qty": 1000, "cost_price": 0.50,
                    "po_number": "PO-FALLBACK",
                    "po_verified": False,
                }
            ],
        )

        mock_gc = AsyncMock()
        mock_gc.get_json = AsyncMock(return_value={"value": []})

        with patch(_PATCH_TOKEN, new_callable=AsyncMock, return_value="tok"), \
             patch(_PATCH_GC, return_value=mock_gc):
            results = await verify_po_sent(plan, db_session)

        assert "PO-FALLBACK" in results


# ── 9. auto_complete_stock_sales ──────────────────────────────────────


class TestAutoCompleteStockSales:
    def test_completes_old_approved_stock_sale(self, db_session, test_requisition, test_quote, test_user):
        plan = _create_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=test_user.id,
            status="approved",
            is_stock_sale=True,
        )
        plan.approved_at = datetime.now(timezone.utc) - timedelta(hours=2)
        db_session.commit()

        completed = auto_complete_stock_sales(db_session)
        assert completed == 1
        db_session.refresh(plan)
        assert plan.status == "complete"
        assert plan.completed_at is not None
        assert plan.completed_by_id is None

    def test_skips_recent_stock_sale(self, db_session, test_requisition, test_quote, test_user):
        plan = _create_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=test_user.id,
            status="approved",
            is_stock_sale=True,
        )
        plan.approved_at = datetime.now(timezone.utc) - timedelta(minutes=30)
        db_session.commit()

        completed = auto_complete_stock_sales(db_session)
        assert completed == 0
        db_session.refresh(plan)
        assert plan.status == "approved"

    def test_skips_non_stock_sale(self, db_session, test_requisition, test_quote, test_user):
        plan = _create_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=test_user.id,
            status="approved",
            is_stock_sale=False,
        )
        plan.approved_at = datetime.now(timezone.utc) - timedelta(hours=2)
        db_session.commit()

        completed = auto_complete_stock_sales(db_session)
        assert completed == 0

    def test_skips_non_approved_status(self, db_session, test_requisition, test_quote, test_user):
        plan = _create_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=test_user.id,
            status="pending_approval",
            is_stock_sale=True,
        )
        plan.approved_at = datetime.now(timezone.utc) - timedelta(hours=2)
        db_session.commit()

        completed = auto_complete_stock_sales(db_session)
        assert completed == 0

    def test_multiple_plans(self, db_session, test_requisition, test_quote, test_user):
        old_time = datetime.now(timezone.utc) - timedelta(hours=3)

        for _ in range(3):
            p = _create_plan(
                db_session,
                requisition_id=test_requisition.id,
                quote_id=test_quote.id,
                submitted_by_id=test_user.id,
                status="approved",
                is_stock_sale=True,
            )
            p.approved_at = old_time
        db_session.commit()

        completed = auto_complete_stock_sales(db_session)
        assert completed == 3

    def test_returns_zero_when_none_found(self, db_session):
        completed = auto_complete_stock_sales(db_session)
        assert completed == 0

    def test_exactly_one_hour_boundary(self, db_session, test_requisition, test_quote, test_user):
        """Plan approved just over 1 hour ago should be auto-completed."""
        plan = _create_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=test_user.id,
            status="approved",
            is_stock_sale=True,
        )
        plan.approved_at = datetime.now(timezone.utc) - timedelta(hours=1, seconds=1)
        db_session.commit()

        completed = auto_complete_stock_sales(db_session)
        assert completed == 1


# ── 10. run_buyplan_bg ────────────────────────────────────────────────


class TestRunBuyplanBg:
    def test_creates_asyncio_task(self, db_session, test_user, test_requisition, test_quote):
        """Verify run_buyplan_bg schedules a task via asyncio.create_task."""
        plan = _create_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=test_user.id,
        )

        mock_coro = AsyncMock()
        mock_coro.__name__ = "test_coro"

        with patch("app.services.buyplan_service.asyncio") as mock_asyncio:
            run_buyplan_bg(mock_coro, plan.id)
            mock_asyncio.create_task.assert_called_once()


# ── 11. _post_teams_channel ──────────────────────────────────────────


class TestPostTeamsChannel:
    @pytest.mark.asyncio
    async def test_skips_when_no_webhook(self):
        with patch("app.services.buyplan_service.get_credential_cached", return_value=None):
            await _post_teams_channel("Test message")

    @pytest.mark.asyncio
    async def test_posts_when_configured(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200

        with patch("app.services.buyplan_service.get_credential_cached", return_value="https://webhook.test"), \
             patch("app.services.buyplan_service.http") as mock_http:
            mock_http.post = AsyncMock(return_value=mock_resp)
            await _post_teams_channel("Buy plan notification")

        mock_http.post.assert_awaited_once()
        call_args = mock_http.post.call_args
        assert call_args[0][0] == "https://webhook.test"
        payload = call_args[1]["json"]
        assert payload["type"] == "message"
        assert "Buy plan notification" in payload["attachments"][0]["content"]["body"][0]["text"]

    @pytest.mark.asyncio
    async def test_handles_webhook_error(self):
        with patch("app.services.buyplan_service.get_credential_cached", return_value="https://webhook.test"), \
             patch("app.services.buyplan_service.http") as mock_http:
            mock_http.post = AsyncMock(side_effect=Exception("Connection refused"))
            await _post_teams_channel("Test message")

    @pytest.mark.asyncio
    async def test_handles_non_200_response(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_resp.text = "Forbidden"

        with patch("app.services.buyplan_service.get_credential_cached", return_value="https://webhook.test"), \
             patch("app.services.buyplan_service.http") as mock_http:
            mock_http.post = AsyncMock(return_value=mock_resp)
            await _post_teams_channel("Test message")


# ── 12. _send_teams_dm ──────────────────────────────────────────────


class TestSendTeamsDm:
    @pytest.mark.asyncio
    async def test_sends_dm_with_db_session(self, db_session, test_user):
        mock_gc = AsyncMock()
        mock_gc.post_json = AsyncMock(return_value={"id": "chat-123"})

        with patch(_PATCH_TOKEN, new_callable=AsyncMock, return_value="tok"), \
             patch(_PATCH_GC, return_value=mock_gc):
            await _send_teams_dm(test_user, "Hello from test", db_session)

        assert mock_gc.post_json.await_count == 2

    @pytest.mark.asyncio
    async def test_skips_when_no_token(self, db_session, test_user):
        with patch(_PATCH_TOKEN, new_callable=AsyncMock, return_value=None):
            await _send_teams_dm(test_user, "Test", db_session)

    @pytest.mark.asyncio
    async def test_skips_without_token_and_no_db(self):
        user = MagicMock()
        user.access_token = None
        user.email = "test@test.com"

        await _send_teams_dm(user, "Test", None)

    @pytest.mark.asyncio
    async def test_handles_graph_error(self, db_session, test_user):
        with patch(_PATCH_TOKEN, new_callable=AsyncMock, return_value="tok"), \
             patch(_PATCH_GC, side_effect=Exception("Teams API error")):
            await _send_teams_dm(test_user, "Test", db_session)


# ── 13. Edge cases & integration ─────────────────────────────────────


class TestEdgeCases:
    def test_activity_log_with_different_statuses(self, db_session, test_user, test_requisition, test_quote):
        """log_buyplan_activity captures current status in notes."""
        for status in ["pending_approval", "approved", "rejected", "po_entered", "complete", "cancelled"]:
            plan = _create_plan(
                db_session,
                requisition_id=test_requisition.id,
                quote_id=test_quote.id,
                submitted_by_id=test_user.id,
                status=status,
            )
            log_buyplan_activity(db_session, test_user.id, plan, f"test_{status}", f"status is {status}")
            db_session.commit()

            logs = _get_activities(db_session, f"test_{status}")
            assert len(logs) == 1
            assert f"status={status}" in logs[0].notes

    @pytest.mark.asyncio
    async def test_empty_line_items(self, db_session, test_user, test_requisition, test_quote):
        """verify_po_sent with empty line items returns empty dict."""
        plan = _create_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=test_user.id,
            status="po_entered",
            line_items=[],
        )

        results = await verify_po_sent(plan, db_session)
        assert results == {}

    @pytest.mark.asyncio
    async def test_submitted_total_cost_calculation(
        self, db_session, test_user, admin_user, test_requisition, test_quote, admin_in_settings,
    ):
        """Teams channel post includes total cost from line items."""
        plan = _create_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=test_user.id,
            line_items=[
                {"mpn": "A", "vendor_name": "V1", "plan_qty": 100, "cost_price": 1.00, "lead_time": "1w"},
                {"mpn": "B", "vendor_name": "V2", "plan_qty": 200, "cost_price": 2.00, "lead_time": "2w"},
            ],
        )

        mock_teams = AsyncMock()
        with patch(_PATCH_TOKEN, new_callable=AsyncMock, return_value=None), \
             patch(_PATCH_TEAMS_CH, mock_teams), \
             patch(_PATCH_TEAMS_DM, new_callable=AsyncMock):
            await notify_buyplan_submitted(plan, db_session)

        msg = mock_teams.call_args[0][0]
        # Total = 100*1.00 + 200*2.00 = $500.00
        assert "$500.00" in msg
        assert "2 line items" in msg
