"""test_buyplan_token.py — Tests for Buy Plan token-based approval.

Tests cover:
  - GET plan by token (valid, invalid, expired)
  - PUT approve by token (success, invalidated after use, stock sale auto-complete,
    wrong status, expired)
  - PUT reject by token (success, empty reason OK)
  - Token generated on submit via service layer

Called by: pytest
Depends on: conftest fixtures, app.models.buy_plan, app.routers.crm.buy_plans
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.database import get_db
from app.main import app
from app.models import Quote, Requisition, User
from app.models.buy_plan import BuyPlan, BuyPlanStatus

# ── Helpers ──────────────────────────────────────────────────────────


@pytest.fixture()
def noauth_client(db_session: Session) -> TestClient:
    """TestClient with only get_db overridden — no auth override.

    Token endpoints are public, so we don't need auth overrides.
    """

    def _override_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture()
def pending_plan(db_session: Session, test_user: User, test_quote: Quote) -> BuyPlan:
    """A pending buy plan with a valid approval token."""
    req = db_session.query(Requisition).first()
    plan = BuyPlan(
        quote_id=test_quote.id,
        requisition_id=req.id,
        status=BuyPlanStatus.PENDING.value,
        total_cost=1000.00,
        total_revenue=1500.00,
        total_margin_pct=33.33,
        ai_summary="Recommend vendor X for best margin",
        ai_flags=["high_risk", "new_vendor"],
        salesperson_notes="Customer wants fast delivery",
        case_report="Full case analysis here",
        submitted_by_id=test_user.id,
        submitted_at=datetime.now(timezone.utc),
        approval_token="test-token-abc123",
        token_expires_at=datetime.now(timezone.utc) + timedelta(days=30),
    )
    db_session.add(plan)
    db_session.commit()
    db_session.refresh(plan)
    return plan


@pytest.fixture()
def expired_plan(db_session: Session, test_user: User, test_quote: Quote) -> BuyPlan:
    """A pending buy plan with an expired token."""
    req = db_session.query(Requisition).first()
    plan = BuyPlan(
        quote_id=test_quote.id,
        requisition_id=req.id,
        status=BuyPlanStatus.PENDING.value,
        total_cost=500.00,
        submitted_by_id=test_user.id,
        submitted_at=datetime.now(timezone.utc),
        approval_token="expired-token-xyz",
        token_expires_at=datetime.now(timezone.utc) - timedelta(days=1),
    )
    db_session.add(plan)
    db_session.commit()
    db_session.refresh(plan)
    return plan


@pytest.fixture()
def stock_sale_plan(db_session: Session, test_user: User, test_quote: Quote) -> BuyPlan:
    """A pending stock-sale buy plan with a valid approval token."""
    req = db_session.query(Requisition).first()
    plan = BuyPlan(
        quote_id=test_quote.id,
        requisition_id=req.id,
        status=BuyPlanStatus.PENDING.value,
        is_stock_sale=True,
        total_cost=200.00,
        submitted_by_id=test_user.id,
        submitted_at=datetime.now(timezone.utc),
        approval_token="stock-token-999",
        token_expires_at=datetime.now(timezone.utc) + timedelta(days=30),
    )
    db_session.add(plan)
    db_session.commit()
    db_session.refresh(plan)
    return plan


# ── TestGetPlanByToken ───────────────────────────────────────────────


class TestGetPlanByToken:
    """GET /api/buy-plans/token/{token}"""

    def test_valid_token(self, noauth_client: TestClient, pending_plan: BuyPlan):
        resp = noauth_client.get(f"/api/buy-plans/token/{pending_plan.approval_token}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == pending_plan.id
        assert data["status"] == "pending_approval"

    def test_token_response_excludes_sensitive_fields(self, noauth_client: TestClient, pending_plan: BuyPlan):
        """Sensitive commercial data must NOT be exposed via the public token
        endpoint."""
        resp = noauth_client.get(f"/api/buy-plans/token/{pending_plan.approval_token}")
        assert resp.status_code == 200
        data = resp.json()
        # These fields must never appear in the token response
        sensitive_fields = [
            "ai_summary",
            "ai_flags",
            "total_margin_pct",
            "case_report",
            "salesperson_notes",
            "lines",
            "line_items",
        ]
        for field in sensitive_fields:
            assert field not in data, f"Sensitive field '{field}' must not be in token response"

    def test_token_response_includes_approval_fields(self, noauth_client: TestClient, pending_plan: BuyPlan):
        """Token response must include only the fields an approver needs."""
        resp = noauth_client.get(f"/api/buy-plans/token/{pending_plan.approval_token}")
        assert resp.status_code == 200
        data = resp.json()
        expected_fields = [
            "id",
            "status",
            "total_cost",
            "total_revenue",
            "line_count",
            "vendor_names",
            "created_at",
            "requested_by_name",
        ]
        for field in expected_fields:
            assert field in data, f"Expected field '{field}' missing from token response"
        assert data["total_cost"] == 1000.00
        assert data["total_revenue"] == 1500.00

    def test_invalid_token_404(self, noauth_client: TestClient, pending_plan: BuyPlan):
        resp = noauth_client.get("/api/buy-plans/token/nonexistent-token")
        assert resp.status_code == 404

    def test_expired_token_410(self, noauth_client: TestClient, expired_plan: BuyPlan):
        resp = noauth_client.get(f"/api/buy-plans/token/{expired_plan.approval_token}")
        assert resp.status_code == 410


# ── TestApproveByToken ───────────────────────────────────────────────


class TestApproveByToken:
    """PUT /api/buy-plans/token/{token}/approve."""

    def test_approve_success(self, noauth_client: TestClient, pending_plan: BuyPlan, db_session: Session):
        resp = noauth_client.put(
            f"/api/buy-plans/token/{pending_plan.approval_token}/approve",
            json={"sales_order_number": "SO-12345", "notes": "Looks good"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "approved"
        assert data["sales_order_number"] == "SO-12345"
        assert data["approval_notes"] == "Looks good"

    def test_token_invalidated_after_use(self, noauth_client: TestClient, pending_plan: BuyPlan, db_session: Session):
        token = pending_plan.approval_token
        resp = noauth_client.put(
            f"/api/buy-plans/token/{token}/approve",
            json={"sales_order_number": "SO-99999"},
        )
        assert resp.status_code == 200
        # Second attempt with same token should fail
        resp2 = noauth_client.put(
            f"/api/buy-plans/token/{token}/approve",
            json={"sales_order_number": "SO-99999"},
        )
        assert resp2.status_code == 404

    def test_stock_sale_auto_complete(self, noauth_client: TestClient, stock_sale_plan: BuyPlan):
        resp = noauth_client.put(
            f"/api/buy-plans/token/{stock_sale_plan.approval_token}/approve",
            json={"sales_order_number": "SO-STOCK-001"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "complete"
        assert data["completed_at"] is not None

    def test_wrong_status_400(self, noauth_client: TestClient, db_session: Session, pending_plan: BuyPlan):
        # Move plan to active first
        pending_plan.status = BuyPlanStatus.ACTIVE.value
        db_session.commit()
        resp = noauth_client.put(
            f"/api/buy-plans/token/{pending_plan.approval_token}/approve",
            json={"sales_order_number": "SO-000"},
        )
        assert resp.status_code == 400

    def test_expired_token_410(self, noauth_client: TestClient, expired_plan: BuyPlan):
        resp = noauth_client.put(
            f"/api/buy-plans/token/{expired_plan.approval_token}/approve",
            json={"sales_order_number": "SO-000"},
        )
        assert resp.status_code == 410


# ── TestRejectByToken ────────────────────────────────────────────────


class TestRejectByToken:
    """PUT /api/buy-plans/token/{token}/reject."""

    def test_reject_success(self, noauth_client: TestClient, pending_plan: BuyPlan, db_session: Session):
        resp = noauth_client.put(
            f"/api/buy-plans/token/{pending_plan.approval_token}/reject",
            json={"reason": "Need better pricing"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "rejected"
        # Token should be invalidated
        resp2 = noauth_client.get(f"/api/buy-plans/token/{pending_plan.approval_token}")
        assert resp2.status_code == 404

    def test_empty_reason_ok(self, noauth_client: TestClient, pending_plan: BuyPlan):
        resp = noauth_client.put(
            f"/api/buy-plans/token/{pending_plan.approval_token}/reject",
            json={"reason": ""},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "draft"


# ── TestTokenGeneratedOnSubmit ───────────────────────────────────────


class TestTokenGeneratedOnSubmit:
    """Verify token is generated when buy plan is submitted via service layer."""

    def test_token_generated_on_submit(self, db_session: Session, test_user: User, test_quote: Quote):
        """Submit a draft plan via service and verify token fields are set."""
        from app.services.buyplan_workflow import submit_buy_plan

        req = db_session.query(Requisition).first()
        # Create a draft plan with high cost to avoid auto-approve
        plan = BuyPlan(
            quote_id=test_quote.id,
            requisition_id=req.id,
            status=BuyPlanStatus.DRAFT.value,
            total_cost=999999.00,
            total_revenue=1500000.00,
        )
        db_session.add(plan)
        db_session.commit()
        db_session.refresh(plan)

        result = submit_buy_plan(
            plan.id,
            "SO-TOKEN-TEST",
            test_user,
            db_session,
        )
        db_session.commit()

        assert result.status == BuyPlanStatus.PENDING.value
        assert result.approval_token is not None
        assert len(result.approval_token) > 20  # token_urlsafe(32) produces ~43 chars
        assert result.token_expires_at is not None
        # Token should expire ~30 days from now
        now = datetime.now(timezone.utc)
        expires = result.token_expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        delta = expires - now
        assert 29 <= delta.days <= 30
