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
from app.models import BuyPlan, Offer, User

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

    def test_trader_sees_own(
        self, db_session, trader_client, trader_user, test_requisition, test_quote, sales_user, test_user
    ):
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
            "id",
            "requisition_id",
            "requisition_name",
            "quote_id",
            "quote_number",
            "quote_subtotal",
            "customer_name",
            "status",
            "line_items",
            "is_stock_sale",
            "total_cost",
            "total_revenue",
            "total_profit",
            "overall_margin_pct",
            "sales_order_number",
            "salesperson_notes",
            "manager_notes",
            "rejection_reason",
            "submitted_by",
            "submitted_by_id",
            "approved_by",
            "approved_by_id",
            "rejected_by",
            "rejected_by_id",
            "submitted_at",
            "approved_at",
            "rejected_at",
            "completed_at",
            "completed_by",
            "cancelled_at",
            "cancelled_by",
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

    def test_dict_shows_rejected_by(
        self, db_session, manager_client, manager_user, test_requisition, test_quote, sales_user
    ):
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
                    "offer_id": 1,
                    "mpn": "LM317T",
                    "vendor_name": "Arrow",
                    "qty": 1000,
                    "cost_price": 0.50,
                    "po_number": "PO-OLD",
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
                    "offer_id": 1,
                    "mpn": "LM317T",
                    "vendor_name": "Arrow",
                    "qty": 1000,
                    "cost_price": 0.50,
                    "po_number": "PO-X",
                    "po_entered_at": "2026-01-01T00:00:00",
                    "po_sent_at": None,
                    "po_recipient": None,
                    "po_verified": False,
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

    def test_buyer_allowed_from_po_confirmed(self, db_session, buyer_client, test_requisition, test_quote, sales_user):
        """Buyers can complete from po_confirmed status."""
        plan = _create_buy_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=sales_user.id,
            status="po_confirmed",
        )
        r = buyer_client.put(f"/api/buy-plans/{plan.id}/complete")
        assert r.status_code == 200

    def test_buyer_forbidden_from_approved(self, db_session, buyer_client, test_requisition, test_quote, sales_user):
        """Buyers cannot complete from approved status (only po_entered/po_confirmed)."""
        plan = _create_buy_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=sales_user.id,
            status="approved",
        )
        r = buyer_client.put(f"/api/buy-plans/{plan.id}/complete")
        assert r.status_code == 403

    def test_wrong_status(self, db_session, admin_client, test_requisition, test_quote, sales_user):
        plan = _create_buy_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=sales_user.id,
            status="pending_approval",
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
                    "offer_id": 1,
                    "mpn": "LM317T",
                    "vendor_name": "Arrow",
                    "qty": 1000,
                    "cost_price": 0.50,
                    "po_number": "PO-123",
                    "po_entered_at": "2026-01-01",
                    "po_sent_at": None,
                    "po_recipient": None,
                    "po_verified": False,
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

    def test_buyer_not_submitter_forbidden(
        self, db_session, buyer_client, test_user, test_requisition, test_quote, sales_user
    ):
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
                    "offer_id": 1,
                    "mpn": "LM317T",
                    "vendor_name": "Arrow",
                    "qty": 1000,
                    "cost_price": 0.50,
                    "po_number": "PO-OLD",
                    "po_entered_at": "2026-01-01",
                    "po_sent_at": "2026-01-02",
                    "po_recipient": "vendor@test.com",
                    "po_verified": True,
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
        self,
        db_session,
        sales_client,
        sales_user,
        manager_client,
        buyer_client,
        admin_client,
        test_quote,
        test_offer,
        test_requisition,
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
        self,
        db_session,
        sales_client,
        sales_user,
        manager_client,
        test_quote,
        test_offer,
        test_requisition,
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
        self,
        db_session,
        sales_client,
        sales_user,
        manager_client,
        test_quote,
        test_offer,
        test_requisition,
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
                    "offer_id": 1,
                    "mpn": "LM317T",
                    "vendor_name": "Trio",
                    "qty": 1000,
                    "plan_qty": 1000,
                    "cost_price": 0.30,
                    "lead_time": "stock",
                    "condition": "new",
                    "entered_by_id": None,
                    "po_number": None,
                    "po_entered_at": None,
                    "po_sent_at": None,
                    "po_recipient": None,
                    "po_verified": False,
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
                    "offer_id": 1,
                    "mpn": "LM317T",
                    "vendor_name": "Trio",
                    "qty": 1000,
                    "plan_qty": 1000,
                    "cost_price": 0.30,
                    "lead_time": "stock",
                    "condition": "new",
                    "entered_by_id": None,
                    "po_number": None,
                    "po_entered_at": None,
                    "po_sent_at": None,
                    "po_recipient": None,
                    "po_verified": False,
                }
            ],
        )

    def test_approve_stock_sale_goes_complete(
        self,
        db_session,
        manager_client,
        manager_user,
        test_requisition,
        test_quote,
        sales_user,
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
        self,
        db_session,
        manager_client,
        test_requisition,
        test_quote,
        sales_user,
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
        self,
        db_session,
        noauth_client,
        test_requisition,
        test_quote,
        test_user,
    ):
        plan = self._stock_plan(db_session, test_requisition, test_quote, test_user.id)
        r = noauth_client.put(
            f"/api/buy-plans/token/{plan.approval_token}/approve",
            json={"sales_order_number": "SO-STOCK-TOKEN"},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "complete"

    def test_po_entry_on_completed_stock_sale_rejected(
        self,
        db_session,
        buyer_client,
        test_requisition,
        test_quote,
        sales_user,
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


# ── V1 Deprecation Flag ──────────────────────────────────────────────


class TestV1DeprecationFlag:
    def test_submit_returns_410_when_v1_disabled(self, sales_client, test_quote, test_offer):
        """POST /api/quotes/{id}/buy-plan returns 410 when V1 is disabled."""
        with patch("app.routers.crm.buy_plans.settings") as mock_settings:
            mock_settings.buy_plan_v1_enabled = False
            mock_settings.stock_sale_vendor_names = []
            resp = sales_client.post(
                f"/api/quotes/{test_quote.id}/buy-plan",
                json={"offer_ids": [test_offer.id]},
            )
        assert resp.status_code == 410

    def test_draft_returns_410_when_v1_disabled(self, sales_client, test_quote, test_offer):
        """POST /api/quotes/{id}/buy-plan/draft returns 410 when V1 is disabled."""
        with patch("app.routers.crm.buy_plans.settings") as mock_settings:
            mock_settings.buy_plan_v1_enabled = False
            mock_settings.stock_sale_vendor_names = []
            resp = sales_client.post(
                f"/api/quotes/{test_quote.id}/buy-plan/draft",
                json={"offer_ids": [test_offer.id]},
            )
        assert resp.status_code == 410

    def test_submit_works_when_v1_enabled(self, sales_client, test_quote, test_offer):
        """POST /api/quotes/{id}/buy-plan works normally when V1 is enabled."""
        resp = sales_client.post(
            f"/api/quotes/{test_quote.id}/buy-plan",
            json={"offer_ids": [test_offer.id]},
        )
        assert resp.status_code == 200


# ── 18. V1 Draft Endpoint (POST /api/quotes/{qid}/buy-plan/draft) ───


class TestCreateBuyPlanDraft:
    """Cover lines 176-203: create_buy_plan_draft success when V1 enabled."""

    def test_draft_success(self, db_session, sales_client, sales_user, test_quote, test_offer):
        """Create a draft buy plan when V1 is enabled."""
        with patch("app.routers.crm.buy_plans.settings") as mock_settings:
            mock_settings.buy_plan_v1_enabled = True
            mock_settings.stock_sale_vendor_names = []
            r = sales_client.post(
                f"/api/quotes/{test_quote.id}/buy-plan/draft",
                json={"offer_ids": [test_offer.id]},
            )
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["status"] == "draft"
        assert data["buy_plan_id"] is not None
        plan = db_session.get(BuyPlan, data["buy_plan_id"])
        assert plan.status == "draft"
        assert plan.submitted_by_id == sales_user.id

    def test_draft_nonexistent_quote(self, sales_client):
        """Draft against nonexistent quote → 404."""
        with patch("app.routers.crm.buy_plans.settings") as mock_settings:
            mock_settings.buy_plan_v1_enabled = True
            mock_settings.stock_sale_vendor_names = []
            r = sales_client.post(
                "/api/quotes/99999/buy-plan/draft",
                json={"offer_ids": [1]},
            )
        assert r.status_code == 404

    def test_draft_no_offers(self, sales_client, test_quote):
        """Draft with empty offer_ids → 400."""
        with patch("app.routers.crm.buy_plans.settings") as mock_settings:
            mock_settings.buy_plan_v1_enabled = True
            mock_settings.stock_sale_vendor_names = []
            r = sales_client.post(
                f"/api/quotes/{test_quote.id}/buy-plan/draft",
                json={"offer_ids": []},
            )
        assert r.status_code == 400

    def test_draft_invalid_offer_ids(self, db_session, sales_client, test_quote):
        """Draft with non-existent offer IDs → 400 (no valid offers)."""
        with patch("app.routers.crm.buy_plans.settings") as mock_settings:
            mock_settings.buy_plan_v1_enabled = True
            mock_settings.stock_sale_vendor_names = []
            r = sales_client.post(
                f"/api/quotes/{test_quote.id}/buy-plan/draft",
                json={"offer_ids": [99999]},
            )
        assert r.status_code == 400

    def test_draft_with_notes(self, db_session, sales_client, sales_user, test_quote, test_offer):
        """Draft with salesperson notes."""
        with patch("app.routers.crm.buy_plans.settings") as mock_settings:
            mock_settings.buy_plan_v1_enabled = True
            mock_settings.stock_sale_vendor_names = []
            r = sales_client.post(
                f"/api/quotes/{test_quote.id}/buy-plan/draft",
                json={"offer_ids": [test_offer.id], "salesperson_notes": "Urgent"},
            )
        assert r.status_code == 200
        plan = db_session.get(BuyPlan, r.json()["buy_plan_id"])
        assert plan.salesperson_notes == "Urgent"


# ── 19. Submit Draft (PUT /api/buy-plans/{id}/submit) ────────────────


class TestSubmitDraftBuyPlan:
    """Cover lines 213-248: submit_draft_buy_plan success path."""

    def test_submit_draft_by_creator(
        self,
        db_session,
        sales_client,
        sales_user,
        test_quote,
        test_requisition,
        test_offer,
    ):
        """Creator can submit their own draft plan → pending_approval."""
        plan = _create_buy_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=sales_user.id,
            status="draft",
            line_items=[
                {
                    "offer_id": test_offer.id,
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
        )
        r = sales_client.put(f"/api/buy-plans/{plan.id}/submit")
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["status"] == "pending_approval"
        db_session.refresh(plan)
        assert plan.status == "pending_approval"
        assert plan.approval_token is not None
        assert plan.submitted_at is not None
        # Quote and req statuses updated
        db_session.refresh(test_quote)
        assert test_quote.result == "won"
        assert test_quote.status == "won"
        # Offer status also updated
        db_session.refresh(test_offer)
        assert test_offer.status == "won"

    def test_submit_draft_by_admin(
        self,
        db_session,
        admin_client,
        admin_user,
        sales_user,
        test_quote,
        test_requisition,
    ):
        """Admin can submit any draft plan."""
        plan = _create_buy_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=sales_user.id,
            status="draft",
        )
        r = admin_client.put(f"/api/buy-plans/{plan.id}/submit")
        assert r.status_code == 200
        assert r.json()["status"] == "pending_approval"

    def test_submit_draft_not_found(self, sales_client):
        """Submit nonexistent draft → 404."""
        r = sales_client.put("/api/buy-plans/99999/submit")
        assert r.status_code == 404

    def test_submit_draft_wrong_status(
        self,
        db_session,
        sales_client,
        sales_user,
        test_requisition,
        test_quote,
    ):
        """Submit a non-draft plan → 400."""
        plan = _create_buy_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=sales_user.id,
            status="pending_approval",
        )
        r = sales_client.put(f"/api/buy-plans/{plan.id}/submit")
        assert r.status_code == 400

    def test_submit_draft_forbidden(
        self,
        db_session,
        trader_client,
        sales_user,
        test_requisition,
        test_quote,
    ):
        """Non-creator, non-admin/manager cannot submit."""
        plan = _create_buy_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=sales_user.id,
            status="draft",
        )
        r = trader_client.put(f"/api/buy-plans/{plan.id}/submit")
        assert r.status_code == 403


# ── 20. Submit V1 Disabled — no valid offers path (line 271) ─────────


class TestSubmitNoValidOffers:
    def test_submit_no_valid_offers_v1_enabled(
        self,
        db_session,
        sales_client,
        test_quote,
    ):
        """Submit with non-existent offer IDs → 400 (no valid offers)."""
        r = sales_client.post(
            f"/api/quotes/{test_quote.id}/buy-plan",
            json={"offer_ids": [99999]},
        )
        assert r.status_code == 400
        assert "No valid offers" in r.json()["error"]


# ── 21. List with non-approved status filter (line 344) ──────────────


class TestListNonApprovedFilter:
    def test_filter_pending(
        self,
        db_session,
        admin_client,
        test_requisition,
        test_quote,
        test_user,
    ):
        """Filter by pending_approval status returns only pending plans."""
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
        r = admin_client.get("/api/buy-plans?status=pending_approval")
        assert r.status_code == 200
        plans = r.json()
        assert len(plans) == 1
        assert plans[0]["status"] == "pending_approval"

    def test_filter_draft(
        self,
        db_session,
        admin_client,
        test_requisition,
        test_quote,
        test_user,
    ):
        """Filter by draft status returns only draft plans."""
        _create_buy_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=test_user.id,
            status="draft",
        )
        _create_buy_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=test_user.id,
            status="approved",
        )
        r = admin_client.get("/api/buy-plans?status=draft")
        assert r.status_code == 200
        plans = r.json()
        assert len(plans) == 1
        assert plans[0]["status"] == "draft"


# ── 22. Complete — non-admin, non-buyer user → 403 (line 658) ────────


class TestCompleteNonBuyerNonAdmin:
    def test_sales_cannot_complete(
        self,
        db_session,
        sales_client,
        sales_user,
        test_requisition,
        test_quote,
    ):
        """Sales user (not admin, not buyer) → 403."""
        plan = _create_buy_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=sales_user.id,
            status="po_entered",
        )
        r = sales_client.put(f"/api/buy-plans/{plan.id}/complete")
        assert r.status_code == 403
        assert "Only admin, manager, or buyer" in r.json()["error"]


# ── 23. _record_purchase_history coverage (lines 966-1003) ───────────


class TestRecordPurchaseHistory:
    """Direct tests for the _record_purchase_history helper function."""

    def test_no_req(self, db_session):
        """No requisition → returns early."""
        from app.models import Quote
        from app.routers.crm.buy_plans import _record_purchase_history

        quote = Quote(id=1, line_items=[])
        _record_purchase_history(db_session, None, quote, [])
        # No exception — just returns

    def test_req_no_site_id(self, db_session, test_requisition):
        """Requisition with no customer_site_id → returns early."""
        from app.models import Quote
        from app.routers.crm.buy_plans import _record_purchase_history

        test_requisition.customer_site_id = None
        db_session.commit()
        quote = Quote(id=1, line_items=[])
        _record_purchase_history(db_session, test_requisition, quote, [])

    def test_site_no_company(self, db_session, test_requisition, test_customer_site):
        """Site with no company_id → returns at line 972."""
        from unittest.mock import MagicMock

        from app.models import Quote
        from app.routers.crm.buy_plans import _record_purchase_history

        test_requisition.customer_site_id = test_customer_site.id
        db_session.commit()
        # Mock db.get to return a site with company_id=None (can't set NULL on real column)
        fake_site = MagicMock()
        fake_site.company_id = None
        original_get = db_session.get

        def patched_get(model, pk, **kw):
            from app.models import CustomerSite as CS

            if model is CS and pk == test_customer_site.id:
                return fake_site
            return original_get(model, pk, **kw)

        with patch.object(db_session, "get", side_effect=patched_get):
            quote = Quote(id=1, line_items=[])
            _record_purchase_history(db_session, test_requisition, quote, [])

    def test_offer_no_material_card(
        self,
        db_session,
        test_requisition,
        test_customer_site,
        test_offer,
    ):
        """Offer with no material_card_id → skip (line 978)."""
        from app.models import Quote
        from app.routers.crm.buy_plans import _record_purchase_history

        test_requisition.customer_site_id = test_customer_site.id
        db_session.commit()
        test_offer.material_card_id = None
        db_session.commit()
        quote = Quote(id=1, line_items=[])
        _record_purchase_history(db_session, test_requisition, quote, [test_offer])

    def test_offer_with_material_card(
        self,
        db_session,
        test_requisition,
        test_customer_site,
        test_offer,
        test_material_card,
    ):
        """Offer with material_card_id → calls upsert_purchase."""
        from app.models import Quote
        from app.routers.crm.buy_plans import _record_purchase_history

        test_requisition.customer_site_id = test_customer_site.id
        db_session.commit()
        test_offer.material_card_id = test_material_card.id
        db_session.commit()
        quote = Quote(id=1, line_items=[])
        _record_purchase_history(db_session, test_requisition, quote, [test_offer])

    def test_quote_line_items_with_card(
        self,
        db_session,
        test_requisition,
        test_customer_site,
        test_material_card,
    ):
        """Quote line items with material_card_id → calls upsert_purchase."""
        from app.models import Quote
        from app.routers.crm.buy_plans import _record_purchase_history

        test_requisition.customer_site_id = test_customer_site.id
        db_session.commit()
        quote = Quote(
            id=1,
            line_items=[
                {"material_card_id": test_material_card.id, "sell_price": 0.75, "qty": 500},
            ],
        )
        _record_purchase_history(db_session, test_requisition, quote, [])

    def test_quote_line_items_without_card(
        self,
        db_session,
        test_requisition,
        test_customer_site,
    ):
        """Quote line items without material_card_id → skipped (line 991)."""
        from app.models import Quote
        from app.routers.crm.buy_plans import _record_purchase_history

        test_requisition.customer_site_id = test_customer_site.id
        db_session.commit()
        quote = Quote(
            id=1,
            line_items=[
                {"mpn": "LM317T", "sell_price": 0.75, "qty": 500},
            ],
        )
        _record_purchase_history(db_session, test_requisition, quote, [])

    def test_exception_logged_not_raised(
        self,
        db_session,
        test_requisition,
        test_customer_site,
    ):
        """Exception in purchase history → logged but not raised (lines 1002-1003)."""
        from app.models import Quote
        from app.routers.crm.buy_plans import _record_purchase_history

        test_requisition.customer_site_id = test_customer_site.id
        db_session.commit()
        quote = Quote(
            id=1,
            line_items=[
                {"material_card_id": 99999, "sell_price": 0.75, "qty": 500},
            ],
        )
        # Force an error by patching upsert_purchase at the source
        with patch(
            "app.services.purchase_history_service.upsert_purchase",
            side_effect=Exception("Forced test error"),
        ):
            # Should not raise — exception is caught and logged
            _record_purchase_history(db_session, test_requisition, quote, [])


# ── 24. Token Expired Edge Cases ────────────────────────────────────


class TestTokenExpired:
    """Cover expired token paths: lines 363, 379, 436."""

    @staticmethod
    def _expired_plan(db_session, test_requisition, test_quote, submitted_by_id):
        """Create a plan with an expired token_expires_at (naive, in the past)."""
        plan = _create_buy_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=submitted_by_id,
            token_expires_at=datetime(2020, 1, 1),
        )
        return plan

    def _mock_now(self):
        """Mock datetime.now(tz) to return naive UTC so it matches SQLite storage."""
        return patch(
            "app.routers.crm.buy_plans.datetime",
            wraps=datetime,
            **{"now.return_value": datetime(2025, 6, 1)},
        )

    def test_get_by_expired_token(
        self,
        db_session,
        noauth_client,
        test_requisition,
        test_quote,
        test_user,
    ):
        """GET token endpoint → 410 when token expired."""
        plan = self._expired_plan(db_session, test_requisition, test_quote, test_user.id)
        with self._mock_now():
            r = noauth_client.get(f"/api/buy-plans/token/{plan.approval_token}")
        assert r.status_code == 410

    def test_approve_expired_token(
        self,
        db_session,
        noauth_client,
        test_requisition,
        test_quote,
        test_user,
    ):
        """PUT approve via token → 410 when token expired."""
        plan = self._expired_plan(db_session, test_requisition, test_quote, test_user.id)
        with self._mock_now():
            r = noauth_client.put(
                f"/api/buy-plans/token/{plan.approval_token}/approve",
                json={"sales_order_number": "SO-EXPIRED"},
            )
        assert r.status_code == 410

    def test_reject_expired_token(
        self,
        db_session,
        noauth_client,
        test_requisition,
        test_quote,
        test_user,
    ):
        """PUT reject via token → 410 when token expired."""
        plan = self._expired_plan(db_session, test_requisition, test_quote, test_user.id)
        with self._mock_now():
            r = noauth_client.put(
                f"/api/buy-plans/token/{plan.approval_token}/reject",
                json={"reason": "test"},
            )
        assert r.status_code == 410


# ── 25. Token Approve Edge Cases ────────────────────────────────────


class TestTokenApproveEdgeCases:
    """Cover lines 377, 385, 389: invalid token approve, blank SO, notes."""

    def test_approve_invalid_token(self, noauth_client):
        """Approve with invalid token → 404."""
        r = noauth_client.put(
            "/api/buy-plans/token/nonexistent-token/approve",
            json={"sales_order_number": "SO-X"},
        )
        assert r.status_code == 404

    def test_approve_blank_so(
        self,
        db_session,
        noauth_client,
        test_requisition,
        test_quote,
        test_user,
    ):
        """Approve via token with blank SO → 400."""
        plan = _create_buy_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=test_user.id,
        )
        r = noauth_client.put(
            f"/api/buy-plans/token/{plan.approval_token}/approve",
            json={"sales_order_number": "   "},
        )
        assert r.status_code == 400

    def test_approve_with_notes(
        self,
        db_session,
        noauth_client,
        test_requisition,
        test_quote,
        test_user,
    ):
        """Approve via token with manager_notes → notes stored."""
        plan = _create_buy_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=test_user.id,
        )
        r = noauth_client.put(
            f"/api/buy-plans/token/{plan.approval_token}/approve",
            json={"sales_order_number": "SO-NOTES", "manager_notes": "Looks good"},
        )
        assert r.status_code == 200
        db_session.refresh(plan)
        assert plan.manager_notes == "Looks good"


# ── 26. Token Reject Edge Cases ────────────────────────────────────


class TestTokenRejectEdgeCases:
    """Cover lines 434, 436, 438: invalid token reject, expired, wrong status."""

    def test_reject_invalid_token(self, noauth_client):
        """Reject with invalid token → 404."""
        r = noauth_client.put(
            "/api/buy-plans/token/nonexistent-token/reject",
            json={"reason": "test"},
        )
        assert r.status_code == 404

    def test_reject_wrong_status(
        self,
        db_session,
        noauth_client,
        test_requisition,
        test_quote,
        test_user,
    ):
        """Reject via token on non-pending plan → 400."""
        plan = _create_buy_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=test_user.id,
            status="approved",
        )
        r = noauth_client.put(
            f"/api/buy-plans/token/{plan.approval_token}/reject",
            json={"reason": "test"},
        )
        assert r.status_code == 400


# ── 27. Approve Not Found & Blank SO ────────────────────────────────


class TestApproveNotFoundAndBlankSO:
    """Cover lines 486, 492."""

    def test_approve_not_found(self, manager_client):
        """Approve nonexistent plan → 404."""
        r = manager_client.put(
            "/api/buy-plans/99999/approve",
            json={"sales_order_number": "SO-X"},
        )
        assert r.status_code == 404

    def test_approve_blank_so(
        self,
        db_session,
        manager_client,
        test_requisition,
        test_quote,
        sales_user,
    ):
        """Approve with blank SO → 400."""
        plan = _create_buy_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=sales_user.id,
        )
        r = manager_client.put(
            f"/api/buy-plans/{plan.id}/approve",
            json={"sales_order_number": "   "},
        )
        assert r.status_code == 400

    def test_approve_with_line_items_override(
        self,
        db_session,
        manager_client,
        test_requisition,
        test_quote,
        sales_user,
    ):
        """Approve with line_items override → line_items stored (line 496)."""
        plan = _create_buy_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=sales_user.id,
        )
        new_items = [{"offer_id": 1, "mpn": "UPDATED", "qty": 200}]
        r = manager_client.put(
            f"/api/buy-plans/{plan.id}/approve",
            json={"sales_order_number": "SO-LI", "line_items": new_items},
        )
        assert r.status_code == 200
        db_session.refresh(plan)
        assert plan.line_items[0]["mpn"] == "UPDATED"


# ── 28. Reject Not Found ─────────────────────────────────────────────


class TestRejectNotFound:
    """Cover line 546."""

    def test_reject_not_found(self, manager_client):
        """Reject nonexistent plan → 404."""
        r = manager_client.put(
            "/api/buy-plans/99999/reject",
            json={"reason": "test"},
        )
        assert r.status_code == 404


# ── 29. PO Entry Not Found & Empty PO ────────────────────────────────


class TestPOEntryNotFoundEmptyPO:
    """Cover lines 580, 587."""

    def test_po_not_found(self, buyer_client):
        """PO entry on nonexistent plan → 404."""
        r = buyer_client.put(
            "/api/buy-plans/99999/po",
            json={"line_index": 0, "po_number": "PO-X"},
        )
        assert r.status_code == 404

    def test_po_blank_number(
        self,
        db_session,
        buyer_client,
        test_requisition,
        test_quote,
        sales_user,
    ):
        """PO entry with blank po_number → 400."""
        plan = _create_buy_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=sales_user.id,
            status="approved",
        )
        r = buyer_client.put(
            f"/api/buy-plans/{plan.id}/po",
            json={"line_index": 0, "po_number": "   "},
        )
        assert r.status_code == 400


# ── 30. Complete Not Found ────────────────────────────────────────────


class TestCompleteNotFound:
    """Cover line 644."""

    def test_complete_not_found(self, admin_client):
        """Complete nonexistent plan → 404."""
        r = admin_client.put("/api/buy-plans/99999/complete")
        assert r.status_code == 404


# ── 31. Cancel Not Found ─────────────────────────────────────────────


class TestCancelNotFound:
    """Cover line 691."""

    def test_cancel_not_found(self, admin_client):
        """Cancel nonexistent plan → 404."""
        r = admin_client.put(
            "/api/buy-plans/99999/cancel",
            json={"reason": "test"},
        )
        assert r.status_code == 404


# ── 32. Resubmit Not Found ───────────────────────────────────────────


class TestResubmitNotFound:
    """Cover line 767."""

    def test_resubmit_not_found(self, sales_client):
        """Resubmit nonexistent plan → 404."""
        r = sales_client.put(
            "/api/buy-plans/99999/resubmit",
            json={"salesperson_notes": "test"},
        )
        assert r.status_code == 404


# ── 33. Bulk PO Not Found & Invalid Index ────────────────────────────


class TestBulkPONotFoundAndInvalidIndex:
    """Cover lines 859, 881."""

    def test_bulk_po_not_found(self, buyer_client):
        """Bulk PO on nonexistent plan → 404."""
        r = buyer_client.put(
            "/api/buy-plans/99999/po-bulk",
            json={"entries": [{"line_index": 0, "po_number": "PO-X"}]},
        )
        assert r.status_code == 404

    def test_bulk_po_invalid_index_skipped(
        self,
        db_session,
        buyer_client,
        test_requisition,
        test_quote,
        sales_user,
    ):
        """Bulk PO with invalid line_index is skipped (line 881)."""
        plan = _create_buy_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=sales_user.id,
            status="approved",
        )
        r = buyer_client.put(
            f"/api/buy-plans/{plan.id}/po-bulk",
            json={"entries": [{"line_index": 99, "po_number": "PO-INVALID"}]},
        )
        assert r.status_code == 200
        # Status stays approved since no valid entries were processed
        assert r.json()["status"] == "approved"
