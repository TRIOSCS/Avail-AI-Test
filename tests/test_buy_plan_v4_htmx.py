"""test_buy_plan_v4_htmx.py — Buy Plan V4 HTMX View Tests.

Tests the HTMX-rendered buy plan list and detail views, plus workflow
form submissions (submit, approve, cancel, reset).

Called by: pytest
Depends on: conftest.py fixtures, app.routers.htmx_views
"""

from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import require_buyer, require_user
from app.main import app
from app.models import Offer, Quote, Requirement, User
from app.models.buy_plan import (
    BuyPlan,
    BuyPlanLine,
    BuyPlanLineStatus,
    BuyPlanStatus,
)


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


def _make_draft_plan(db, test_quote, test_user, *, total_cost=500.0):
    """Create a draft plan with one line."""
    req = db.query(Requirement).filter_by(requisition_id=test_quote.requisition_id).first()
    offer = Offer(
        requisition_id=test_quote.requisition_id,
        requirement_id=req.id,
        vendor_name="Arrow",
        mpn="LM317T",
        qty_available=1000,
        unit_price=0.50,
        status="active",
        created_at=datetime.now(timezone.utc),
    )
    db.add(offer)
    db.flush()

    plan = BuyPlan(
        quote_id=test_quote.id,
        requisition_id=test_quote.requisition_id,
        status=BuyPlanStatus.draft.value,
        total_cost=total_cost,
        total_revenue=750.0,
        total_margin_pct=33.33,
    )
    db.add(plan)
    db.flush()
    line = BuyPlanLine(
        buy_plan_id=plan.id,
        requirement_id=req.id,
        offer_id=offer.id,
        quantity=1000,
        unit_cost=0.50,
        unit_sell=0.75,
        margin_pct=33.33,
        buyer_id=test_user.id,
        status=BuyPlanLineStatus.awaiting_po.value,
    )
    db.add(line)
    db.commit()
    db.refresh(plan)
    return plan, line


class TestHTMXListView:
    def test_list_returns_html(self, db_session: Session, test_quote: Quote, test_user: User):
        """GET /v2/partials/buy-plans returns HTML with plan data."""
        _make_draft_plan(db_session, test_quote, test_user)
        c = _make_client(db_session, test_user)
        r = c.get("/v2/partials/buy-plans")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        assert "Buy Plans" in r.text

    def test_list_filter_status(self, db_session: Session, test_quote: Quote, test_user: User):
        """Status filter works."""
        _make_draft_plan(db_session, test_quote, test_user)
        c = _make_client(db_session, test_user)
        r = c.get("/v2/partials/buy-plans?status=draft")
        assert r.status_code == 200
        assert "draft" in r.text.lower()

    def test_list_empty(self, db_session: Session, test_user: User):
        """Empty list shows 'no buy plans' message."""
        c = _make_client(db_session, test_user)
        r = c.get("/v2/partials/buy-plans")
        assert r.status_code == 200
        assert "No buy plans found" in r.text


class TestHTMXDetailView:
    def test_detail_returns_html(self, db_session: Session, test_quote: Quote, test_user: User):
        """GET /v2/partials/buy-plans/{id} returns HTML detail."""
        plan, _ = _make_draft_plan(db_session, test_quote, test_user)
        c = _make_client(db_session, test_user)
        r = c.get(f"/v2/partials/buy-plans/{plan.id}")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        assert f"Buy Plan #{plan.id}" in r.text

    def test_detail_not_found(self, db_session: Session, test_user: User):
        """Nonexistent plan returns 404."""
        c = _make_client(db_session, test_user)
        r = c.get("/v2/partials/buy-plans/99999")
        assert r.status_code == 404

    def test_detail_shows_lines(self, db_session: Session, test_quote: Quote, test_user: User):
        """Detail view shows line items."""
        plan, _ = _make_draft_plan(db_session, test_quote, test_user)
        c = _make_client(db_session, test_user)
        r = c.get(f"/v2/partials/buy-plans/{plan.id}")
        assert "LM317T" in r.text or "Arrow" in r.text
        assert "Line Items" in r.text


class TestHTMXSubmit:
    def test_submit_returns_updated_detail(self, db_session: Session, test_quote: Quote, test_user: User):
        """POST submit returns refreshed detail with new status."""
        plan, _ = _make_draft_plan(db_session, test_quote, test_user)
        c = _make_client(db_session, test_user)
        r = c.post(
            f"/v2/partials/buy-plans/{plan.id}/submit",
            data={"sales_order_number": "SO-001"},
        )
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]

    def test_submit_blank_so_rejected(self, db_session: Session, test_quote: Quote, test_user: User):
        """Blank SO# returns 400."""
        plan, _ = _make_draft_plan(db_session, test_quote, test_user)
        c = _make_client(db_session, test_user)
        r = c.post(
            f"/v2/partials/buy-plans/{plan.id}/submit",
            data={"sales_order_number": ""},
        )
        assert r.status_code == 400


class TestHTMXCancel:
    def test_cancel_returns_updated_detail(self, db_session: Session, test_quote: Quote, test_user: User):
        """POST cancel returns refreshed detail with cancelled status."""
        plan, _ = _make_draft_plan(db_session, test_quote, test_user)
        c = _make_client(db_session, test_user)
        r = c.post(
            f"/v2/partials/buy-plans/{plan.id}/cancel",
            data={"reason": "No longer needed"},
        )
        assert r.status_code == 200
        assert "CANCELLED" in r.text


class TestHTMXReset:
    def test_reset_halted_plan(self, db_session: Session, test_quote: Quote, test_user: User):
        """POST reset returns plan back in draft."""
        plan, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.halted.value
        db_session.commit()

        c = _make_client(db_session, test_user)
        r = c.post(f"/v2/partials/buy-plans/{plan.id}/reset")
        assert r.status_code == 200
        assert "DRAFT" in r.text


class TestFullPageLoad:
    def test_buy_plans_full_page(self, db_session: Session, test_user: User):
        """GET /v2/buy-plans returns full page HTML."""
        c = _make_client(db_session, test_user)
        r = c.get("/v2/buy-plans")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]

    def test_buy_plan_detail_full_page(self, db_session: Session, test_quote: Quote, test_user: User):
        """GET /v2/buy-plans/{id} returns full page HTML."""
        plan, _ = _make_draft_plan(db_session, test_quote, test_user)
        c = _make_client(db_session, test_user)
        r = c.get(f"/v2/buy-plans/{plan.id}")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
