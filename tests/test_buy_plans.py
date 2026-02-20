"""
test_buy_plans.py — Buy Plan Workflow Tests

Covers the full 7-status state machine: pending_approval → approved →
po_entered → po_confirmed → complete, with rejected/cancelled branches.

Tests: submit, list, get detail, approve, reject, token-based actions,
PO entry (single & bulk), verify PO, complete, cancel, resubmit,
for-quote lookup, and full status transition flows.

Includes regression tests for:
- Bug 2: Access control on get_buy_plan
- Bug 3: Resubmit authorization
- Bug 4: rejected_by surfaced in API response
"""

import secrets
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import require_buyer, require_user
from app.main import app
from app.models import BuyPlan, Offer, Quote, Requisition, User


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


def _create_buy_plan(db_session: Session, **overrides) -> BuyPlan:
    """Insert a BuyPlan directly for pre-state setup."""
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
    db_session.add(plan)
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
    c = _make_client(db_session, admin_user)
    yield c
    app.dependency_overrides.clear()


@pytest.fixture()
def manager_client(db_session, manager_user):
    c = _make_client(db_session, manager_user)
    yield c
    app.dependency_overrides.clear()


@pytest.fixture()
def sales_client(db_session, sales_user):
    c = _make_client(db_session, sales_user)
    yield c
    app.dependency_overrides.clear()


@pytest.fixture()
def trader_client(db_session, trader_user):
    c = _make_client(db_session, trader_user)
    yield c
    app.dependency_overrides.clear()


@pytest.fixture()
def noauth_client(db_session):
    """Client with DB override but no auth override (for token endpoints)."""

    def _override_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_db
    c = TestClient(app)
    yield c
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def mock_background_tasks(monkeypatch):
    """Prevent asyncio.create_task from spawning real background work."""
    monkeypatch.setattr("asyncio.create_task", lambda coro: coro.close())


# ── 1. Submit (POST /api/quotes/{qid}/buy-plan) ─────────────────────


class TestSubmitBuyPlan:
    def test_submit_success(self, db_session, sales_client, sales_user, test_quote, test_offer):
        """Sales user can submit a buy plan for a sent quote."""
        r = sales_client.post(
            f"/api/quotes/{test_quote.id}/buy-plan",
            json={"offer_ids": [test_offer.id]},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["status"] == "pending_approval"
        assert data["buy_plan_id"] is not None

    def test_submit_missing_offers(self, sales_client, test_quote):
        """Submit with no offers → 400."""
        r = sales_client.post(
            f"/api/quotes/{test_quote.id}/buy-plan",
            json={"offer_ids": []},
        )
        assert r.status_code == 400

    def test_submit_nonexistent_quote(self, sales_client):
        """Submit against a nonexistent quote → 404."""
        r = sales_client.post(
            "/api/quotes/99999/buy-plan",
            json={"offer_ids": [1]},
        )
        assert r.status_code == 404

    def test_submit_with_notes_and_qtys(self, db_session, sales_client, sales_user, test_quote, test_offer):
        """Submit with salesperson notes and custom quantities."""
        r = sales_client.post(
            f"/api/quotes/{test_quote.id}/buy-plan",
            json={
                "offer_ids": [test_offer.id],
                "salesperson_notes": "Rush order for ACME",
                "plan_qtys": {str(test_offer.id): 500},
            },
        )
        assert r.status_code == 200
        plan = db_session.get(BuyPlan, r.json()["buy_plan_id"])
        assert plan.salesperson_notes == "Rush order for ACME"
        assert plan.line_items[0]["plan_qty"] == 500

    def test_submit_marks_quote_won(self, db_session, sales_client, sales_user, test_quote, test_offer):
        """Submitting a buy plan marks the quote as won."""
        sales_client.post(
            f"/api/quotes/{test_quote.id}/buy-plan",
            json={"offer_ids": [test_offer.id]},
        )
        db_session.refresh(test_quote)
        assert test_quote.status == "won"
        assert test_quote.result == "won"


# ── 2. List (GET /api/buy-plans) ────────────────────────────────────


class TestListBuyPlans:
    def _seed(self, db_session, test_requisition, test_quote, sales_user, test_user):
        """Create plans by different users."""
        p1 = _create_buy_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=sales_user.id,
        )
        p2 = _create_buy_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=test_user.id,
        )
        return p1, p2

    def test_admin_sees_all(self, db_session, admin_client, test_requisition, test_quote, sales_user, test_user):
        self._seed(db_session, test_requisition, test_quote, sales_user, test_user)
        r = admin_client.get("/api/buy-plans")
        assert r.status_code == 200
        assert len(r.json()) == 2

    def test_sales_sees_own(self, db_session, sales_client, test_requisition, test_quote, sales_user, test_user):
        self._seed(db_session, test_requisition, test_quote, sales_user, test_user)
        r = sales_client.get("/api/buy-plans")
        assert r.status_code == 200
        plans = r.json()
        assert len(plans) == 1
        assert plans[0]["submitted_by_id"] == sales_user.id

    def test_trader_sees_own(self, db_session, trader_client, trader_user, test_requisition, test_quote, sales_user, test_user):
        self._seed(db_session, test_requisition, test_quote, sales_user, test_user)
        # Trader has no plans, so should see 0
        r = trader_client.get("/api/buy-plans")
        assert r.status_code == 200
        assert len(r.json()) == 0

    def test_buyer_sees_all(self, db_session, buyer_client, test_requisition, test_quote, sales_user, test_user):
        self._seed(db_session, test_requisition, test_quote, sales_user, test_user)
        r = buyer_client.get("/api/buy-plans")
        assert r.status_code == 200
        assert len(r.json()) == 2

    def test_manager_sees_all(self, db_session, manager_client, test_requisition, test_quote, sales_user, test_user):
        self._seed(db_session, test_requisition, test_quote, sales_user, test_user)
        r = manager_client.get("/api/buy-plans")
        assert r.status_code == 200
        assert len(r.json()) == 2

    def test_filter_by_status(self, db_session, admin_client, test_requisition, test_quote, test_user):
        _create_buy_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=test_user.id,
            status="pending_approval",
        )
        _create_buy_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=test_user.id,
            status="approved",
        )
        r = admin_client.get("/api/buy-plans?status=approved")
        assert len(r.json()) == 1
        assert r.json()[0]["status"] == "approved"


# ── 3. Get Detail (GET /api/buy-plans/{id}) — Bug 2 regression ──────


class TestGetBuyPlan:
    def test_admin_any_plan(self, db_session, admin_client, test_requisition, test_quote, sales_user):
        plan = _create_buy_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=sales_user.id,
        )
        r = admin_client.get(f"/api/buy-plans/{plan.id}")
        assert r.status_code == 200

    def test_manager_any_plan(self, db_session, manager_client, test_requisition, test_quote, sales_user):
        plan = _create_buy_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=sales_user.id,
        )
        r = manager_client.get(f"/api/buy-plans/{plan.id}")
        assert r.status_code == 200

    def test_buyer_any_plan(self, db_session, buyer_client, test_requisition, test_quote, sales_user):
        plan = _create_buy_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=sales_user.id,
        )
        r = buyer_client.get(f"/api/buy-plans/{plan.id}")
        assert r.status_code == 200

    def test_sales_own_plan(self, db_session, sales_client, sales_user, test_requisition, test_quote):
        plan = _create_buy_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=sales_user.id,
        )
        r = sales_client.get(f"/api/buy-plans/{plan.id}")
        assert r.status_code == 200

    def test_trader_own_plan(self, db_session, trader_client, trader_user, test_requisition, test_quote):
        plan = _create_buy_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=trader_user.id,
        )
        r = trader_client.get(f"/api/buy-plans/{plan.id}")
        assert r.status_code == 200

    def test_sales_other_forbidden(self, db_session, sales_client, test_requisition, test_quote, test_user):
        """Sales cannot view another user's plan (Bug 2 regression)."""
        plan = _create_buy_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=test_user.id,  # buyer user, not sales_user
        )
        r = sales_client.get(f"/api/buy-plans/{plan.id}")
        assert r.status_code == 403

    def test_trader_other_forbidden(self, db_session, trader_client, test_requisition, test_quote, test_user):
        """Trader cannot view another user's plan (Bug 2 regression)."""
        plan = _create_buy_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=test_user.id,
        )
        r = trader_client.get(f"/api/buy-plans/{plan.id}")
        assert r.status_code == 403

    def test_nonexistent(self, admin_client):
        r = admin_client.get("/api/buy-plans/99999")
        assert r.status_code == 404

    def test_dict_shape(self, db_session, admin_client, test_requisition, test_quote, test_user):
        """Response dict includes all expected keys."""
        plan = _create_buy_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=test_user.id,
        )
        r = admin_client.get(f"/api/buy-plans/{plan.id}")
        data = r.json()
        expected_keys = {
            "id", "requisition_id", "requisition_name", "quote_id",
            "quote_number", "quote_subtotal", "customer_name", "status",
            "line_items", "is_stock_sale",
            "total_cost", "total_revenue", "total_profit", "overall_margin_pct",
            "sales_order_number", "salesperson_notes",
            "manager_notes", "rejection_reason", "submitted_by",
            "submitted_by_id", "approved_by", "approved_by_id",
            "rejected_by", "rejected_by_id",
            "submitted_at", "approved_at", "rejected_at",
            "completed_at", "completed_by", "cancelled_at", "cancelled_by",
            "cancellation_reason",
        }
        assert expected_keys.issubset(set(data.keys()))


# ── 4. Approve (PUT /api/buy-plans/{id}/approve) ────────────────────


class TestApproveBuyPlan:
    def test_manager_approves(self, db_session, manager_client, test_requisition, test_quote, sales_user):
        plan = _create_buy_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=sales_user.id,
        )
        r = manager_client.put(
            f"/api/buy-plans/{plan.id}/approve",
            json={"sales_order_number": "SO-001"},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "approved"

    def test_admin_approves(self, db_session, admin_client, test_requisition, test_quote, sales_user):
        plan = _create_buy_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=sales_user.id,
        )
        r = admin_client.put(
            f"/api/buy-plans/{plan.id}/approve",
            json={"sales_order_number": "SO-002"},
        )
        assert r.status_code == 200

    def test_buyer_forbidden(self, db_session, buyer_client, test_requisition, test_quote, sales_user):
        plan = _create_buy_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=sales_user.id,
        )
        r = buyer_client.put(
            f"/api/buy-plans/{plan.id}/approve",
            json={"sales_order_number": "SO-003"},
        )
        assert r.status_code == 403

    def test_sales_forbidden(self, db_session, sales_client, test_requisition, test_quote, sales_user):
        plan = _create_buy_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=sales_user.id,
        )
        r = sales_client.put(
            f"/api/buy-plans/{plan.id}/approve",
            json={"sales_order_number": "SO-004"},
        )
        assert r.status_code == 403

    def test_missing_so_number(self, db_session, manager_client, test_requisition, test_quote, sales_user):
        plan = _create_buy_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=sales_user.id,
        )
        r = manager_client.put(
            f"/api/buy-plans/{plan.id}/approve",
            json={},
        )
        assert r.status_code == 422  # Pydantic validates sales_order_number is required

    def test_wrong_status(self, db_session, manager_client, test_requisition, test_quote, sales_user):
        plan = _create_buy_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=sales_user.id,
            status="approved",
        )
        r = manager_client.put(
            f"/api/buy-plans/{plan.id}/approve",
            json={"sales_order_number": "SO-005"},
        )
        assert r.status_code == 400

    def test_approve_with_notes(self, db_session, manager_client, test_requisition, test_quote, sales_user):
        plan = _create_buy_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=sales_user.id,
        )
        r = manager_client.put(
            f"/api/buy-plans/{plan.id}/approve",
            json={"sales_order_number": "SO-006", "manager_notes": "Looks good"},
        )
        assert r.status_code == 200
        db_session.refresh(plan)
        assert plan.manager_notes == "Looks good"


# ── 5. Reject (PUT /api/buy-plans/{id}/reject) — Bug 4 regression ───


class TestRejectBuyPlan:
    def test_manager_rejects(self, db_session, manager_client, test_requisition, test_quote, sales_user):
        plan = _create_buy_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=sales_user.id,
        )
        r = manager_client.put(
            f"/api/buy-plans/{plan.id}/reject",
            json={"reason": "Price too high"},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "rejected"

    def test_admin_rejects(self, db_session, admin_client, test_requisition, test_quote, sales_user):
        plan = _create_buy_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=sales_user.id,
        )
        r = admin_client.put(
            f"/api/buy-plans/{plan.id}/reject",
            json={"reason": "Wrong vendor"},
        )
        assert r.status_code == 200

    def test_buyer_forbidden(self, db_session, buyer_client, test_requisition, test_quote, sales_user):
        plan = _create_buy_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=sales_user.id,
        )
        r = buyer_client.put(
            f"/api/buy-plans/{plan.id}/reject",
            json={"reason": "test"},
        )
        assert r.status_code == 403

    def test_wrong_status(self, db_session, manager_client, test_requisition, test_quote, sales_user):
        plan = _create_buy_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=sales_user.id,
            status="approved",
        )
        r = manager_client.put(
            f"/api/buy-plans/{plan.id}/reject",
            json={"reason": "test"},
        )
        assert r.status_code == 400

    def test_dict_shows_rejected_by(self, db_session, manager_client, manager_user, test_requisition, test_quote, sales_user):
        """Bug 4 regression: rejected_by shows who rejected."""
        plan = _create_buy_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=sales_user.id,
        )
        manager_client.put(
            f"/api/buy-plans/{plan.id}/reject",
            json={"reason": "Bad pricing"},
        )
        r = manager_client.get(f"/api/buy-plans/{plan.id}")
        data = r.json()
        assert data["rejected_by"] == manager_user.name
        assert data["rejected_by_id"] == manager_user.id


# ── 6. Token-Based (GET/PUT /api/buy-plans/token/{t}/...) ───────────


class TestTokenBased:
    def test_get_by_token(self, db_session, noauth_client, test_requisition, test_quote, test_user):
        plan = _create_buy_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=test_user.id,
        )
        r = noauth_client.get(f"/api/buy-plans/token/{plan.approval_token}")
        assert r.status_code == 200
        assert r.json()["id"] == plan.id

    def test_invalid_token(self, noauth_client):
        r = noauth_client.get("/api/buy-plans/token/nonexistent-token")
        assert r.status_code == 404

    def test_approve_by_token(self, db_session, noauth_client, test_requisition, test_quote, test_user):
        plan = _create_buy_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=test_user.id,
        )
        r = noauth_client.put(
            f"/api/buy-plans/token/{plan.approval_token}/approve",
            json={"sales_order_number": "SO-TOKEN-1"},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "approved"

    def test_approve_token_missing_so(self, db_session, noauth_client, test_requisition, test_quote, test_user):
        plan = _create_buy_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=test_user.id,
        )
        r = noauth_client.put(
            f"/api/buy-plans/token/{plan.approval_token}/approve",
            json={},
        )
        assert r.status_code == 422  # Pydantic validates sales_order_number is required

    def test_reject_by_token(self, db_session, noauth_client, test_requisition, test_quote, test_user):
        plan = _create_buy_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=test_user.id,
        )
        r = noauth_client.put(
            f"/api/buy-plans/token/{plan.approval_token}/reject",
            json={"reason": "Token-based rejection"},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "rejected"

    def test_wrong_status_token(self, db_session, noauth_client, test_requisition, test_quote, test_user):
        plan = _create_buy_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=test_user.id,
            status="approved",
        )
        r = noauth_client.put(
            f"/api/buy-plans/token/{plan.approval_token}/approve",
            json={"sales_order_number": "SO-X"},
        )
        assert r.status_code == 400


# ── 7. Single PO Entry (PUT /api/buy-plans/{id}/po) ─────────────────


class TestSinglePO:
    def test_po_entry_success(self, db_session, buyer_client, test_user, test_requisition, test_quote, sales_user):
        plan = _create_buy_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=sales_user.id,
            status="approved",
        )
        r = buyer_client.put(
            f"/api/buy-plans/{plan.id}/po",
            json={"line_index": 0, "po_number": "PO-001"},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "po_entered"

    def test_po_wrong_status(self, db_session, buyer_client, test_requisition, test_quote, sales_user):
        plan = _create_buy_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=sales_user.id,
            status="pending_approval",
        )
        r = buyer_client.put(
            f"/api/buy-plans/{plan.id}/po",
            json={"line_index": 0, "po_number": "PO-002"},
        )
        assert r.status_code == 400

    def test_po_missing_fields(self, db_session, buyer_client, test_requisition, test_quote, sales_user):
        plan = _create_buy_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=sales_user.id,
            status="approved",
        )
        r = buyer_client.put(
            f"/api/buy-plans/{plan.id}/po",
            json={"line_index": 0},
        )
        assert r.status_code == 422  # Pydantic validates po_number is required

    def test_po_invalid_line_index(self, db_session, buyer_client, test_requisition, test_quote, sales_user):
        plan = _create_buy_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=sales_user.id,
            status="approved",
        )
        r = buyer_client.put(
            f"/api/buy-plans/{plan.id}/po",
            json={"line_index": 99, "po_number": "PO-003"},
        )
        assert r.status_code == 400


# ── 8. Bulk PO (PUT /api/buy-plans/{id}/po-bulk) ────────────────────


class TestBulkPO:
    def test_bulk_entry(self, db_session, buyer_client, test_requisition, test_quote, sales_user):
        plan = _create_buy_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=sales_user.id,
            status="approved",
        )
        r = buyer_client.put(
            f"/api/buy-plans/{plan.id}/po-bulk",
            json={"entries": [{"line_index": 0, "po_number": "PO-BULK-1"}]},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "po_entered"

    def test_bulk_edit_resets_verification(self, db_session, buyer_client, test_requisition, test_quote, sales_user):
        """Editing an existing PO resets verification flags."""
        plan = _create_buy_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=sales_user.id,
            status="po_entered",
            line_items=[
                {
                    "offer_id": 1, "mpn": "LM317T", "vendor_name": "Arrow",
                    "qty": 1000, "cost_price": 0.50, "po_number": "PO-OLD",
                    "po_entered_at": "2026-01-01T00:00:00",
                    "po_sent_at": "2026-01-01T12:00:00",
                    "po_recipient": "vendor@arrow.com",
                    "po_verified": True,
                }
            ],
        )
        r = buyer_client.put(
            f"/api/buy-plans/{plan.id}/po-bulk",
            json={"entries": [{"line_index": 0, "po_number": "PO-NEW"}]},
        )
        assert r.status_code == 200
        db_session.refresh(plan)
        assert plan.line_items[0]["po_number"] == "PO-NEW"
        assert plan.line_items[0]["po_verified"] is False

    def test_bulk_clear(self, db_session, buyer_client, test_requisition, test_quote, sales_user):
        """Clearing PO reverts status to approved."""
        plan = _create_buy_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=sales_user.id,
            status="po_entered",
            line_items=[
                {
                    "offer_id": 1, "mpn": "LM317T", "vendor_name": "Arrow",
                    "qty": 1000, "cost_price": 0.50, "po_number": "PO-X",
                    "po_entered_at": "2026-01-01T00:00:00",
                    "po_sent_at": None, "po_recipient": None, "po_verified": False,
                }
            ],
        )
        r = buyer_client.put(
            f"/api/buy-plans/{plan.id}/po-bulk",
            json={"entries": [{"line_index": 0, "po_number": ""}]},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "approved"

    def test_bulk_wrong_status(self, db_session, buyer_client, test_requisition, test_quote, sales_user):
        plan = _create_buy_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=sales_user.id,
            status="pending_approval",
        )
        r = buyer_client.put(
            f"/api/buy-plans/{plan.id}/po-bulk",
            json={"entries": [{"line_index": 0, "po_number": "PO-Y"}]},
        )
        assert r.status_code == 400

    def test_bulk_empty_entries(self, db_session, buyer_client, test_requisition, test_quote, sales_user):
        plan = _create_buy_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=sales_user.id,
            status="approved",
        )
        r = buyer_client.put(
            f"/api/buy-plans/{plan.id}/po-bulk",
            json={"entries": []},
        )
        assert r.status_code == 400


# ── 9. Verify PO (GET /api/buy-plans/{id}/verify-po) ────────────────


class TestVerifyPO:
    def test_returns_structure(self, db_session, buyer_client, test_requisition, test_quote, sales_user):
        plan = _create_buy_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=sales_user.id,
            status="po_entered",
        )
        with patch("app.services.buyplan_service.verify_po_sent", return_value=[]):
            r = buyer_client.get(f"/api/buy-plans/{plan.id}/verify-po")
        assert r.status_code == 200
        data = r.json()
        assert "plan_id" in data
        assert "verifications" in data
        assert "line_items" in data

    def test_nonexistent(self, buyer_client):
        r = buyer_client.get("/api/buy-plans/99999/verify-po")
        assert r.status_code == 404


# ── 10. Complete (PUT /api/buy-plans/{id}/complete) ──────────────────


class TestCompleteBuyPlan:
    def test_admin_completes(self, db_session, admin_client, test_requisition, test_quote, sales_user):
        plan = _create_buy_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=sales_user.id,
            status="po_confirmed",
        )
        r = admin_client.put(f"/api/buy-plans/{plan.id}/complete")
        assert r.status_code == 200
        assert r.json()["status"] == "complete"

    def test_manager_completes(self, db_session, manager_client, test_requisition, test_quote, sales_user):
        plan = _create_buy_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=sales_user.id,
            status="po_confirmed",
        )
        r = manager_client.put(f"/api/buy-plans/{plan.id}/complete")
        assert r.status_code == 200

    def test_buyer_forbidden(self, db_session, buyer_client, test_requisition, test_quote, sales_user):
        plan = _create_buy_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=sales_user.id,
            status="po_confirmed",
        )
        r = buyer_client.put(f"/api/buy-plans/{plan.id}/complete")
        assert r.status_code == 403

    def test_wrong_status(self, db_session, admin_client, test_requisition, test_quote, sales_user):
        plan = _create_buy_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=sales_user.id,
            status="approved",
        )
        r = admin_client.put(f"/api/buy-plans/{plan.id}/complete")
        assert r.status_code == 400


# ── 11. Cancel (PUT /api/buy-plans/{id}/cancel) ─────────────────────


class TestCancelBuyPlan:
    def test_pending_by_submitter(self, db_session, sales_client, sales_user, test_requisition, test_quote):
        plan = _create_buy_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=sales_user.id,
        )
        r = sales_client.put(
            f"/api/buy-plans/{plan.id}/cancel",
            json={"reason": "Changed mind"},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "cancelled"

    def test_pending_by_manager(self, db_session, manager_client, test_requisition, test_quote, sales_user):
        plan = _create_buy_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=sales_user.id,
        )
        r = manager_client.put(
            f"/api/buy-plans/{plan.id}/cancel",
            json={"reason": "Budget cut"},
        )
        assert r.status_code == 200

    def test_pending_by_other_forbidden(self, db_session, trader_client, test_requisition, test_quote, sales_user):
        """Non-submitter, non-admin can't cancel another user's pending plan."""
        plan = _create_buy_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=sales_user.id,
        )
        r = trader_client.put(
            f"/api/buy-plans/{plan.id}/cancel",
            json={"reason": "test"},
        )
        assert r.status_code == 403

    def test_approved_by_manager(self, db_session, manager_client, test_requisition, test_quote, sales_user):
        plan = _create_buy_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=sales_user.id,
            status="approved",
        )
        r = manager_client.put(
            f"/api/buy-plans/{plan.id}/cancel",
            json={"reason": "Vendor issue"},
        )
        assert r.status_code == 200

    def test_approved_by_buyer_forbidden(self, db_session, buyer_client, test_requisition, test_quote, sales_user):
        """Buyer (non-admin/manager) cannot cancel an approved plan."""
        plan = _create_buy_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=sales_user.id,
            status="approved",
        )
        r = buyer_client.put(
            f"/api/buy-plans/{plan.id}/cancel",
            json={"reason": "test"},
        )
        assert r.status_code == 403

    def test_approved_with_pos_blocked(self, db_session, manager_client, test_requisition, test_quote, sales_user):
        """Cannot cancel approved plan when POs already entered."""
        plan = _create_buy_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=sales_user.id,
            status="approved",
            line_items=[
                {
                    "offer_id": 1, "mpn": "LM317T", "vendor_name": "Arrow",
                    "qty": 1000, "cost_price": 0.50, "po_number": "PO-123",
                    "po_entered_at": "2026-01-01", "po_sent_at": None,
                    "po_recipient": None, "po_verified": False,
                }
            ],
        )
        r = manager_client.put(
            f"/api/buy-plans/{plan.id}/cancel",
            json={"reason": "test"},
        )
        assert r.status_code == 400

    def test_wrong_status(self, db_session, admin_client, test_requisition, test_quote, sales_user):
        plan = _create_buy_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=sales_user.id,
            status="po_entered",
        )
        r = admin_client.put(
            f"/api/buy-plans/{plan.id}/cancel",
            json={"reason": "test"},
        )
        assert r.status_code == 400

    def test_cancel_with_reason(self, db_session, sales_client, sales_user, test_requisition, test_quote):
        plan = _create_buy_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=sales_user.id,
        )
        sales_client.put(
            f"/api/buy-plans/{plan.id}/cancel",
            json={"reason": "Customer cancelled order"},
        )
        db_session.refresh(plan)
        assert plan.cancellation_reason == "Customer cancelled order"


# ── 12. Resubmit (PUT /api/buy-plans/{id}/resubmit) — Bug 3 ────────


class TestResubmitBuyPlan:
    def test_by_submitter(self, db_session, sales_client, sales_user, test_requisition, test_quote):
        plan = _create_buy_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=sales_user.id,
            status="rejected",
        )
        r = sales_client.put(
            f"/api/buy-plans/{plan.id}/resubmit",
            json={"salesperson_notes": "Updated pricing"},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "pending_approval"

    def test_by_admin(self, db_session, admin_client, test_requisition, test_quote, sales_user):
        plan = _create_buy_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=sales_user.id,
            status="rejected",
        )
        r = admin_client.put(
            f"/api/buy-plans/{plan.id}/resubmit",
            json={},
        )
        assert r.status_code == 200

    def test_by_manager(self, db_session, manager_client, test_requisition, test_quote, sales_user):
        plan = _create_buy_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=sales_user.id,
            status="rejected",
        )
        r = manager_client.put(
            f"/api/buy-plans/{plan.id}/resubmit",
            json={},
        )
        assert r.status_code == 200

    def test_other_sales_forbidden(self, db_session, trader_client, test_requisition, test_quote, sales_user):
        """Bug 3 regression: another trader can't resubmit someone else's plan."""
        plan = _create_buy_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=sales_user.id,
            status="rejected",
        )
        r = trader_client.put(
            f"/api/buy-plans/{plan.id}/resubmit",
            json={},
        )
        assert r.status_code == 403

    def test_buyer_not_submitter_forbidden(self, db_session, buyer_client, test_user, test_requisition, test_quote, sales_user):
        """Bug 3 regression: buyer who didn't submit can't resubmit."""
        plan = _create_buy_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=sales_user.id,
            status="rejected",
        )
        r = buyer_client.put(
            f"/api/buy-plans/{plan.id}/resubmit",
            json={},
        )
        assert r.status_code == 403

    def test_wrong_status(self, db_session, sales_client, sales_user, test_requisition, test_quote):
        plan = _create_buy_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=sales_user.id,
            status="approved",
        )
        r = sales_client.put(
            f"/api/buy-plans/{plan.id}/resubmit",
            json={},
        )
        assert r.status_code == 400

    def test_cancelled_plan(self, db_session, sales_client, sales_user, test_requisition, test_quote):
        """Can resubmit a cancelled plan."""
        plan = _create_buy_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=sales_user.id,
            status="cancelled",
        )
        r = sales_client.put(
            f"/api/buy-plans/{plan.id}/resubmit",
            json={},
        )
        assert r.status_code == 200

    def test_resets_po_fields(self, db_session, sales_client, sales_user, test_requisition, test_quote):
        """Resubmitted plan has PO fields cleared."""
        plan = _create_buy_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=sales_user.id,
            status="rejected",
            line_items=[
                {
                    "offer_id": 1, "mpn": "LM317T", "vendor_name": "Arrow",
                    "qty": 1000, "cost_price": 0.50, "po_number": "PO-OLD",
                    "po_entered_at": "2026-01-01", "po_sent_at": "2026-01-02",
                    "po_recipient": "vendor@test.com", "po_verified": True,
                }
            ],
        )
        r = sales_client.put(
            f"/api/buy-plans/{plan.id}/resubmit",
            json={},
        )
        new_id = r.json()["new_plan_id"]
        new_plan = db_session.get(BuyPlan, new_id)
        assert new_plan.line_items[0]["po_number"] is None
        assert new_plan.line_items[0]["po_verified"] is False


# ── 13. For Quote (GET /api/buy-plans/for-quote/{qid}) ──────────────


class TestForQuote:
    def test_returns_plan(self, db_session, buyer_client, test_requisition, test_quote, test_user):
        plan = _create_buy_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=test_user.id,
        )
        r = buyer_client.get(f"/api/buy-plans/for-quote/{test_quote.id}")
        assert r.status_code == 200
        assert r.json()["id"] == plan.id

    def test_none(self, buyer_client, test_quote):
        r = buyer_client.get(f"/api/buy-plans/for-quote/{test_quote.id}")
        assert r.status_code == 200
        assert r.json() is None

    def test_nonexistent_quote(self, buyer_client):
        r = buyer_client.get("/api/buy-plans/for-quote/99999")
        assert r.status_code == 200
        assert r.json() is None


# ── 14. Status Transitions ──────────────────────────────────────────


class TestStatusTransitions:
    def test_full_happy_path(
        self, db_session, sales_client, sales_user, manager_client, buyer_client,
        admin_client, test_quote, test_offer, test_requisition,
    ):
        """Full lifecycle: pending → approved → po_entered → po_confirmed → complete."""
        # Submit
        r = sales_client.post(
            f"/api/quotes/{test_quote.id}/buy-plan",
            json={"offer_ids": [test_offer.id]},
        )
        assert r.status_code == 200
        plan_id = r.json()["buy_plan_id"]

        # Approve
        r = manager_client.put(
            f"/api/buy-plans/{plan_id}/approve",
            json={"sales_order_number": "SO-HAPPY"},
        )
        assert r.json()["status"] == "approved"

        # Enter PO
        r = buyer_client.put(
            f"/api/buy-plans/{plan_id}/po",
            json={"line_index": 0, "po_number": "PO-HAPPY"},
        )
        assert r.json()["status"] == "po_entered"

        # Simulate po_confirmed (manually — verification is async)
        plan = db_session.get(BuyPlan, plan_id)
        plan.status = "po_confirmed"
        db_session.commit()

        # Complete
        r = admin_client.put(f"/api/buy-plans/{plan_id}/complete")
        assert r.json()["status"] == "complete"

    def test_reject_and_resubmit_cycle(
        self, db_session, sales_client, sales_user, manager_client,
        test_quote, test_offer, test_requisition,
    ):
        """Submit → reject → resubmit → approve."""
        r = sales_client.post(
            f"/api/quotes/{test_quote.id}/buy-plan",
            json={"offer_ids": [test_offer.id]},
        )
        plan_id = r.json()["buy_plan_id"]

        # Reject
        r = manager_client.put(
            f"/api/buy-plans/{plan_id}/reject",
            json={"reason": "Need lower price"},
        )
        assert r.json()["status"] == "rejected"

        # Resubmit
        r = sales_client.put(
            f"/api/buy-plans/{plan_id}/resubmit",
            json={"salesperson_notes": "Negotiated lower price"},
        )
        new_plan_id = r.json()["new_plan_id"]
        assert r.json()["status"] == "pending_approval"

        # Approve resubmitted plan
        r = manager_client.put(
            f"/api/buy-plans/{new_plan_id}/approve",
            json={"sales_order_number": "SO-RESUB"},
        )
        assert r.json()["status"] == "approved"

    def test_cancel_and_resubmit_cycle(
        self, db_session, sales_client, sales_user, manager_client,
        test_quote, test_offer, test_requisition,
    ):
        """Submit → cancel → resubmit → approve."""
        r = sales_client.post(
            f"/api/quotes/{test_quote.id}/buy-plan",
            json={"offer_ids": [test_offer.id]},
        )
        plan_id = r.json()["buy_plan_id"]

        # Cancel
        r = sales_client.put(
            f"/api/buy-plans/{plan_id}/cancel",
            json={"reason": "Customer asked to hold"},
        )
        assert r.json()["status"] == "cancelled"

        # Resubmit
        r = sales_client.put(
            f"/api/buy-plans/{plan_id}/resubmit",
            json={},
        )
        new_plan_id = r.json()["new_plan_id"]

        # Approve
        r = manager_client.put(
            f"/api/buy-plans/{new_plan_id}/approve",
            json={"sales_order_number": "SO-CANCEL-RESUB"},
        )
        assert r.json()["status"] == "approved"


# ── 15. Stock Sale Detection ───────────────────────────────────────────


class TestStockSaleDetection:
    """Verify is_stock_sale flag is set correctly on submit and resubmit."""

    def test_all_stock_vendors(self, db_session, sales_client, sales_user, test_quote):
        """All vendor_names match stock_sale_vendor_names → is_stock_sale=True."""
        # Create an offer with a stock sale vendor name
        from app.models import Offer
        offer = Offer(
            requisition_id=test_quote.requisition_id,
            vendor_name="Trio Supply Chain",
            mpn="LM317T",
            qty_available=500,
            unit_price=0.30,
            entered_by_id=sales_user.id,
            status="active",
        )
        db_session.add(offer)
        db_session.commit()
        db_session.refresh(offer)

        r = sales_client.post(
            f"/api/quotes/{test_quote.id}/buy-plan",
            json={"offer_ids": [offer.id]},
        )
        assert r.status_code == 200
        plan = db_session.get(BuyPlan, r.json()["buy_plan_id"])
        assert plan.is_stock_sale is True

    def test_mixed_vendors(self, db_session, sales_client, sales_user, test_quote, test_offer):
        """Mix of stock + external vendors → is_stock_sale=False."""
        from app.models import Offer
        stock_offer = Offer(
            requisition_id=test_quote.requisition_id,
            vendor_name="Trio",
            mpn="LM317T",
            qty_available=200,
            unit_price=0.25,
            entered_by_id=sales_user.id,
            status="active",
        )
        db_session.add(stock_offer)
        db_session.commit()
        db_session.refresh(stock_offer)

        r = sales_client.post(
            f"/api/quotes/{test_quote.id}/buy-plan",
            json={"offer_ids": [test_offer.id, stock_offer.id]},
        )
        assert r.status_code == 200
        plan = db_session.get(BuyPlan, r.json()["buy_plan_id"])
        assert plan.is_stock_sale is False

    def test_external_vendors(self, db_session, sales_client, sales_user, test_quote, test_offer):
        """All external vendors → is_stock_sale=False."""
        r = sales_client.post(
            f"/api/quotes/{test_quote.id}/buy-plan",
            json={"offer_ids": [test_offer.id]},
        )
        assert r.status_code == 200
        plan = db_session.get(BuyPlan, r.json()["buy_plan_id"])
        assert plan.is_stock_sale is False

    def test_resubmit_preserves_detection(self, db_session, sales_client, sales_user, test_quote, test_requisition):
        """Resubmit of a stock sale plan re-detects is_stock_sale."""
        plan = _create_buy_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=sales_user.id,
            status="rejected",
            line_items=[
                {
                    "offer_id": 1, "mpn": "LM317T", "vendor_name": "Trio",
                    "qty": 1000, "plan_qty": 1000, "cost_price": 0.30,
                    "lead_time": "stock", "condition": "new",
                    "entered_by_id": None, "po_number": None,
                    "po_entered_at": None, "po_sent_at": None,
                    "po_recipient": None, "po_verified": False,
                }
            ],
        )
        r = sales_client.put(
            f"/api/buy-plans/{plan.id}/resubmit",
            json={},
        )
        assert r.status_code == 200
        new_plan = db_session.get(BuyPlan, r.json()["new_plan_id"])
        assert new_plan.is_stock_sale is True


# ── 16. Stock Sale Fast-Track ──────────────────────────────────────────


class TestStockSaleFastTrack:
    """Approving a stock sale skips PO flow and goes straight to complete."""

    def _stock_plan(self, db_session, test_requisition, test_quote, submitted_by_id):
        return _create_buy_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=submitted_by_id,
            is_stock_sale=True,
            line_items=[
                {
                    "offer_id": 1, "mpn": "LM317T", "vendor_name": "Trio",
                    "qty": 1000, "plan_qty": 1000, "cost_price": 0.30,
                    "lead_time": "stock", "condition": "new",
                    "entered_by_id": None, "po_number": None,
                    "po_entered_at": None, "po_sent_at": None,
                    "po_recipient": None, "po_verified": False,
                }
            ],
        )

    def test_approve_stock_sale_goes_complete(
        self, db_session, manager_client, manager_user, test_requisition, test_quote, sales_user,
    ):
        plan = self._stock_plan(db_session, test_requisition, test_quote, sales_user.id)
        r = manager_client.put(
            f"/api/buy-plans/{plan.id}/approve",
            json={"sales_order_number": "SO-STOCK-1"},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "complete"
        db_session.refresh(plan)
        assert plan.completed_at is not None
        assert plan.completed_by_id == manager_user.id

    def test_approve_non_stock_stays_approved(
        self, db_session, manager_client, test_requisition, test_quote, sales_user,
    ):
        plan = _create_buy_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=sales_user.id,
        )
        r = manager_client.put(
            f"/api/buy-plans/{plan.id}/approve",
            json={"sales_order_number": "SO-NORMAL"},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "approved"

    def test_token_approve_stock_sale_goes_complete(
        self, db_session, noauth_client, test_requisition, test_quote, test_user,
    ):
        plan = self._stock_plan(db_session, test_requisition, test_quote, test_user.id)
        r = noauth_client.put(
            f"/api/buy-plans/token/{plan.approval_token}/approve",
            json={"sales_order_number": "SO-STOCK-TOKEN"},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "complete"

    def test_po_entry_on_completed_stock_sale_rejected(
        self, db_session, buyer_client, test_requisition, test_quote, sales_user,
    ):
        """PO entry on a completed stock sale → 400."""
        plan = self._stock_plan(db_session, test_requisition, test_quote, sales_user.id)
        plan.status = "complete"
        plan.completed_at = datetime.now(timezone.utc)
        db_session.commit()

        r = buyer_client.put(
            f"/api/buy-plans/{plan.id}/po",
            json={"line_index": 0, "po_number": "PO-NOPE"},
        )
        assert r.status_code == 400


# ── 17. Auto-Complete Stock Sales ──────────────────────────────────────


class TestAutoCompleteStockSales:
    """Safety net: approved stock sales older than 1 hour get auto-completed."""

    def test_old_approved_stock_sale_completed(self, db_session, test_requisition, test_quote, test_user):
        from datetime import timedelta
        from app.services.buyplan_service import auto_complete_stock_sales

        plan = _create_buy_plan(
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

    def test_recent_approved_stock_sale_skipped(self, db_session, test_requisition, test_quote, test_user):
        from app.services.buyplan_service import auto_complete_stock_sales

        plan = _create_buy_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=test_user.id,
            status="approved",
            is_stock_sale=True,
        )
        plan.approved_at = datetime.now(timezone.utc)
        db_session.commit()

        completed = auto_complete_stock_sales(db_session)
        assert completed == 0
        db_session.refresh(plan)
        assert plan.status == "approved"

    def test_non_stock_sale_skipped(self, db_session, test_requisition, test_quote, test_user):
        from datetime import timedelta
        from app.services.buyplan_service import auto_complete_stock_sales

        plan = _create_buy_plan(
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
        db_session.refresh(plan)
        assert plan.status == "approved"
