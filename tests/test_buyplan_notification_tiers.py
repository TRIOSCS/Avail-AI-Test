"""test_buyplan_notification_tiers.py — single-path delivery policy per event.

W3.8/§5.5 replaced the urgent/routine multi-channel tiers with exactly ONE delivery
system per approval-lifecycle event: an ApprovalOutbox email. This file pins that
policy per event:
- notify_so_rejected → ONE outbox email to the salesperson; no Teams DM, no in-app.
- notify_po_rejected → ONE outbox email to the line's buyer; no Teams DM, no in-app.
- notify_completed → still routine: in-app only (non-approval event), no outbox row.
- notify_approved → ONE outbox email per buyer; no Teams DM, no in-app.
- verify-po reject path wiring fires notify_po_rejected; approve does not.

Called by: pytest
Depends on: conftest.py, app.services.buyplan_notifications, app.models.approvals
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.constants import ApprovalGateType, ApprovalRequestStatus, ApprovalSubjectType
from app.models import ActivityLog, User
from app.models.approvals import ApprovalOutbox, ApprovalRequest
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
        created_at=datetime.now(UTC),
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
        status="open",
        created_by=submitter_id,
        created_at=datetime.now(UTC),
    )
    db.add(req)
    db.flush()

    co = Company(name="Acme Corp", is_active=True, created_at=datetime.now(UTC))
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
        created_at=datetime.now(UTC),
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
            created_at=datetime.now(UTC),
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
        created_at=datetime.now(UTC),
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


def _make_request(db, plan, status=ApprovalRequestStatus.APPROVED):
    """Anchor ApprovalRequest for *plan* (the outbox rows FK onto it)."""
    req = ApprovalRequest(
        gate_type=ApprovalGateType.BUY_PLAN,
        subject_type=ApprovalSubjectType.BUY_PLAN,
        subject_id=plan.id,
        requested_by_id=plan.submitted_by_id,
        owner_id=plan.submitted_by_id,
        status=status,
    )
    db.add(req)
    db.commit()
    return req


# ═══════════════════════════════════════════════════════════════════════
# C1 — notify_so_rejected: ONE outbox email, no Teams DM
# ═══════════════════════════════════════════════════════════════════════


class TestNotifySoRejectedSinglePath:
    @pytest.mark.asyncio
    async def test_one_outbox_email_no_dm(self, db_session):
        from app.services.buyplan_notifications import notify_so_rejected

        submitter = _make_user(db_session, "sales@trioscs.com", "Sales Person", "sales")
        plan = _make_plan(db_session, submitter.id, so_rejection_note="Wrong SO number")
        _make_request(db_session, plan, status=ApprovalRequestStatus.CANCELLED)

        with patch("app.services.teams_notifications.send_teams_dm", new_callable=AsyncMock) as mock_dm:
            await notify_so_rejected(plan, db_session, action="reject")

        mock_dm.assert_not_awaited()
        rows = db_session.query(ApprovalOutbox).all()
        assert len(rows) == 1
        assert rows[0].recipient_user_id == submitter.id
        assert "Wrong SO number" in rows[0].payload["html"]
        assert db_session.query(ActivityLog).count() == 0


# ═══════════════════════════════════════════════════════════════════════
# C2 — notify_po_rejected: ONE outbox email to the line's buyer
# ═══════════════════════════════════════════════════════════════════════


class TestNotifyPoRejected:
    @pytest.mark.asyncio
    async def test_one_outbox_email_to_buyer(self, db_session):
        from app.services.buyplan_notifications import notify_po_rejected

        submitter = _make_user(db_session, "sales@trioscs.com", "Sales", "sales")
        buyer = _make_user(db_session, "buyer1@trioscs.com", "Buyer One", "buyer")
        plan = _make_plan(db_session, submitter.id)
        line = _add_line(db_session, plan, buyer_id=buyer.id, po_rejection_note="PO total mismatch")
        _make_request(db_session, plan)

        with patch("app.services.teams_notifications.send_teams_dm", new_callable=AsyncMock) as mock_dm:
            await notify_po_rejected(plan, db_session, line_id=line.id)

        mock_dm.assert_not_awaited()
        rows = db_session.query(ApprovalOutbox).all()
        assert len(rows) == 1
        assert rows[0].recipient_user_id == buyer.id
        assert "PO total mismatch" in rows[0].payload["html"]
        # No in-app row — the outbox email is the event's only delivery.
        assert db_session.query(ActivityLog).filter_by(user_id=buyer.id, buy_plan_id=plan.id).count() == 0

    @pytest.mark.asyncio
    async def test_no_buyer_skips(self, db_session):
        from app.services.buyplan_notifications import notify_po_rejected

        submitter = _make_user(db_session, "sales@trioscs.com", "Sales", "sales")
        plan = _make_plan(db_session, submitter.id)
        line = _add_line(db_session, plan, buyer_id=None)
        _make_request(db_session, plan)

        await notify_po_rejected(plan, db_session, line_id=line.id)

        assert db_session.query(ApprovalOutbox).count() == 0


# ═══════════════════════════════════════════════════════════════════════
# C3 — notify_completed: routine (in-app only, no outbox row)
# ═══════════════════════════════════════════════════════════════════════


class TestNotifyCompletedRoutine:
    @pytest.mark.asyncio
    async def test_inapp_only_no_email_no_outbox(self, db_session):
        from app.services.buyplan_notifications import notify_completed

        submitter = _make_user(db_session, "sales@trioscs.com", "Sales", "sales")
        plan = _make_plan(db_session, submitter.id, status="completed")
        _add_line(db_session, plan)

        with patch("app.services.buyplan_notifications._send_email", new_callable=AsyncMock) as mock_email:
            await notify_completed(plan, db_session)

        mock_email.assert_not_awaited()
        assert db_session.query(ApprovalOutbox).count() == 0
        acts = db_session.query(ActivityLog).filter_by(activity_type="buyplan_completed", user_id=submitter.id).all()
        assert len(acts) == 1


# ═══════════════════════════════════════════════════════════════════════
# C4 — notify_approved: ONE outbox email per buyer
# ═══════════════════════════════════════════════════════════════════════


class TestNotifyApprovedSinglePath:
    @pytest.mark.asyncio
    async def test_each_buyer_one_outbox_email(self, db_session):
        from app.services.buyplan_notifications import notify_approved

        submitter = _make_user(db_session, "sales@trioscs.com", "Sales", "sales")
        buyer = _make_user(db_session, "buyer1@trioscs.com", "Buyer One", "buyer")
        plan = _make_plan(db_session, submitter.id)
        _add_line(db_session, plan, buyer_id=buyer.id)
        _make_request(db_session, plan)

        with patch("app.services.teams_notifications.send_teams_dm", new_callable=AsyncMock) as mock_dm:
            await notify_approved(plan, db_session)

        mock_dm.assert_not_awaited()
        rows = db_session.query(ApprovalOutbox).all()
        assert len(rows) == 1
        assert rows[0].recipient_user_id == buyer.id
        assert db_session.query(ActivityLog).filter_by(user_id=buyer.id).count() == 0


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
    from app.routers.htmx import buy_plans as htmx_buy_plans

    form = {"action": action}
    if action == "reject":
        form["rejection_note"] = "bad PO"
    req = _FakeRequest(form)
    # The reject branch persists real rows (the 2.2 note-to-the-fixer: an ActivityLog
    # NOTE keyed to the plan/line + the actor), so the handler needs REAL FK targets —
    # a bare MagicMock user / invented plan-line ids fail SQLite binding + FK
    # enforcement.
    actor = _make_user(db_session, email=f"verify-{action}@trioscs.com", name="Verifier", role="manager")
    plan = _make_plan(db_session, actor.id)
    line = _add_line(db_session, plan, buyer_id=actor.id, status="pending_verify", po_number="PO-T10")
    stub_line = MagicMock()
    stub_line.buyer_id = line.buyer_id
    stub_line.buy_plan = None
    with (
        patch("app.services.buyplan_workflow.verify_po", return_value=stub_line),
        patch("app.services.buyplan_workflow.check_completion", return_value=None),
        patch("app.services.buyplan_notifications.run_notify_bg", mock_bg),
        patch.object(htmx_buy_plans, "buy_plan_detail_partial", new_callable=AsyncMock, return_value="ok"),
    ):
        await htmx_buy_plans.buy_plan_verify_po_partial(
            req, plan_id=plan.id, line_id=line.id, user=actor, db=db_session
        )


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
        # The confirm → notify_po_confirmed path lives in a DIFFERENT handler
        # (buy_plan_confirm_po_partial) and is covered by the broader regression
        # in test_buyplan_notifications.py; this file only asserts the verify-po
        # reject path, so here we just verify approve does NOT fire the reject notice.
        mock_bg = AsyncMock()
        await _drive_verify_po("approve", db_session, mock_bg)

        called = [c.args[0].__name__ for c in mock_bg.await_args_list]
        assert "notify_po_rejected" not in called
