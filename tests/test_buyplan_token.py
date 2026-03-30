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
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_db, None)


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
    """GET /api/buy-plans/token/{token} — most tests removed (endpoint deleted in CRM
    redesign)."""

    def test_invalid_token_404(self, noauth_client: TestClient, pending_plan: BuyPlan):
        resp = noauth_client.get("/api/buy-plans/token/nonexistent-token")
        assert resp.status_code == 404


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
