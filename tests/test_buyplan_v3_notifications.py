"""test_buyplan_v3_notifications.py — Tests for notification functions.

Covers: notify_stock_sale_approved, notify_token_approved, notify_token_rejected,
log_buyplan_activity, run_v3_notify_bg.

Called by: pytest
Depends on: conftest.py, app.services.buyplan_notifications
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models import ActivityLog, User
from app.models.buy_plan import BuyPlan, BuyPlanLine

# ═══════════════════════════════════════════════════════════════════════
# HELPER FACTORIES
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
        name="REQ-V3",
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
        status="pending",
        so_status="pending",
        sales_order_number="SO-V3-001",
    )
    defaults.update(overrides)
    plan = BuyPlan(**defaults)
    db.add(plan)
    db.commit()
    db.refresh(plan)
    return plan


def _add_line(db, plan, buyer_id=None, quantity=100, unit_cost=1.50):
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
    )
    db.add(line)
    db.commit()
    db.refresh(plan)
    return line


# ═══════════════════════════════════════════════════════════════════════
# notify_stock_sale_approved
# ═══════════════════════════════════════════════════════════════════════


class TestNotifyStockSaleApproved:
    @pytest.mark.asyncio
    async def test_sends_stock_sale_emails(self, db_session):
        from app.services.buyplan_notifications import notify_stock_sale_approved

        submitter = _make_user(db_session)
        admin = _make_user(db_session, "admin@trioscs.com", "Admin", "admin")
        admin.access_token = "fake-token"
        db_session.commit()

        plan = _make_plan(db_session, submitter.id, approved_by_id=admin.id)
        _add_line(db_session, plan, quantity=50, unit_cost=3.00)

        mock_gc = MagicMock()
        mock_gc.post_json = AsyncMock()

        with patch("app.services.buyplan_notifications.settings") as mock_settings:
            mock_settings.admin_emails = ["admin@trioscs.com"]
            mock_settings.stock_sale_notify_emails = ["logistics@trioscs.com", "accounting@trioscs.com"]
            mock_settings.app_url = "https://avail.test"
            with patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="tok"):
                with patch("app.utils.graph_client.GraphClient", return_value=mock_gc):
                    with patch("app.services.buyplan_notifications._teams_channel", new_callable=AsyncMock):
                        await notify_stock_sale_approved(plan, db_session)

        assert mock_gc.post_json.await_count == 2
        subjects = [call[0][1]["message"]["subject"] for call in mock_gc.post_json.call_args_list]
        assert all("Stock Sale Approved" in s for s in subjects)

    @pytest.mark.asyncio
    async def test_creates_submitter_activity(self, db_session):
        from app.services.buyplan_notifications import notify_stock_sale_approved

        submitter = _make_user(db_session)
        plan = _make_plan(db_session, submitter.id)

        with patch("app.services.buyplan_notifications.settings") as mock_settings:
            mock_settings.admin_emails = []
            mock_settings.stock_sale_notify_emails = []
            mock_settings.app_url = "https://avail.test"
            with patch("app.services.buyplan_notifications._teams_channel", new_callable=AsyncMock):
                await notify_stock_sale_approved(plan, db_session)

        activities = db_session.query(ActivityLog).filter_by(activity_type="buyplan_completed").all()
        assert len(activities) == 1
        assert "Stock sale" in activities[0].subject
        assert "no PO required" in activities[0].subject

    @pytest.mark.asyncio
    async def test_no_submitter_skips_activity(self, db_session):
        from app.services.buyplan_notifications import notify_stock_sale_approved

        user = _make_user(db_session)
        plan = _make_plan(db_session, user.id, submitted_by_id=None)

        with patch("app.services.buyplan_notifications.settings") as mock_settings:
            mock_settings.admin_emails = []
            mock_settings.stock_sale_notify_emails = []
            mock_settings.app_url = "https://avail.test"
            with patch("app.services.buyplan_notifications._teams_channel", new_callable=AsyncMock):
                await notify_stock_sale_approved(plan, db_session)

        activities = db_session.query(ActivityLog).filter_by(activity_type="buyplan_completed").all()
        assert len(activities) == 0

    @pytest.mark.asyncio
    async def test_teams_channel_posted(self, db_session):
        from app.services.buyplan_notifications import notify_stock_sale_approved

        submitter = _make_user(db_session)
        plan = _make_plan(db_session, submitter.id)

        with patch("app.services.buyplan_notifications.settings") as mock_settings:
            mock_settings.admin_emails = []
            mock_settings.stock_sale_notify_emails = []
            mock_settings.app_url = "https://avail.test"
            with patch("app.services.buyplan_notifications._teams_channel", new_callable=AsyncMock) as mock_teams:
                await notify_stock_sale_approved(plan, db_session)

        mock_teams.assert_awaited_once()
        msg = mock_teams.call_args[0][0]
        assert "Stock Sale Approved" in msg


# ═══════════════════════════════════════════════════════════════════════
# notify_token_approved
# ═══════════════════════════════════════════════════════════════════════


class TestNotifyTokenApproved:
    @pytest.mark.asyncio
    async def test_creates_submitter_activity(self, db_session):
        from app.services.buyplan_notifications import notify_token_approved

        submitter = _make_user(db_session)
        approver = _make_user(db_session, "mgr@trioscs.com", "Manager", "manager")
        plan = _make_plan(db_session, submitter.id, approved_by_id=approver.id)

        with patch("app.services.buyplan_notifications._teams_channel", new_callable=AsyncMock):
            await notify_token_approved(plan, db_session)

        activities = (
            db_session.query(ActivityLog)
            .filter_by(
                activity_type="buyplan_approved",
                user_id=submitter.id,
            )
            .all()
        )
        assert len(activities) == 1
        assert "approved via email" in activities[0].subject
        assert "Manager" in activities[0].subject

    @pytest.mark.asyncio
    async def test_creates_approver_activity(self, db_session):
        from app.services.buyplan_notifications import notify_token_approved

        submitter = _make_user(db_session)
        approver = _make_user(db_session, "mgr@trioscs.com", "Manager", "manager")
        plan = _make_plan(db_session, submitter.id, approved_by_id=approver.id)

        with patch("app.services.buyplan_notifications._teams_channel", new_callable=AsyncMock):
            await notify_token_approved(plan, db_session)

        activities = (
            db_session.query(ActivityLog)
            .filter_by(
                activity_type="buyplan_approved",
                user_id=approver.id,
            )
            .all()
        )
        assert len(activities) == 1
        assert "via email token" in activities[0].subject

    @pytest.mark.asyncio
    async def test_no_submitter(self, db_session):
        from app.services.buyplan_notifications import notify_token_approved

        user = _make_user(db_session)
        approver = _make_user(db_session, "mgr@trioscs.com", "Manager", "manager")
        plan = _make_plan(db_session, user.id, submitted_by_id=None, approved_by_id=approver.id)

        with patch("app.services.buyplan_notifications._teams_channel", new_callable=AsyncMock):
            await notify_token_approved(plan, db_session)

        # Only approver activity (no submitter activity)
        activities = db_session.query(ActivityLog).filter_by(activity_type="buyplan_approved").all()
        assert len(activities) == 1
        assert activities[0].user_id == approver.id

    @pytest.mark.asyncio
    async def test_teams_channel_posted(self, db_session):
        from app.services.buyplan_notifications import notify_token_approved

        submitter = _make_user(db_session)
        approver = _make_user(db_session, "mgr@trioscs.com", "Manager", "manager")
        plan = _make_plan(db_session, submitter.id, approved_by_id=approver.id)

        with patch("app.services.buyplan_notifications._teams_channel", new_callable=AsyncMock) as mock_teams:
            await notify_token_approved(plan, db_session)

        mock_teams.assert_awaited_once()
        msg = mock_teams.call_args[0][0]
        assert "Email Token" in msg
        assert "Manager" in msg

    @pytest.mark.asyncio
    async def test_no_approver_uses_fallback_name(self, db_session):
        from app.services.buyplan_notifications import notify_token_approved

        submitter = _make_user(db_session)
        plan = _make_plan(db_session, submitter.id, approved_by_id=None)

        with patch("app.services.buyplan_notifications._teams_channel", new_callable=AsyncMock) as mock_teams:
            await notify_token_approved(plan, db_session)

        msg = mock_teams.call_args[0][0]
        assert "Manager (email token)" in msg


# ═══════════════════════════════════════════════════════════════════════
# notify_token_rejected
# ═══════════════════════════════════════════════════════════════════════


class TestNotifyTokenRejected:
    @pytest.mark.asyncio
    async def test_creates_submitter_activity(self, db_session):
        from app.services.buyplan_notifications import notify_token_rejected

        submitter = _make_user(db_session)
        approver = _make_user(db_session, "mgr@trioscs.com", "Manager", "manager")
        plan = _make_plan(
            db_session,
            submitter.id,
            approved_by_id=approver.id,
            approval_notes="Price too high",
        )

        await notify_token_rejected(plan, db_session)

        activities = (
            db_session.query(ActivityLog)
            .filter_by(
                activity_type="buyplan_rejected",
                user_id=submitter.id,
            )
            .all()
        )
        assert len(activities) == 1
        assert "rejected via email" in activities[0].subject
        assert "Price too high" in activities[0].subject

    @pytest.mark.asyncio
    async def test_no_submitter_skips_activity(self, db_session):
        from app.services.buyplan_notifications import notify_token_rejected

        user = _make_user(db_session)
        plan = _make_plan(db_session, user.id, submitted_by_id=None)

        await notify_token_rejected(plan, db_session)

        activities = db_session.query(ActivityLog).filter_by(activity_type="buyplan_rejected").all()
        assert len(activities) == 0

    @pytest.mark.asyncio
    async def test_no_reason_shows_default(self, db_session):
        from app.services.buyplan_notifications import notify_token_rejected

        submitter = _make_user(db_session)
        approver = _make_user(db_session, "mgr@trioscs.com", "Manager", "manager")
        plan = _make_plan(
            db_session,
            submitter.id,
            approved_by_id=approver.id,
            approval_notes=None,
        )

        await notify_token_rejected(plan, db_session)

        activities = db_session.query(ActivityLog).filter_by(activity_type="buyplan_rejected").all()
        assert len(activities) == 1
        assert "No reason given" in activities[0].subject


# ═══════════════════════════════════════════════════════════════════════
# log_buyplan_activity
# ═══════════════════════════════════════════════════════════════════════


class TestLogBuyplanActivity:
    def test_creates_activity_with_detail(self, db_session):
        from app.services.buyplan_notifications import log_buyplan_activity

        user = _make_user(db_session)
        plan = _make_plan(db_session, user.id, status="active")

        log_buyplan_activity(db_session, user.id, plan, "buyplan_approved", detail="Manager approved")
        db_session.commit()

        act = db_session.query(ActivityLog).filter_by(activity_type="buyplan_approved").first()
        assert act is not None
        assert f"Buy Plan #{plan.id}: Manager approved" == act.subject
        assert f"plan_id={plan.id}" in act.notes
        assert "status=active" in act.notes

    def test_creates_activity_without_detail(self, db_session):
        from app.services.buyplan_notifications import log_buyplan_activity

        user = _make_user(db_session)
        plan = _make_plan(db_session, user.id)

        log_buyplan_activity(db_session, user.id, plan, "buyplan_pending")
        db_session.commit()

        act = db_session.query(ActivityLog).filter_by(activity_type="buyplan_pending").first()
        assert act is not None
        assert act.subject == f"Buy Plan #{plan.id}"

    def test_links_to_requisition(self, db_session):
        from app.services.buyplan_notifications import log_buyplan_activity

        user = _make_user(db_session)
        plan = _make_plan(db_session, user.id)

        log_buyplan_activity(db_session, user.id, plan, "buyplan_submitted")
        db_session.commit()

        act = db_session.query(ActivityLog).filter_by(activity_type="buyplan_submitted").first()
        assert act.requisition_id == plan.requisition_id
