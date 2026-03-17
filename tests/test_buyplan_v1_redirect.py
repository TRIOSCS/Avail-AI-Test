"""test_buyplan_v1_redirect.py — V1 Buy Plan Compatibility Layer Tests.

Tests specific to the V1→V3 status mapping logic and deprecated mutation
redirects. Validates that V3 data is returned in V1-shaped format.

Called by: pytest
Depends on: conftest.py fixtures, app.routers.crm.buy_plans
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
)
from app.routers.crm.buy_plans import _map_v3_status_to_v1, _v3_to_v1_dict

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
    c = TestClient(app)
    return c


def _make_offer(db, req_id, requirement_id, **overrides) -> Offer:
    defaults = {
        "requisition_id": req_id,
        "requirement_id": requirement_id,
        "vendor_name": "Arrow",
        "mpn": "LM317T",
        "qty_available": 1000,
        "unit_price": 0.50,
        "status": "active",
        "created_at": datetime.now(timezone.utc),
    }
    defaults.update(overrides)
    offer = Offer(**defaults)
    db.add(offer)
    db.flush()
    return offer


def _make_plan_with_line(
    db, test_quote, test_user, *, status="draft", line_status="awaiting_po", po_number=None, cancellation_reason=None
):
    """Create a plan with one line for testing status mapping."""
    req = db.query(Requirement).filter_by(requisition_id=test_quote.requisition_id).first()
    offer = _make_offer(db, test_quote.requisition_id, req.id, entered_by_id=test_user.id)
    plan = BuyPlan(
        quote_id=test_quote.id,
        requisition_id=test_quote.requisition_id,
        status=status,
        total_cost=500.0,
        total_revenue=750.0,
        cancellation_reason=cancellation_reason,
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
        buyer_id=test_user.id,
        status=line_status,
        po_number=po_number,
    )
    db.add(line)
    db.commit()
    db.refresh(plan)
    return plan, line


# ── Status Mapping Unit Tests ────────────────────────────────────────


class TestV1StatusMapping:
    """Test _map_v3_status_to_v1 for all V3→V1 status transitions."""

    def test_draft_maps_to_draft(self, db_session: Session, test_quote: Quote, test_user: User):
        plan, _ = _make_plan_with_line(db_session, test_quote, test_user, status="draft")
        assert _map_v3_status_to_v1(plan) == "draft"

    def test_pending_maps_to_pending_approval(self, db_session: Session, test_quote: Quote, test_user: User):
        plan, _ = _make_plan_with_line(db_session, test_quote, test_user, status="pending")
        assert _map_v3_status_to_v1(plan) == "pending_approval"

    def test_active_no_po_maps_to_approved(self, db_session: Session, test_quote: Quote, test_user: User):
        plan, _ = _make_plan_with_line(db_session, test_quote, test_user, status="active")
        assert _map_v3_status_to_v1(plan) == "approved"

    def test_active_with_po_awaiting_maps_to_po_entered(self, db_session: Session, test_quote: Quote, test_user: User):
        plan, _ = _make_plan_with_line(
            db_session,
            test_quote,
            test_user,
            status="active",
            line_status="awaiting_po",
            po_number="PO-001",
        )
        assert _map_v3_status_to_v1(plan) == "po_entered"

    def test_active_with_po_pending_verify_maps_to_po_confirmed(
        self, db_session: Session, test_quote: Quote, test_user: User
    ):
        plan, _ = _make_plan_with_line(
            db_session,
            test_quote,
            test_user,
            status="active",
            line_status="pending_verify",
            po_number="PO-001",
        )
        assert _map_v3_status_to_v1(plan) == "po_confirmed"

    def test_active_with_po_verified_maps_to_po_confirmed(
        self, db_session: Session, test_quote: Quote, test_user: User
    ):
        plan, _ = _make_plan_with_line(
            db_session,
            test_quote,
            test_user,
            status="active",
            line_status="verified",
            po_number="PO-001",
        )
        assert _map_v3_status_to_v1(plan) == "po_confirmed"

    def test_completed_maps_to_complete(self, db_session: Session, test_quote: Quote, test_user: User):
        plan, _ = _make_plan_with_line(db_session, test_quote, test_user, status="completed")
        assert _map_v3_status_to_v1(plan) == "complete"

    def test_cancelled_maps_to_cancelled(self, db_session: Session, test_quote: Quote, test_user: User):
        plan, _ = _make_plan_with_line(db_session, test_quote, test_user, status="cancelled")
        assert _map_v3_status_to_v1(plan) == "cancelled"

    def test_draft_with_rejection_note_maps_to_rejected(self, db_session: Session, test_quote: Quote, test_user: User):
        plan, _ = _make_plan_with_line(
            db_session,
            test_quote,
            test_user,
            status="draft",
            cancellation_reason="Fix pricing",
        )
        assert _map_v3_status_to_v1(plan) == "rejected"

    def test_halted_maps_to_halted(self, db_session: Session, test_quote: Quote, test_user: User):
        plan, _ = _make_plan_with_line(db_session, test_quote, test_user, status="halted")
        assert _map_v3_status_to_v1(plan) == "halted"


# ── V3→V1 Dict Conversion ───────────────────────────────────────────


class TestV3ToV1Dict:
    """Test _v3_to_v1_dict produces correct V1-shaped output."""

    def test_has_line_items_alias(self, db_session: Session, test_quote: Quote, test_user: User):
        """V1 response includes both 'lines' and 'line_items' keys."""
        plan, _ = _make_plan_with_line(db_session, test_quote, test_user)
        result = _v3_to_v1_dict(plan)
        assert "lines" in result
        assert "line_items" in result
        assert result["lines"] == result["line_items"]

    def test_status_is_v1_mapped(self, db_session: Session, test_quote: Quote, test_user: User):
        """V1 dict uses mapped status name."""
        plan, _ = _make_plan_with_line(db_session, test_quote, test_user, status="pending")
        result = _v3_to_v1_dict(plan)
        assert result["status"] == "pending_approval"

    def test_includes_all_v1_fields(self, db_session: Session, test_quote: Quote, test_user: User):
        """V1 dict includes essential fields."""
        plan, _ = _make_plan_with_line(db_session, test_quote, test_user)
        result = _v3_to_v1_dict(plan)
        for key in (
            "id",
            "quote_id",
            "requisition_id",
            "status",
            "lines",
            "line_items",
            "line_count",
            "vendor_count",
            "total_cost",
        ):
            assert key in result, f"Missing key: {key}"


# ── V1 List Endpoint with Status Mapping ─────────────────────────────


class TestV1ListStatusMapping:
    """Verify GET /api/buy-plans returns V1-mapped statuses in list items."""

    def test_list_returns_v1_status_names(self, db_session: Session, test_quote: Quote, test_user: User):
        """List endpoint returns V1 status names."""
        plan, _ = _make_plan_with_line(db_session, test_quote, test_user, status="pending")
        c = _make_client(db_session, test_user)
        r = c.get("/api/buy-plans")
        assert r.status_code == 200
        items = r.json()["items"]
        matching = [i for i in items if i["id"] == plan.id]
        assert len(matching) == 1
        assert matching[0]["status"] == "pending_approval"

    def test_list_completed_shows_as_complete(self, db_session: Session, test_quote: Quote, test_user: User):
        """V3 'completed' appears as V1 'complete' in list."""
        plan, _ = _make_plan_with_line(db_session, test_quote, test_user, status="completed")
        c = _make_client(db_session, test_user)
        r = c.get("/api/buy-plans")
        assert r.status_code == 200
        items = r.json()["items"]
        matching = [i for i in items if i["id"] == plan.id]
        assert len(matching) == 1
        assert matching[0]["status"] == "complete"


# ── V1 Mutation Redirects ────────────────────────────────────────────


class TestV1MutationRedirects:
    """Verify deprecated V1 mutations return 410 with V3 endpoint info."""

    def test_build_410_has_v3_endpoint(self, db_session: Session, test_quote: Quote, test_user: User):
        c = _make_client(db_session, test_user)
        r = c.post(f"/api/quotes/{test_quote.id}/buy-plan/build")
        assert r.status_code == 410
        data = r.json()
        assert "v3_endpoint" in data
        assert str(test_quote.id) in data["v3_endpoint"]

    def test_submit_410_has_v3_endpoint(self, db_session: Session, test_quote: Quote, test_user: User):
        plan, _ = _make_plan_with_line(db_session, test_quote, test_user)
        c = _make_client(db_session, test_user)
        r = c.post(f"/api/buy-plans/{plan.id}/submit", json={"sales_order_number": "SO-001"})
        assert r.status_code == 410
        data = r.json()
        assert str(plan.id) in data["v3_endpoint"]

    def test_approve_410_has_v3_endpoint(self, db_session: Session, test_quote: Quote, test_user: User):
        plan, _ = _make_plan_with_line(db_session, test_quote, test_user)
        c = _make_client(db_session, test_user)
        r = c.post(f"/api/buy-plans/{plan.id}/approve", json={"action": "approve"})
        assert r.status_code == 410
        assert "approve" in r.json()["v3_endpoint"]

    def test_resubmit_410_has_v3_endpoint(self, db_session: Session, test_quote: Quote, test_user: User):
        plan, _ = _make_plan_with_line(db_session, test_quote, test_user)
        c = _make_client(db_session, test_user)
        r = c.post(f"/api/buy-plans/{plan.id}/resubmit", json={"sales_order_number": "SO-001"})
        assert r.status_code == 410
        assert "resubmit" in r.json()["v3_endpoint"]

    def test_reset_to_draft_410_has_v3_endpoint(self, db_session: Session, test_quote: Quote, test_user: User):
        plan, _ = _make_plan_with_line(db_session, test_quote, test_user)
        c = _make_client(db_session, test_user)
        r = c.post(f"/api/buy-plans/{plan.id}/reset-to-draft")
        assert r.status_code == 410
        assert "reset-to-draft" in r.json()["v3_endpoint"]
