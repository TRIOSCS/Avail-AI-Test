"""test_buyplan_notification_tiers.py — Tests for Task 10 notification re-tiering.

Covers the urgent (email + Teams DM + in-app) vs routine (in-app only) tier policy:
- notify_so_rejected → urgent: email + Teams DM to the salesperson.
- notify_po_rejected (new) → urgent: email + Teams DM + in-app to the line's buyer.
- notify_completed → routine: in-app only (no email, no Teams channel).
- notify_approved → urgent: email + Teams DM (+ in-app) to each buyer.
- verify-po reject path wiring fires notify_po_rejected; confirm still fires
  notify_po_confirmed.

Called by: pytest
Depends on: conftest.py, app.services.buyplan_notifications
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models import ActivityLog, User
from app.models.buy_plan import BuyPlan, BuyPlanLine

# ═══════════════════════════════════════════════════════════════════════
# HELPER FACTORIES (mirror tests/test_buyplan_v3_notifications.py)
# ═══════════════════════════════════════════════════════════════════════


def _make_user(db, email="buyer@trioscs.com", name="Test Buyer", role="buyer"):
    u = User(
        email=email,
        name=name,
        role=role,
        azure_id=f"az-{email}",
        m365_connected=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _make_plan(db, submitter_id, **overrides):
    """Create a minimal BuyPlan with required FKs."""
    from app.models import Company, CustomerSite, Quote, Requisition

    req = Requisition(
        name="REQ-T10",
        status="active",
        created_by=submitter_id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(req)
    db.flush()

    co = Company(name="Acme Corp", is_active=True, created_at=datetime.now(timezone.utc))
    db.add(co)
    db.flush()
    site = CustomerSite(company_id=co.id, site_name="Acme HQ")
    db.add(site)
    db.flush()
    q = Quote(
        requisition_id=req.id,
        customer_site_id=site.id,
        quote_number="Q-2026-0099",
        status="sent",
        line_items=[],
        subtotal=1000.0,
        total_cost=500.0,
        total_margin_pct=50.0,
        created_by_id=submitter_id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(q)
    db.flush()

    defaults = dict(
        quote_id=q.id,
        requisition_id=req.id,
        submitted_by_id=submitter_id,
        status="active",
        so_status="verified",
        sales_order_number="SO-T10-001",
    )
    defaults.update(overrides)
    plan = BuyPlan(**defaults)
    db.add(plan)
    db.commit()
    db.refresh(plan)
    return plan


def _add_line(db, plan, buyer_id=None, quantity=100, unit_cost=1.50, **overrides):
    """Add a BuyPlanLine with an offer."""
    from app.models import Offer, Requirement

    req = db.query(Requirement).first()
    if not req:
        req = Requirement(
            requisition_id=plan.requisition_id,
            primary_mpn="LM317T",
            target_qty=1000,
            created_at=datetime.now(timezone.utc),
        )
        db.add(req)
        db.flush()

    offer = Offer(
        requisition_id=plan.requisition_id,
        vendor_name="Arrow Electronics",
        mpn="LM317T",
        qty_available=1000,
        unit_price=1.50,
        entered_by_id=plan.submitted_by_id,
        status="active",
        lead_time="2 weeks",
        created_at=datetime.now(timezone.utc),
    )
    db.add(offer)
    db.flush()

    line = BuyPlanLine(
        buy_plan_id=plan.id,
        requirement_id=req.id,
        offer_id=offer.id,
        quantity=quantity,
        unit_cost=unit_cost,
        buyer_id=buyer_id,
        **overrides,
    )
    db.add(line)
    db.commit()
    db.refresh(plan)
    return line


# ═══════════════════════════════════════════════════════════════════════
# C1 — notify_so_rejected: urgent (email + Teams DM)
# ═══════════════════════════════════════════════════════════════════════


class TestNotifySoRejectedUrgent:
    @pytest.mark.asyncio
    async def test_emails_and_dms_submitter(self, db_session):
        from app.services.buyplan_notifications import notify_so_rejected

        submitter = _make_user(db_session, "sales@trioscs.com", "Sales Person", "sales")
        plan = _make_plan(db_session, submitter.id, so_rejection_note="Wrong SO number")

        with patch("app.services.buyplan_notifications._send_email", new_callable=AsyncMock) as mock_email:
            with patch("app.services.buyplan_notifications._teams_dm", new_callable=AsyncMock) as mock_dm:
                await notify_so_rejected(plan, db_session, action="reject")

        mock_email.assert_awaited_once()
        mock_dm.assert_awaited_once()
        assert mock_dm.call_args[0][0].id == submitter.id


# ═══════════════════════════════════════════════════════════════════════
# C2 — notify_po_rejected (new): urgent (email + Teams DM + in-app)
# ═══════════════════════════════════════════════════════════════════════


class TestNotifyPoRejected:
    @pytest.mark.asyncio
    async def test_emails_dms_and_inapp_to_buyer(self, db_session):
        from app.services.buyplan_notifications import notify_po_rejected

        submitter = _make_user(db_session, "sales@trioscs.com", "Sales", "sales")
        buyer = _make_user(db_session, "buyer1@trioscs.com", "Buyer One", "buyer")
        plan = _make_plan(db_session, submitter.id)
        line = _add_line(db_session, plan, buyer_id=buyer.id, po_rejection_note="PO total mismatch")

        with patch("app.services.buyplan_notifications._send_email", new_callable=AsyncMock) as mock_email:
            with patch("app.services.buyplan_notifications._teams_dm", new_callable=AsyncMock) as mock_dm:
                await notify_po_rejected(plan, db_session, line_id=line.id)

        mock_email.assert_awaited_once()
        mock_dm.assert_awaited_once()
        # Recipient is the line's buyer
        assert mock_email.call_args[0][0].id == buyer.id
        assert mock_dm.call_args[0][0].id == buyer.id
        # Rejection note appears in the email body (arg 2) and the DM message (arg 1)
        assert "PO total mismatch" in mock_email.call_args[0][2]
        assert "PO total mismatch" in mock_dm.call_args[0][1]
        # In-app ActivityLog row created for the buyer
        acts = db_session.query(ActivityLog).filter_by(user_id=buyer.id, buy_plan_id=plan.id).all()
        assert len(acts) == 1

    @pytest.mark.asyncio
    async def test_no_buyer_skips(self, db_session):
        from app.services.buyplan_notifications import notify_po_rejected

        submitter = _make_user(db_session, "sales@trioscs.com", "Sales", "sales")
        plan = _make_plan(db_session, submitter.id)
        line = _add_line(db_session, plan, buyer_id=None)

        with patch("app.services.buyplan_notifications._send_email", new_callable=AsyncMock) as mock_email:
            with patch("app.services.buyplan_notifications._teams_dm", new_callable=AsyncMock) as mock_dm:
                await notify_po_rejected(plan, db_session, line_id=line.id)

        mock_email.assert_not_awaited()
        mock_dm.assert_not_awaited()


# ═══════════════════════════════════════════════════════════════════════
# C3 — notify_completed: routine (in-app only)
# ═══════════════════════════════════════════════════════════════════════


class TestNotifyCompletedRoutine:
    @pytest.mark.asyncio
    async def test_inapp_only_no_email_no_teams(self, db_session):
        from app.services.buyplan_notifications import notify_completed

        submitter = _make_user(db_session, "sales@trioscs.com", "Sales", "sales")
        plan = _make_plan(db_session, submitter.id, status="completed")
        _add_line(db_session, plan)

        with patch("app.services.buyplan_notifications._send_email", new_callable=AsyncMock) as mock_email:
            with patch("app.services.buyplan_notifications._teams_channel", new_callable=AsyncMock) as mock_teams:
                await notify_completed(plan, db_session)

        mock_email.assert_not_awaited()
        mock_teams.assert_not_awaited()
        acts = db_session.query(ActivityLog).filter_by(activity_type="buyplan_completed", user_id=submitter.id).all()
        assert len(acts) == 1


# ═══════════════════════════════════════════════════════════════════════
# C4 — notify_approved: urgent (email + Teams DM + in-app per buyer)
# ═══════════════════════════════════════════════════════════════════════


class TestNotifyApprovedUrgent:
    @pytest.mark.asyncio
    async def test_each_buyer_email_dm_inapp(self, db_session):
        from app.services.buyplan_notifications import notify_approved

        submitter = _make_user(db_session, "sales@trioscs.com", "Sales", "sales")
        buyer = _make_user(db_session, "buyer1@trioscs.com", "Buyer One", "buyer")
        plan = _make_plan(db_session, submitter.id)
        _add_line(db_session, plan, buyer_id=buyer.id)

        with patch("app.services.buyplan_notifications._send_email", new_callable=AsyncMock) as mock_email:
            with patch("app.services.buyplan_notifications._teams_channel", new_callable=AsyncMock):
                with patch("app.services.buyplan_notifications._teams_dm", new_callable=AsyncMock) as mock_dm:
                    await notify_approved(plan, db_session)

        assert mock_email.await_count == 1
        assert mock_email.call_args[0][0].id == buyer.id
        assert mock_dm.await_count == 1
        assert mock_dm.call_args[0][0].id == buyer.id
        acts = db_session.query(ActivityLog).filter_by(activity_type="buyplan_approved", user_id=buyer.id).all()
        assert len(acts) == 1


# ═══════════════════════════════════════════════════════════════════════
# C2 wiring — verify-po handler dispatches notify_po_rejected on reject
# ═══════════════════════════════════════════════════════════════════════


class _FakeRequest:
    """Minimal stand-in for a Starlette Request exposing an awaitable form()."""

    def __init__(self, form_data):
        self._form = dict(form_data)

    async def form(self):
        return self._form


async def _drive_verify_po(action, db_session, mock_bg):
    """Invoke the verify-po handler with workflow/notify deps stubbed.

    The handler imports verify_po/check_completion/run_notify_bg locally from their
    SOURCE modules, so patches must target those modules — not htmx_views.
    """
    from app.routers import htmx_views

    form = {"action": action}
    if action == "reject":
        form["rejection_note"] = "bad PO"
    req = _FakeRequest(form)
    with (
        patch("app.services.buyplan_workflow.verify_po"),
        patch("app.services.buyplan_workflow.check_completion", return_value=None),
        patch("app.services.buyplan_notifications.run_notify_bg", mock_bg),
        patch.object(htmx_views, "buy_plan_detail_partial", new_callable=AsyncMock, return_value="ok"),
    ):
        await htmx_views.buy_plan_verify_po_partial(req, plan_id=5, line_id=9, user=MagicMock(), db=db_session)


class TestVerifyPoWiring:
    @pytest.mark.asyncio
    async def test_reject_fires_notify_po_rejected(self, db_session):
        mock_bg = AsyncMock()
        await _drive_verify_po("reject", db_session, mock_bg)

        called = [c.args[0].__name__ for c in mock_bg.await_args_list]
        assert "notify_po_rejected" in called
        assert "notify_completed" not in called

    @pytest.mark.asyncio
    async def test_approve_does_not_fire_po_rejected(self, db_session):
        mock_bg = AsyncMock()
        await _drive_verify_po("approve", db_session, mock_bg)

        called = [c.args[0].__name__ for c in mock_bg.await_args_list]
        assert "notify_po_rejected" not in called
