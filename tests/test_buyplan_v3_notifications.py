"""test_buyplan_v3_notifications.py — Tests for notification functions.

Covers: notify_stock_sale_approved (single-path outbox email, W3.8/§5.5),
log_buyplan_activity (audit trail — kept).

Called by: pytest
Depends on: conftest.py, app.services.buyplan_notifications, app.models.approvals
"""

from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from app.constants import ApprovalGateType, ApprovalRequestStatus, ApprovalSubjectType
from app.models import ActivityLog, User
from app.models.approvals import ApprovalOutbox, ApprovalRequest
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
        name="REQ-V3",
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
# notify_stock_sale_approved — single delivery: ONE outbox email to the DLs
# ═══════════════════════════════════════════════════════════════════════


class TestNotifyStockSaleApproved:
    @pytest.mark.asyncio
    async def test_enqueues_stock_sale_email(self, db_session):
        from app.services.buyplan_notifications import notify_stock_sale_approved

        submitter = _make_user(db_session)
        admin = _make_user(db_session, "admin@trioscs.com", "Admin", "admin")
        admin.access_token = "fake-token"
        db_session.commit()

        plan = _make_plan(db_session, submitter.id, approved_by_id=admin.id)
        _add_line(db_session, plan, quantity=50, unit_cost=3.00)
        request = _make_request(db_session, plan)

        with patch("app.services.buyplan_notifications.settings") as mock_settings:
            mock_settings.admin_emails = ["admin@trioscs.com"]
            mock_settings.stock_sale_notify_emails = ["logistics@trioscs.com", "accounting@trioscs.com"]
            await notify_stock_sale_approved(plan, db_session)

        rows = db_session.query(ApprovalOutbox).all()
        assert len(rows) == 1
        assert rows[0].channel == "email"
        assert rows[0].request_id == request.id
        assert rows[0].recipient_user_id == admin.id  # delegated Graph sender
        assert rows[0].payload["to"] == ["logistics@trioscs.com", "accounting@trioscs.com"]
        assert "Stock Sale Approved" in rows[0].payload["subject"]
        assert "LM317T" in rows[0].payload["html"]

    @pytest.mark.asyncio
    async def test_no_inapp_row_written(self, db_session):
        """Single path: the submitter no longer gets an ActivityLog row here (their
        decision notice is the engine's own outbox email)."""
        from app.services.buyplan_notifications import notify_stock_sale_approved

        submitter = _make_user(db_session)
        admin = _make_user(db_session, "admin@trioscs.com", "Admin", "admin")
        plan = _make_plan(db_session, submitter.id)
        _make_request(db_session, plan)

        with patch("app.services.buyplan_notifications.settings") as mock_settings:
            mock_settings.admin_emails = ["admin@trioscs.com"]
            mock_settings.stock_sale_notify_emails = ["logistics@trioscs.com"]
            await notify_stock_sale_approved(plan, db_session)

        assert db_session.query(ActivityLog).count() == 0

    @pytest.mark.asyncio
    async def test_no_dls_configured_enqueues_nothing(self, db_session):
        from app.services.buyplan_notifications import notify_stock_sale_approved

        submitter = _make_user(db_session)
        plan = _make_plan(db_session, submitter.id)
        _make_request(db_session, plan)

        with patch("app.services.buyplan_notifications.settings") as mock_settings:
            mock_settings.admin_emails = []
            mock_settings.stock_sale_notify_emails = []
            await notify_stock_sale_approved(plan, db_session)

        assert db_session.query(ApprovalOutbox).count() == 0

    @pytest.mark.asyncio
    async def test_no_request_enqueues_nothing(self, db_session):
        from app.services.buyplan_notifications import notify_stock_sale_approved

        submitter = _make_user(db_session)
        _make_user(db_session, "admin@trioscs.com", "Admin", "admin")
        plan = _make_plan(db_session, submitter.id)  # no ApprovalRequest anchor

        with patch("app.services.buyplan_notifications.settings") as mock_settings:
            mock_settings.admin_emails = ["admin@trioscs.com"]
            mock_settings.stock_sale_notify_emails = ["logistics@trioscs.com"]
            await notify_stock_sale_approved(plan, db_session)  # no crash

        assert db_session.query(ApprovalOutbox).count() == 0


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
