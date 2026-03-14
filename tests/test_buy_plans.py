"""
test_buy_plans.py — V1→V3 Redirect Shim Tests

Tests that:
- Read endpoints (GET) query BuyPlanV3 + BuyPlanLine and return V1-shaped JSON
- All mutation endpoints (POST/PUT) return HTTP 410
- Access control works on read endpoints
- V3→V1 status mapping is correct

Called by: pytest
Depends on: conftest.py fixtures, app.models.buy_plan (BuyPlanV3, BuyPlanLine)
"""

import secrets
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import require_buyer, require_user
from app.main import app
from app.models import Offer, Quote, Requirement, Requisition, User
from app.models.buy_plan import BuyPlanLine, BuyPlanLineStatus, BuyPlanStatus, BuyPlanV3


# ── Helpers ──────────────────────────────────────────────────────────


def _make_client(db_session: Session, user: User) -> TestClient:
    """Build a TestClient authenticated as the given user."""

    def _override_db():
        yield db_session

    def _override_user():
        return user

    def _override_buyer():
        return user

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_user] = _override_user
    app.dependency_overrides[require_buyer] = _override_buyer
    return TestClient(app)


def _create_v3_plan(db_session, *, with_lines=True, **overrides):
    """Insert a BuyPlanV3 directly for pre-state setup."""
    defaults = {
        "status": BuyPlanStatus.pending.value,
        "total_cost": 500.00,
        "total_revenue": 750.00,
        "submitted_at": datetime.now(timezone.utc),
    }
    defaults.update(overrides)
    plan = BuyPlanV3(**defaults)
    db_session.add(plan)
    db_session.flush()
    if with_lines:
        req = db_session.query(Requirement).filter_by(requisition_id=plan.requisition_id).first()
        offer = db_session.query(Offer).filter_by(requisition_id=plan.requisition_id).first()
        line = BuyPlanLine(
            buy_plan_id=plan.id,
            requirement_id=req.id if req else None,
            offer_id=offer.id if offer else None,
            quantity=1000,
            unit_cost=0.50,
            unit_sell=0.75,
            buyer_id=plan.submitted_by_id,
            status=BuyPlanLineStatus.awaiting_po.value,
        )
        db_session.add(line)
    db_session.commit()
    db_session.refresh(plan)
    return plan


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture()
def buyer_client(db_session, test_user):
    """Client authenticated as the default buyer user."""
    c = _make_client(db_session, test_user)
    yield c
    app.dependency_overrides.clear()


@pytest.fixture()
def admin_client(db_session, admin_user):
    """FastAPI TestClient with admin auth overrides."""
    from app.dependencies import require_admin

    def _override_db():
        yield db_session

    def _override_user():
        return admin_user

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_user] = _override_user
    app.dependency_overrides[require_buyer] = _override_user
    app.dependency_overrides[require_admin] = _override_user

    with TestClient(app) as c:
        yield c

    app.dependency_overrides.clear()


@pytest.fixture()
def manager_client(db_session, manager_user):
    """FastAPI TestClient with manager auth overrides."""

    def _override_db():
        yield db_session

    def _override_user():
        return manager_user

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_user] = _override_user
    app.dependency_overrides[require_buyer] = _override_user

    with TestClient(app) as c:
        yield c

    app.dependency_overrides.clear()


@pytest.fixture()
def sales_client(db_session, sales_user):
    """Client authenticated as sales user (restricted)."""
    c = _make_client(db_session, sales_user)
    yield c
    app.dependency_overrides.clear()


@pytest.fixture()
def trader_client(db_session, trader_user):
    """Client authenticated as trader user (restricted)."""
    c = _make_client(db_session, trader_user)
    yield c
    app.dependency_overrides.clear()


# ── Test: List Buy Plans ─────────────────────────────────────────────


class TestListBuyPlans:
    def test_admin_sees_all(self, admin_client, db_session, admin_user, test_requisition, test_quote, test_offer):
        """Admin can see all buy plans."""
        _create_v3_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=admin_user.id,
        )
        resp = admin_client.get("/api/buy-plans")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) >= 1

    def test_sales_sees_own(
        self, sales_client, db_session, sales_user, admin_user, test_requisition, test_quote, test_offer
    ):
        """Sales user sees only plans they submitted."""
        # Plan submitted by sales user
        _create_v3_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=sales_user.id,
        )
        # Plan submitted by admin (should not be visible to sales)
        _create_v3_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=admin_user.id,
        )
        resp = sales_client.get("/api/buy-plans")
        assert resp.status_code == 200
        data = resp.json()
        for plan in data:
            assert plan["submitted_by_id"] == sales_user.id

    def test_trader_sees_own_only(
        self, trader_client, db_session, trader_user, admin_user, test_requisition, test_quote, test_offer
    ):
        """Trader sees only own plans (none if they didn't submit any)."""
        _create_v3_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=admin_user.id,
        )
        resp = trader_client.get("/api/buy-plans")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 0

    def test_filter_by_v1_status_name(
        self, admin_client, db_session, admin_user, test_requisition, test_quote, test_offer
    ):
        """V1 status name 'pending_approval' maps to V3 'pending'."""
        _create_v3_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=admin_user.id,
            status=BuyPlanStatus.pending.value,
        )
        _create_v3_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=admin_user.id,
            status=BuyPlanStatus.active.value,
        )
        resp = admin_client.get("/api/buy-plans?status=pending_approval")
        assert resp.status_code == 200
        data = resp.json()
        for plan in data:
            assert plan["status"] == "pending_approval"


# ── Test: Get Buy Plan ───────────────────────────────────────────────


class TestGetBuyPlan:
    def test_admin_can_view_any(
        self, admin_client, db_session, admin_user, test_requisition, test_quote, test_offer
    ):
        """Admin can view any plan."""
        plan = _create_v3_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=admin_user.id,
        )
        resp = admin_client.get(f"/api/buy-plans/{plan.id}")
        assert resp.status_code == 200

    def test_buyer_can_view_any(
        self, buyer_client, db_session, admin_user, test_requisition, test_quote, test_offer
    ):
        """Buyer role can view any plan."""
        plan = _create_v3_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=admin_user.id,
        )
        resp = buyer_client.get(f"/api/buy-plans/{plan.id}")
        assert resp.status_code == 200

    def test_sales_can_view_own(
        self, sales_client, db_session, sales_user, test_requisition, test_quote, test_offer
    ):
        """Sales can view their own plan."""
        plan = _create_v3_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=sales_user.id,
        )
        resp = sales_client.get(f"/api/buy-plans/{plan.id}")
        assert resp.status_code == 200

    def test_sales_cannot_view_other(
        self, sales_client, db_session, admin_user, test_requisition, test_quote, test_offer
    ):
        """Sales cannot view another user's plan."""
        plan = _create_v3_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=admin_user.id,
        )
        resp = sales_client.get(f"/api/buy-plans/{plan.id}")
        assert resp.status_code == 403

    def test_trader_cannot_view_other(
        self, trader_client, db_session, admin_user, test_requisition, test_quote, test_offer
    ):
        """Trader cannot view another user's plan."""
        plan = _create_v3_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=admin_user.id,
        )
        resp = trader_client.get(f"/api/buy-plans/{plan.id}")
        assert resp.status_code == 403

    def test_not_found(self, buyer_client):
        """Non-existent plan returns 404."""
        resp = buyer_client.get("/api/buy-plans/99999")
        assert resp.status_code == 404

    def test_v1_dict_shape(
        self, admin_client, db_session, admin_user, test_requisition, test_quote, test_offer
    ):
        """Returned dict has all V1 keys."""
        plan = _create_v3_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=admin_user.id,
        )
        resp = admin_client.get(f"/api/buy-plans/{plan.id}")
        assert resp.status_code == 200
        data = resp.json()
        expected_keys = {
            "id", "requisition_id", "requisition_name", "quote_id",
            "quote_number", "quote_subtotal", "customer_name", "status",
            "line_items", "is_stock_sale", "total_cost", "total_revenue",
            "total_profit", "overall_margin_pct", "sales_order_number",
            "salesperson_notes", "manager_notes", "rejection_reason",
            "submitted_by", "submitted_by_id", "approved_by", "approved_by_id",
            "rejected_by", "rejected_by_id", "submitted_at", "approved_at",
            "rejected_at", "completed_at", "completed_by", "cancelled_at",
            "cancelled_by", "cancellation_reason",
        }
        assert expected_keys.issubset(set(data.keys()))

    def test_status_mapping_pending(
        self, admin_client, db_session, admin_user, test_requisition, test_quote, test_offer
    ):
        """V3 'pending' maps to V1 'pending_approval'."""
        plan = _create_v3_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=admin_user.id,
            status=BuyPlanStatus.pending.value,
        )
        resp = admin_client.get(f"/api/buy-plans/{plan.id}")
        assert resp.json()["status"] == "pending_approval"

    def test_status_mapping_active(
        self, admin_client, db_session, admin_user, test_requisition, test_quote, test_offer
    ):
        """V3 'active' with no PO maps to V1 'approved'."""
        plan = _create_v3_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=admin_user.id,
            status=BuyPlanStatus.active.value,
        )
        resp = admin_client.get(f"/api/buy-plans/{plan.id}")
        assert resp.json()["status"] == "approved"

    def test_status_mapping_completed(
        self, admin_client, db_session, admin_user, test_requisition, test_quote, test_offer
    ):
        """V3 'completed' maps to V1 'complete'."""
        plan = _create_v3_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=admin_user.id,
            status=BuyPlanStatus.completed.value,
            with_lines=False,
        )
        resp = admin_client.get(f"/api/buy-plans/{plan.id}")
        assert resp.json()["status"] == "complete"

    def test_status_mapping_po_entered(
        self, admin_client, db_session, admin_user, test_requisition, test_quote, test_offer
    ):
        """V3 'active' with PO but not all verified maps to V1 'po_entered'."""
        plan = _create_v3_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=admin_user.id,
            status=BuyPlanStatus.active.value,
            with_lines=False,
        )
        # Add line with PO but not verified
        line = BuyPlanLine(
            buy_plan_id=plan.id,
            offer_id=test_offer.id,
            quantity=1000,
            unit_cost=0.50,
            unit_sell=0.75,
            status=BuyPlanLineStatus.pending_verify.value,
            po_number="PO-001",
        )
        db_session.add(line)
        db_session.commit()
        db_session.refresh(plan)

        resp = admin_client.get(f"/api/buy-plans/{plan.id}")
        assert resp.json()["status"] == "po_entered"

    def test_status_mapping_po_confirmed(
        self, admin_client, db_session, admin_user, test_requisition, test_quote, test_offer
    ):
        """V3 'active' with all PO lines verified maps to V1 'po_confirmed'."""
        plan = _create_v3_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=admin_user.id,
            status=BuyPlanStatus.active.value,
            with_lines=False,
        )
        line = BuyPlanLine(
            buy_plan_id=plan.id,
            offer_id=test_offer.id,
            quantity=1000,
            unit_cost=0.50,
            unit_sell=0.75,
            status=BuyPlanLineStatus.verified.value,
            po_number="PO-001",
        )
        db_session.add(line)
        db_session.commit()
        db_session.refresh(plan)

        resp = admin_client.get(f"/api/buy-plans/{plan.id}")
        assert resp.json()["status"] == "po_confirmed"


# ── Test: Token-Based ────────────────────────────────────────────────


class TestTokenBased:
    def test_get_by_token(self, db_session, admin_user, test_requisition, test_quote, test_offer):
        """Public token endpoint returns V1-shaped plan."""
        token = secrets.token_urlsafe(32)
        plan = _create_v3_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=admin_user.id,
            approval_token=token,
        )
        # Token endpoint has no auth, use a plain client
        c = _make_client(db_session, admin_user)
        resp = c.get(f"/api/buy-plans/token/{token}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == plan.id
        app.dependency_overrides.clear()

    def test_invalid_token_404(self, db_session, admin_user):
        """Invalid token returns 404."""
        c = _make_client(db_session, admin_user)
        resp = c.get("/api/buy-plans/token/bogus-token-value")
        assert resp.status_code == 404
        app.dependency_overrides.clear()


# ── Test: For Quote ──────────────────────────────────────────────────


class TestForQuote:
    def test_returns_plan(
        self, buyer_client, db_session, test_user, test_requisition, test_quote, test_offer
    ):
        """Returns the V3 plan for a quote in V1 shape."""
        _create_v3_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=test_user.id,
        )
        resp = buyer_client.get(f"/api/buy-plans/for-quote/{test_quote.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["quote_id"] == test_quote.id

    def test_returns_none(self, buyer_client, test_quote):
        """No plan for quote returns null."""
        resp = buyer_client.get(f"/api/buy-plans/for-quote/{test_quote.id}")
        assert resp.status_code == 200
        assert resp.json() is None

    def test_nonexistent_quote(self, buyer_client):
        """Nonexistent quote returns null (no plan found)."""
        resp = buyer_client.get("/api/buy-plans/for-quote/99999")
        assert resp.status_code == 200
        assert resp.json() is None


# ── Test: Mutations Return 410 ───────────────────────────────────────


class TestMutationsReturn410:
    """All 13 mutation endpoints return 410."""

    def test_create_draft(self, buyer_client):
        resp = buyer_client.post("/api/quotes/1/buy-plan/draft")
        assert resp.status_code == 410

    def test_submit_buy_plan(self, buyer_client):
        resp = buyer_client.post("/api/quotes/1/buy-plan")
        assert resp.status_code == 410

    def test_submit_draft(self, buyer_client):
        resp = buyer_client.put("/api/buy-plans/1/submit")
        assert resp.status_code == 410

    def test_approve(self, buyer_client):
        resp = buyer_client.put("/api/buy-plans/1/approve")
        assert resp.status_code == 410

    def test_reject(self, buyer_client):
        resp = buyer_client.put("/api/buy-plans/1/reject")
        assert resp.status_code == 410

    def test_po_entry(self, buyer_client):
        resp = buyer_client.put("/api/buy-plans/1/po")
        assert resp.status_code == 410

    def test_po_bulk(self, buyer_client):
        resp = buyer_client.put("/api/buy-plans/1/po-bulk")
        assert resp.status_code == 410

    def test_complete(self, buyer_client):
        resp = buyer_client.put("/api/buy-plans/1/complete")
        assert resp.status_code == 410

    def test_cancel(self, buyer_client):
        resp = buyer_client.put("/api/buy-plans/1/cancel")
        assert resp.status_code == 410

    def test_resubmit(self, buyer_client):
        resp = buyer_client.put("/api/buy-plans/1/resubmit")
        assert resp.status_code == 410

    def test_token_approve(self, buyer_client):
        resp = buyer_client.put("/api/buy-plans/token/abc/approve")
        assert resp.status_code == 410

    def test_token_reject(self, buyer_client):
        resp = buyer_client.put("/api/buy-plans/token/abc/reject")
        assert resp.status_code == 410

    def test_verify_po(self, buyer_client):
        resp = buyer_client.get("/api/buy-plans/1/verify-po")
        assert resp.status_code == 410
