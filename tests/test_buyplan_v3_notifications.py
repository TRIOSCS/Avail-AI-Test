"""
test_buyplan_v3_notifications.py — Tests for buy plan V3 notification service.

Covers all 10 notify_v3_* functions plus helpers (_plan_context, _lines_html,
_wrap_email, _send_email, _teams_channel, _teams_dm, run_v3_notify_bg).

Called by: pytest
Depends on: conftest.py, app.services.buyplan_v3_notifications
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models import ActivityLog, User
from app.models.buy_plan import (
    BuyPlanLine,
    BuyPlanV3,
    VerificationGroupMember,
)


# ═══════════════════════════════════════════════════════════════════════
# HELPER FACTORIES
# ═══════════════════════════════════════════════════════════════════════


def _make_user(db, email="buyer@trioscs.com", name="Test Buyer", role="buyer"):
    u = User(
        email=email, name=name, role=role,
        azure_id=f"az-{email}", m365_connected=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _make_plan(db, submitter_id, **overrides):
    """Create a minimal BuyPlanV3 with required FKs."""
    from app.models import Requisition, Quote, Company, CustomerSite

    # Requisition
    req = Requisition(
        name="REQ-BP", status="open", created_by=submitter_id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(req)
    db.flush()

    # Company + site + quote
    co = Company(name="Acme Corp", is_active=True, created_at=datetime.now(timezone.utc))
    db.add(co)
    db.flush()
    site = CustomerSite(company_id=co.id, site_name="Acme HQ")
    db.add(site)
    db.flush()
    q = Quote(
        requisition_id=req.id, customer_site_id=site.id,
        quote_number="Q-2026-0042", status="sent",
        line_items=[], subtotal=1000.0, total_cost=500.0,
        total_margin_pct=50.0, created_by_id=submitter_id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(q)
    db.flush()

    defaults = dict(
        quote_id=q.id, requisition_id=req.id,
        submitted_by_id=submitter_id,
        status="pending", so_status="pending",
        sales_order_number="SO-001",
    )
    defaults.update(overrides)
    plan = BuyPlanV3(**defaults)
    db.add(plan)
    db.commit()
    db.refresh(plan)
    return plan


def _add_line(db, plan, offer_mock=None, buyer_id=None, quantity=100, unit_cost=1.50, po_number=None):
    """Add a BuyPlanLine with an optional mock offer."""
    from app.models import Offer, Requirement

    req = db.query(Requirement).first()
    if not req:
        req = Requirement(
            requisition_id=plan.requisition_id,
            primary_mpn="LM317T", target_qty=1000,
            created_at=datetime.now(timezone.utc),
        )
        db.add(req)
        db.flush()

    offer = Offer(
        requisition_id=plan.requisition_id,
        vendor_name="Arrow Electronics", mpn="LM317T",
        qty_available=1000, unit_price=1.50,
        entered_by_id=plan.submitted_by_id,
        status="active", lead_time="2 weeks",
        created_at=datetime.now(timezone.utc),
    )
    db.add(offer)
    db.flush()

    line = BuyPlanLine(
        buy_plan_id=plan.id, requirement_id=req.id,
        offer_id=offer.id, quantity=quantity,
        unit_cost=unit_cost, buyer_id=buyer_id,
        po_number=po_number,
    )
    db.add(line)
    db.commit()
    db.refresh(plan)
    return line


# ═══════════════════════════════════════════════════════════════════════
# _plan_context
# ═══════════════════════════════════════════════════════════════════════


class TestPlanContext:
    def test_basic_context(self, db_session):
        from app.services.buyplan_v3_notifications import _plan_context

        user = _make_user(db_session)
        plan = _make_plan(db_session, user.id)
        ctx = _plan_context(plan, db_session)

        assert ctx["submitter"].id == user.id
        assert ctx["submitter_name"] == "Test Buyer"
        assert ctx["customer_name"] == "Acme Corp"
        assert ctx["quote_number"] == "Q-2026-0042"

    def test_no_submitter(self, db_session):
        from app.services.buyplan_v3_notifications import _plan_context

        user = _make_user(db_session)
        plan = _make_plan(db_session, user.id, submitted_by_id=None)
        ctx = _plan_context(plan, db_session)

        assert ctx["submitter"] is None
        assert ctx["submitter_name"] == "Unknown"

    def test_no_quote(self, db_session):
        from app.services.buyplan_v3_notifications import _plan_context

        # Use a mock with quote_id=None to test the no-quote path
        # (real BuyPlanV3 has NOT NULL on quote_id)
        mock_plan = MagicMock(submitted_by_id=None, quote_id=None)
        ctx = _plan_context(mock_plan, db_session)

        assert ctx["customer_name"] == ""
        assert ctx["quote_number"] == ""
        assert ctx["submitter"] is None


# ═══════════════════════════════════════════════════════════════════════
# _lines_html
# ═══════════════════════════════════════════════════════════════════════


class TestLinesHtml:
    def test_empty_lines(self):
        from app.services.buyplan_v3_notifications import _lines_html

        plan = MagicMock(lines=[])
        rows, total = _lines_html(plan)
        assert rows == ""
        assert total == 0.0

    def test_none_lines(self):
        from app.services.buyplan_v3_notifications import _lines_html

        plan = MagicMock(lines=None)
        rows, total = _lines_html(plan)
        assert rows == ""
        assert total == 0.0

    def test_with_lines(self, db_session):
        from app.services.buyplan_v3_notifications import _lines_html

        user = _make_user(db_session)
        plan = _make_plan(db_session, user.id)
        _add_line(db_session, plan, quantity=100, unit_cost=2.00)

        rows, total = _lines_html(plan)
        assert "LM317T" in rows
        assert "Arrow Electronics" in rows
        assert total == 200.0

    def test_line_no_offer(self):
        from app.services.buyplan_v3_notifications import _lines_html

        line = MagicMock(offer=None, unit_cost=5.0, quantity=10)
        plan = MagicMock(lines=[line])
        rows, total = _lines_html(plan)
        assert "—" in rows  # dash for missing offer
        assert total == 50.0


# ═══════════════════════════════════════════════════════════════════════
# _wrap_email
# ═══════════════════════════════════════════════════════════════════════


class TestWrapEmail:
    def test_wraps_content(self):
        from app.services.buyplan_v3_notifications import _wrap_email

        result = _wrap_email("Test Title", "<p>Body</p>")
        assert "Test Title" in result
        assert "<p>Body</p>" in result
        assert "automated alert from AVAIL" in result

    def test_escapes_title(self):
        from app.services.buyplan_v3_notifications import _wrap_email

        result = _wrap_email("Title <script>", "<p>ok</p>")
        assert "&lt;script&gt;" in result


# ═══════════════════════════════════════════════════════════════════════
# _send_email
# ═══════════════════════════════════════════════════════════════════════


class TestSendEmail:
    @pytest.mark.asyncio
    async def test_sends_email(self, db_session):
        from app.services.buyplan_v3_notifications import _send_email

        user = _make_user(db_session)
        mock_gc = MagicMock()
        mock_gc.post_json = AsyncMock()

        with patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="tok"):
            with patch("app.utils.graph_client.GraphClient", return_value=mock_gc):
                await _send_email(user, "Subject", "<b>body</b>", db_session)

        mock_gc.post_json.assert_awaited_once()
        call_args = mock_gc.post_json.call_args
        assert call_args[0][0] == "/me/sendMail"

    @pytest.mark.asyncio
    async def test_no_token(self, db_session):
        from app.services.buyplan_v3_notifications import _send_email

        user = _make_user(db_session)

        with patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value=None):
            await _send_email(user, "Subject", "<b>body</b>", db_session)
        # Should return silently — no error

    @pytest.mark.asyncio
    async def test_send_error_logged(self, db_session):
        from app.services.buyplan_v3_notifications import _send_email

        user = _make_user(db_session)

        with patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="tok"):
            with patch("app.utils.graph_client.GraphClient", side_effect=Exception("fail")):
                await _send_email(user, "Subject", "<b>body</b>", db_session)
        # Should not raise — error is caught and logged


# ═══════════════════════════════════════════════════════════════════════
# _teams_channel / _teams_dm
# ═══════════════════════════════════════════════════════════════════════


class TestTeamsHelpers:
    @pytest.mark.asyncio
    async def test_teams_channel(self):
        from app.services.buyplan_v3_notifications import _teams_channel

        with patch("app.services.buyplan_service._post_teams_channel", new_callable=AsyncMock) as mock:
            await _teams_channel("Hello teams")
        mock.assert_awaited_once_with("Hello teams")

    @pytest.mark.asyncio
    async def test_teams_dm(self, db_session):
        from app.services.buyplan_v3_notifications import _teams_dm

        user = _make_user(db_session)
        with patch("app.services.buyplan_service._send_teams_dm", new_callable=AsyncMock) as mock:
            await _teams_dm(user, "DM message", db_session)
        mock.assert_awaited_once_with(user, "DM message", db_session)


# ═══════════════════════════════════════════════════════════════════════
# run_v3_notify_bg
# ═══════════════════════════════════════════════════════════════════════


class TestRunV3NotifyBg:
    @pytest.mark.asyncio
    async def test_runs_coro_factory(self, db_session):
        from app.services.buyplan_v3_notifications import run_v3_notify_bg

        user = _make_user(db_session)
        plan = _make_plan(db_session, user.id)

        coro_factory = AsyncMock()

        with patch("app.database.SessionLocal", return_value=db_session):
            with patch("asyncio.create_task") as mock_task:
                run_v3_notify_bg(coro_factory, plan.id, extra="arg")
                # Extract the coroutine passed to create_task and await it
                coro = mock_task.call_args[0][0]
                await coro

        coro_factory.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_handles_missing_plan(self, db_session):
        from app.services.buyplan_v3_notifications import run_v3_notify_bg

        coro_factory = AsyncMock()

        with patch("app.database.SessionLocal", return_value=db_session):
            with patch("asyncio.create_task") as mock_task:
                run_v3_notify_bg(coro_factory, 99999)
                coro = mock_task.call_args[0][0]
                await coro

        coro_factory.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_handles_exception(self, db_session):
        from app.services.buyplan_v3_notifications import run_v3_notify_bg

        user = _make_user(db_session)
        plan = _make_plan(db_session, user.id)

        coro_factory = AsyncMock(side_effect=Exception("boom"))

        with patch("app.database.SessionLocal", return_value=db_session):
            with patch("asyncio.create_task") as mock_task:
                run_v3_notify_bg(coro_factory, plan.id)
                coro = mock_task.call_args[0][0]
                await coro  # should not raise


# ═══════════════════════════════════════════════════════════════════════
# notify_v3_submitted
# ═══════════════════════════════════════════════════════════════════════


class TestNotifyV3Submitted:
    @pytest.mark.asyncio
    async def test_submitted_emails_managers(self, db_session):
        from app.services.buyplan_v3_notifications import notify_v3_submitted

        user = _make_user(db_session)
        mgr = _make_user(db_session, "mgr@trioscs.com", "Manager", "manager")
        plan = _make_plan(db_session, user.id)
        _add_line(db_session, plan, quantity=10, unit_cost=5.0)

        with patch("app.services.buyplan_v3_notifications._send_email", new_callable=AsyncMock) as mock_email:
            with patch("app.services.buyplan_v3_notifications._teams_channel", new_callable=AsyncMock):
                await notify_v3_submitted(plan, db_session)

        mock_email.assert_awaited_once()
        args = mock_email.call_args[0]
        assert args[0].id == mgr.id

    @pytest.mark.asyncio
    async def test_submitted_creates_activity(self, db_session):
        from app.services.buyplan_v3_notifications import notify_v3_submitted

        user = _make_user(db_session)
        _make_user(db_session, "admin@trioscs.com", "Admin", "admin")
        plan = _make_plan(db_session, user.id)

        with patch("app.services.buyplan_v3_notifications._send_email", new_callable=AsyncMock):
            with patch("app.services.buyplan_v3_notifications._teams_channel", new_callable=AsyncMock):
                await notify_v3_submitted(plan, db_session)

        activities = db_session.query(ActivityLog).filter_by(activity_type="buyplan_pending").all()
        assert len(activities) >= 1

    @pytest.mark.asyncio
    async def test_submitted_with_notes(self, db_session):
        from app.services.buyplan_v3_notifications import notify_v3_submitted

        user = _make_user(db_session)
        _make_user(db_session, "mgr@trioscs.com", "Manager", "manager")
        plan = _make_plan(db_session, user.id, salesperson_notes="Urgent deal")

        with patch("app.services.buyplan_v3_notifications._send_email", new_callable=AsyncMock) as mock_email:
            with patch("app.services.buyplan_v3_notifications._teams_channel", new_callable=AsyncMock):
                await notify_v3_submitted(plan, db_session)

        # Check that notes appear in the email body
        email_body = mock_email.call_args[0][2]
        assert "Urgent deal" in email_body

    @pytest.mark.asyncio
    async def test_submitted_fallback_admin_emails(self, db_session):
        from app.services.buyplan_v3_notifications import notify_v3_submitted

        user = _make_user(db_session)
        # No managers — should fall back to admin_emails setting
        plan = _make_plan(db_session, user.id)

        with patch("app.services.buyplan_v3_notifications._send_email", new_callable=AsyncMock):
            with patch("app.services.buyplan_v3_notifications._teams_channel", new_callable=AsyncMock):
                await notify_v3_submitted(plan, db_session)
        # No crash even with no managers

    @pytest.mark.asyncio
    async def test_submitted_teams_channel(self, db_session):
        from app.services.buyplan_v3_notifications import notify_v3_submitted

        user = _make_user(db_session)
        _make_user(db_session, "mgr@trioscs.com", "Manager", "manager")
        plan = _make_plan(db_session, user.id)

        with patch("app.services.buyplan_v3_notifications._send_email", new_callable=AsyncMock):
            with patch("app.services.buyplan_v3_notifications._teams_channel", new_callable=AsyncMock) as mock_teams:
                await notify_v3_submitted(plan, db_session)

        mock_teams.assert_awaited_once()
        msg = mock_teams.call_args[0][0]
        assert "Approval Required" in msg


# ═══════════════════════════════════════════════════════════════════════
# notify_v3_approved
# ═══════════════════════════════════════════════════════════════════════


class TestNotifyV3Approved:
    @pytest.mark.asyncio
    async def test_approved_emails_buyers(self, db_session):
        from app.services.buyplan_v3_notifications import notify_v3_approved

        submitter = _make_user(db_session)
        buyer = _make_user(db_session, "buyer2@trioscs.com", "Buyer2", "buyer")
        plan = _make_plan(db_session, submitter.id)
        _add_line(db_session, plan, buyer_id=buyer.id)

        with patch("app.services.buyplan_v3_notifications._send_email", new_callable=AsyncMock) as mock_email:
            with patch("app.services.buyplan_v3_notifications._teams_channel", new_callable=AsyncMock):
                with patch("app.services.buyplan_v3_notifications._teams_dm", new_callable=AsyncMock):
                    await notify_v3_approved(plan, db_session)

        # Should email the buyer
        assert mock_email.await_count == 1
        assert mock_email.call_args[0][0].id == buyer.id

    @pytest.mark.asyncio
    async def test_approved_creates_activities(self, db_session):
        from app.services.buyplan_v3_notifications import notify_v3_approved

        submitter = _make_user(db_session)
        buyer = _make_user(db_session, "buyer2@trioscs.com", "Buyer2", "buyer")
        plan = _make_plan(db_session, submitter.id)
        _add_line(db_session, plan, buyer_id=buyer.id)

        with patch("app.services.buyplan_v3_notifications._send_email", new_callable=AsyncMock):
            with patch("app.services.buyplan_v3_notifications._teams_channel", new_callable=AsyncMock):
                with patch("app.services.buyplan_v3_notifications._teams_dm", new_callable=AsyncMock):
                    await notify_v3_approved(plan, db_session)

        activities = db_session.query(ActivityLog).filter_by(activity_type="buyplan_approved").all()
        assert len(activities) >= 2  # one for buyer, one for submitter

    @pytest.mark.asyncio
    async def test_approved_no_buyers(self, db_session):
        from app.services.buyplan_v3_notifications import notify_v3_approved

        submitter = _make_user(db_session)
        plan = _make_plan(db_session, submitter.id)
        # No lines = no buyers

        with patch("app.services.buyplan_v3_notifications._send_email", new_callable=AsyncMock):
            with patch("app.services.buyplan_v3_notifications._teams_channel", new_callable=AsyncMock):
                with patch("app.services.buyplan_v3_notifications._teams_dm", new_callable=AsyncMock):
                    await notify_v3_approved(plan, db_session)

    @pytest.mark.asyncio
    async def test_approved_teams_dm(self, db_session):
        from app.services.buyplan_v3_notifications import notify_v3_approved

        submitter = _make_user(db_session)
        buyer = _make_user(db_session, "buyer2@trioscs.com", "Buyer2", "buyer")
        plan = _make_plan(db_session, submitter.id)
        _add_line(db_session, plan, buyer_id=buyer.id)

        with patch("app.services.buyplan_v3_notifications._send_email", new_callable=AsyncMock):
            with patch("app.services.buyplan_v3_notifications._teams_channel", new_callable=AsyncMock):
                with patch("app.services.buyplan_v3_notifications._teams_dm", new_callable=AsyncMock) as mock_dm:
                    await notify_v3_approved(plan, db_session)

        mock_dm.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_approved_no_submitter(self, db_session):
        from app.services.buyplan_v3_notifications import notify_v3_approved

        user = _make_user(db_session)
        plan = _make_plan(db_session, user.id, submitted_by_id=None)

        with patch("app.services.buyplan_v3_notifications._send_email", new_callable=AsyncMock):
            with patch("app.services.buyplan_v3_notifications._teams_channel", new_callable=AsyncMock):
                with patch("app.services.buyplan_v3_notifications._teams_dm", new_callable=AsyncMock):
                    await notify_v3_approved(plan, db_session)
        # No crash when no submitter


# ═══════════════════════════════════════════════════════════════════════
# notify_v3_rejected
# ═══════════════════════════════════════════════════════════════════════


class TestNotifyV3Rejected:
    @pytest.mark.asyncio
    async def test_rejected_emails_submitter(self, db_session):
        from app.services.buyplan_v3_notifications import notify_v3_rejected

        user = _make_user(db_session)
        mgr = _make_user(db_session, "mgr@trioscs.com", "Manager", "manager")
        plan = _make_plan(db_session, user.id, approved_by_id=mgr.id, approval_notes="Too expensive")

        with patch("app.services.buyplan_v3_notifications._send_email", new_callable=AsyncMock) as mock_email:
            with patch("app.services.buyplan_v3_notifications._teams_dm", new_callable=AsyncMock):
                await notify_v3_rejected(plan, db_session)

        mock_email.assert_awaited_once()
        body = mock_email.call_args[0][2]
        assert "Too expensive" in body

    @pytest.mark.asyncio
    async def test_rejected_creates_activity(self, db_session):
        from app.services.buyplan_v3_notifications import notify_v3_rejected

        user = _make_user(db_session)
        mgr = _make_user(db_session, "mgr@trioscs.com", "Manager", "manager")
        plan = _make_plan(db_session, user.id, approved_by_id=mgr.id)

        with patch("app.services.buyplan_v3_notifications._send_email", new_callable=AsyncMock):
            with patch("app.services.buyplan_v3_notifications._teams_dm", new_callable=AsyncMock):
                await notify_v3_rejected(plan, db_session)

        activities = db_session.query(ActivityLog).filter_by(activity_type="buyplan_rejected").all()
        assert len(activities) == 1

    @pytest.mark.asyncio
    async def test_rejected_no_submitter(self, db_session):
        from app.services.buyplan_v3_notifications import notify_v3_rejected

        user = _make_user(db_session)
        plan = _make_plan(db_session, user.id, submitted_by_id=None)

        with patch("app.services.buyplan_v3_notifications._send_email", new_callable=AsyncMock):
            with patch("app.services.buyplan_v3_notifications._teams_dm", new_callable=AsyncMock) as mock_dm:
                await notify_v3_rejected(plan, db_session)

        mock_dm.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rejected_no_approver(self, db_session):
        from app.services.buyplan_v3_notifications import notify_v3_rejected

        user = _make_user(db_session)
        plan = _make_plan(db_session, user.id, approved_by_id=None)

        with patch("app.services.buyplan_v3_notifications._send_email", new_callable=AsyncMock) as mock_email:
            with patch("app.services.buyplan_v3_notifications._teams_dm", new_callable=AsyncMock):
                await notify_v3_rejected(plan, db_session)

        body = mock_email.call_args[0][2]
        assert "Manager" in body

    @pytest.mark.asyncio
    async def test_rejected_without_notes(self, db_session):
        from app.services.buyplan_v3_notifications import notify_v3_rejected

        user = _make_user(db_session)
        mgr = _make_user(db_session, "mgr@trioscs.com", "Manager", "manager")
        plan = _make_plan(db_session, user.id, approved_by_id=mgr.id)
        # No approval_notes set

        with patch("app.services.buyplan_v3_notifications._send_email", new_callable=AsyncMock) as mock_email:
            with patch("app.services.buyplan_v3_notifications._teams_dm", new_callable=AsyncMock):
                await notify_v3_rejected(plan, db_session)

        body = mock_email.call_args[0][2]
        assert "Reason:" not in body


# ═══════════════════════════════════════════════════════════════════════
# notify_v3_so_verified
# ═══════════════════════════════════════════════════════════════════════


class TestNotifyV3SOVerified:
    @pytest.mark.asyncio
    async def test_so_verified_creates_activities(self, db_session):
        from app.services.buyplan_v3_notifications import notify_v3_so_verified

        submitter = _make_user(db_session)
        buyer1 = _make_user(db_session, "b1@trioscs.com", "Buyer1", "buyer")
        buyer2 = _make_user(db_session, "b2@trioscs.com", "Buyer2", "buyer")
        plan = _make_plan(db_session, submitter.id)
        _add_line(db_session, plan, buyer_id=buyer1.id)
        _add_line(db_session, plan, buyer_id=buyer2.id)

        await notify_v3_so_verified(plan, db_session)

        activities = db_session.query(ActivityLog).filter_by(activity_type="buyplan_approved").all()
        assert len(activities) == 2

    @pytest.mark.asyncio
    async def test_so_verified_no_buyers(self, db_session):
        from app.services.buyplan_v3_notifications import notify_v3_so_verified

        user = _make_user(db_session)
        plan = _make_plan(db_session, user.id)

        await notify_v3_so_verified(plan, db_session)
        # No crash


# ═══════════════════════════════════════════════════════════════════════
# notify_v3_so_rejected
# ═══════════════════════════════════════════════════════════════════════


class TestNotifyV3SORejected:
    @pytest.mark.asyncio
    async def test_so_rejected(self, db_session):
        from app.services.buyplan_v3_notifications import notify_v3_so_rejected

        user = _make_user(db_session)
        plan = _make_plan(db_session, user.id, so_rejection_note="Invalid SO")

        with patch("app.services.buyplan_v3_notifications._send_email", new_callable=AsyncMock) as mock_email:
            await notify_v3_so_rejected(plan, db_session, action="reject")

        body = mock_email.call_args[0][2]
        assert "rejected" in body
        assert "Invalid SO" in body

    @pytest.mark.asyncio
    async def test_so_halted(self, db_session):
        from app.services.buyplan_v3_notifications import notify_v3_so_rejected

        user = _make_user(db_session)
        plan = _make_plan(db_session, user.id)

        with patch("app.services.buyplan_v3_notifications._send_email", new_callable=AsyncMock) as mock_email:
            await notify_v3_so_rejected(plan, db_session, action="halt")

        body = mock_email.call_args[0][2]
        assert "halted" in body

    @pytest.mark.asyncio
    async def test_so_rejected_no_submitter(self, db_session):
        from app.services.buyplan_v3_notifications import notify_v3_so_rejected

        user = _make_user(db_session)
        plan = _make_plan(db_session, user.id, submitted_by_id=None)

        with patch("app.services.buyplan_v3_notifications._send_email", new_callable=AsyncMock) as mock_email:
            await notify_v3_so_rejected(plan, db_session, action="reject")

        mock_email.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_so_rejected_creates_activity(self, db_session):
        from app.services.buyplan_v3_notifications import notify_v3_so_rejected

        user = _make_user(db_session)
        plan = _make_plan(db_session, user.id)

        with patch("app.services.buyplan_v3_notifications._send_email", new_callable=AsyncMock):
            await notify_v3_so_rejected(plan, db_session, action="reject")

        activities = db_session.query(ActivityLog).filter_by(activity_type="buyplan_rejected").all()
        assert len(activities) == 1

    @pytest.mark.asyncio
    async def test_so_rejected_no_note(self, db_session):
        from app.services.buyplan_v3_notifications import notify_v3_so_rejected

        user = _make_user(db_session)
        plan = _make_plan(db_session, user.id)

        with patch("app.services.buyplan_v3_notifications._send_email", new_callable=AsyncMock) as mock_email:
            await notify_v3_so_rejected(plan, db_session, action="reject")

        body = mock_email.call_args[0][2]
        assert "Reason:" not in body


# ═══════════════════════════════════════════════════════════════════════
# notify_v3_issue_flagged
# ═══════════════════════════════════════════════════════════════════════


class TestNotifyV3IssueFlagged:
    @pytest.mark.asyncio
    async def test_issue_flagged(self, db_session):
        from app.services.buyplan_v3_notifications import notify_v3_issue_flagged

        user = _make_user(db_session)
        mgr = _make_user(db_session, "mgr@trioscs.com", "Manager", "manager")
        plan = _make_plan(db_session, user.id)
        line = _add_line(db_session, plan)

        with patch("app.services.buyplan_v3_notifications._teams_channel", new_callable=AsyncMock) as mock_teams:
            await notify_v3_issue_flagged(plan, db_session, line.id, "sold_out")

        activities = db_session.query(ActivityLog).filter_by(activity_type="buyplan_pending").all()
        assert len(activities) == 1
        assert "Sold Out" in activities[0].subject

        mock_teams.assert_awaited_once()
        msg = mock_teams.call_args[0][0]
        assert "Issue Flagged" in msg

    @pytest.mark.asyncio
    async def test_issue_flagged_unknown_type(self, db_session):
        from app.services.buyplan_v3_notifications import notify_v3_issue_flagged

        user = _make_user(db_session)
        _make_user(db_session, "admin@trioscs.com", "Admin", "admin")
        plan = _make_plan(db_session, user.id)
        line = _add_line(db_session, plan)

        with patch("app.services.buyplan_v3_notifications._teams_channel", new_callable=AsyncMock):
            await notify_v3_issue_flagged(plan, db_session, line.id, "custom_issue")

        activities = db_session.query(ActivityLog).filter_by(activity_type="buyplan_pending").all()
        assert "custom_issue" in activities[0].subject

    @pytest.mark.asyncio
    async def test_issue_flagged_fallback_admins(self, db_session):
        from app.services.buyplan_v3_notifications import notify_v3_issue_flagged

        user = _make_user(db_session)
        # No managers
        plan = _make_plan(db_session, user.id)
        line = _add_line(db_session, plan)

        with patch("app.services.buyplan_v3_notifications._teams_channel", new_callable=AsyncMock):
            await notify_v3_issue_flagged(plan, db_session, line.id, "price_changed")
        # No crash


# ═══════════════════════════════════════════════════════════════════════
# notify_v3_po_confirmed
# ═══════════════════════════════════════════════════════════════════════


class TestNotifyV3POConfirmed:
    @pytest.mark.asyncio
    async def test_po_confirmed(self, db_session):
        from app.services.buyplan_v3_notifications import notify_v3_po_confirmed

        user = _make_user(db_session)
        ops_user = _make_user(db_session, "ops@trioscs.com", "Ops", "buyer")
        plan = _make_plan(db_session, user.id)
        line = _add_line(db_session, plan, po_number="PO-001")

        vgm = VerificationGroupMember(user_id=ops_user.id, is_active=True)
        db_session.add(vgm)
        db_session.commit()

        await notify_v3_po_confirmed(plan, db_session, line.id)

        activities = db_session.query(ActivityLog).filter_by(activity_type="buyplan_pending").all()
        assert len(activities) == 1
        assert "PO-001" in activities[0].subject

    @pytest.mark.asyncio
    async def test_po_confirmed_no_ops_members(self, db_session):
        from app.services.buyplan_v3_notifications import notify_v3_po_confirmed

        user = _make_user(db_session)
        plan = _make_plan(db_session, user.id)
        line = _add_line(db_session, plan)

        await notify_v3_po_confirmed(plan, db_session, line.id)

        activities = db_session.query(ActivityLog).filter_by(activity_type="buyplan_pending").all()
        assert len(activities) == 0

    @pytest.mark.asyncio
    async def test_po_confirmed_inactive_member(self, db_session):
        from app.services.buyplan_v3_notifications import notify_v3_po_confirmed

        user = _make_user(db_session)
        ops_user = _make_user(db_session, "ops@trioscs.com", "Ops", "buyer")
        plan = _make_plan(db_session, user.id)
        line = _add_line(db_session, plan)

        vgm = VerificationGroupMember(user_id=ops_user.id, is_active=False)
        db_session.add(vgm)
        db_session.commit()

        await notify_v3_po_confirmed(plan, db_session, line.id)

        activities = db_session.query(ActivityLog).filter_by(activity_type="buyplan_pending").all()
        assert len(activities) == 0


# ═══════════════════════════════════════════════════════════════════════
# notify_v3_completed
# ═══════════════════════════════════════════════════════════════════════


class TestNotifyV3Completed:
    @pytest.mark.asyncio
    async def test_completed_emails_submitter(self, db_session):
        from app.services.buyplan_v3_notifications import notify_v3_completed

        user = _make_user(db_session)
        plan = _make_plan(db_session, user.id)

        with patch("app.services.buyplan_v3_notifications._send_email", new_callable=AsyncMock) as mock_email:
            with patch("app.services.buyplan_v3_notifications._teams_channel", new_callable=AsyncMock):
                await notify_v3_completed(plan, db_session)

        mock_email.assert_awaited_once()
        assert mock_email.call_args[0][0].id == user.id

    @pytest.mark.asyncio
    async def test_completed_creates_activity(self, db_session):
        from app.services.buyplan_v3_notifications import notify_v3_completed

        user = _make_user(db_session)
        plan = _make_plan(db_session, user.id)

        with patch("app.services.buyplan_v3_notifications._send_email", new_callable=AsyncMock):
            with patch("app.services.buyplan_v3_notifications._teams_channel", new_callable=AsyncMock):
                await notify_v3_completed(plan, db_session)

        activities = db_session.query(ActivityLog).filter_by(activity_type="buyplan_completed").all()
        assert len(activities) == 1

    @pytest.mark.asyncio
    async def test_completed_teams(self, db_session):
        from app.services.buyplan_v3_notifications import notify_v3_completed

        user = _make_user(db_session)
        plan = _make_plan(db_session, user.id)

        with patch("app.services.buyplan_v3_notifications._send_email", new_callable=AsyncMock):
            with patch("app.services.buyplan_v3_notifications._teams_channel", new_callable=AsyncMock) as mock_teams:
                await notify_v3_completed(plan, db_session)

        mock_teams.assert_awaited_once()
        msg = mock_teams.call_args[0][0]
        assert "Completed" in msg

    @pytest.mark.asyncio
    async def test_completed_no_submitter(self, db_session):
        from app.services.buyplan_v3_notifications import notify_v3_completed

        user = _make_user(db_session)
        plan = _make_plan(db_session, user.id, submitted_by_id=None)

        with patch("app.services.buyplan_v3_notifications._send_email", new_callable=AsyncMock) as mock_email:
            with patch("app.services.buyplan_v3_notifications._teams_channel", new_callable=AsyncMock):
                await notify_v3_completed(plan, db_session)

        mock_email.assert_not_awaited()
