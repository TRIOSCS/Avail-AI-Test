"""test_buy_plan_router.py — Buy Plan V1 Compatibility Layer Tests.

Covers the V1 adapter endpoints: GET list/detail return V1-shaped statuses,
deprecated mutations return 410 Gone, operational endpoints (verify-so,
confirm-po, verify-po, flag-issue) keep working, token endpoints keep working,
and service-layer intelligence tests.

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
    BuyPlanLineStatus,
    BuyPlanStatus,
    SOVerificationStatus,
    VerificationGroupMember,
)

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


def _make_draft_plan(db, test_quote, test_user, *, total_cost=500.0):
    """Create a draft plan with one line, ready for submit."""
    req = db.query(Requirement).filter_by(requisition_id=test_quote.requisition_id).first()
    offer = _make_offer(
        db,
        test_quote.requisition_id,
        req.id,
        entered_by_id=test_user.id,
    )
    plan = BuyPlan(
        quote_id=test_quote.id,
        requisition_id=test_quote.requisition_id,
        status=BuyPlanStatus.DRAFT.value,
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
        status=BuyPlanLineStatus.AWAITING_PO.value,
    )
    db.add(line)
    db.commit()
    db.refresh(plan)
    return plan, line, offer, req


def _make_ops_member(db, user):
    member = VerificationGroupMember(user_id=user.id, is_active=True)
    db.add(member)
    db.commit()
    return member


# ── Deprecated Mutations → 410 Gone ─────────────────────────────────


class TestDeprecatedMutations:
    """V1 workflow mutations (build, submit, approve, resubmit, reset-to-draft) should
    return 410 Gone directing consumers to the V3 API."""

    def test_build_returns_410(self, db_session: Session, test_quote: Quote, test_user: User):
        c = _make_client(db_session, test_user)
        r = c.post(f"/api/quotes/{test_quote.id}/buy-plan/build")
        assert r.status_code == 410
        data = r.json()
        assert "deprecated" in data["error"].lower()
        assert "v3" in data["v3_endpoint"].lower() or "v3" in data["error"].lower()
        assert data["status_code"] == 410

    def test_submit_returns_410(self, db_session: Session, test_quote: Quote, test_user: User):
        plan, _, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        c = _make_client(db_session, test_user)
        r = c.post(
            f"/api/buy-plans/{plan.id}/submit",
            json={"sales_order_number": "SO-2026-001"},
        )
        assert r.status_code == 410
        assert r.json()["status_code"] == 410

    def test_approve_returns_410(self, db_session: Session, test_quote: Quote, test_user: User, manager_user: User):
        plan, _, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        c = _make_client(db_session, manager_user)
        r = c.post(
            f"/api/buy-plans/{plan.id}/approve",
            json={"action": "approve"},
        )
        assert r.status_code == 410

    def test_resubmit_returns_410(self, db_session: Session, test_quote: Quote, test_user: User):
        plan, _, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        c = _make_client(db_session, test_user)
        r = c.post(
            f"/api/buy-plans/{plan.id}/resubmit",
            json={"sales_order_number": "SO-FIXED"},
        )
        assert r.status_code == 410

    def test_reset_to_draft_returns_410(self, db_session: Session, test_quote: Quote, test_user: User):
        plan, _, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        c = _make_client(db_session, test_user)
        r = c.post(f"/api/buy-plans/{plan.id}/reset-to-draft")
        assert r.status_code == 410


# ── Get / List (V1 status mapping) ──────────────────────────────────


class TestGetListEndpoints:
    def test_get_plan_draft(self, db_session: Session, test_quote: Quote, test_user: User):
        """GET detail for a draft plan returns V1 status 'draft'."""
        plan, _, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        c = _make_client(db_session, test_user)
        r = c.get(f"/api/buy-plans/{plan.id}")
        assert r.status_code == 200
        data = r.json()
        assert data["id"] == plan.id
        assert data["status"] == "draft"
        assert len(data["lines"]) == 1
        # V1 compat: line_items alias present
        assert "line_items" in data

    def test_get_plan_pending_maps_to_pending_approval(self, db_session: Session, test_quote: Quote, test_user: User):
        """V3 status 'pending' maps to V1 status 'pending_approval'."""
        plan, _, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.PENDING.value
        db_session.commit()

        c = _make_client(db_session, test_user)
        r = c.get(f"/api/buy-plans/{plan.id}")
        assert r.status_code == 200
        assert r.json()["status"] == "pending_approval"

    def test_get_plan_active_maps_to_approved(self, db_session: Session, test_quote: Quote, test_user: User):
        """V3 status 'active' with no POs maps to V1 status 'approved'."""
        plan, line, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.ACTIVE.value
        # No PO on line → maps to 'approved'
        db_session.commit()

        c = _make_client(db_session, test_user)
        r = c.get(f"/api/buy-plans/{plan.id}")
        assert r.status_code == 200
        assert r.json()["status"] == "approved"

    def test_get_plan_active_with_po_maps_to_po_entered(self, db_session: Session, test_quote: Quote, test_user: User):
        """V3 active with PO but awaiting_po status → V1 'po_entered'."""
        plan, line, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.ACTIVE.value
        line.po_number = "PO-001"
        line.status = BuyPlanLineStatus.AWAITING_PO.value
        db_session.commit()

        c = _make_client(db_session, test_user)
        r = c.get(f"/api/buy-plans/{plan.id}")
        assert r.status_code == 200
        assert r.json()["status"] == "po_entered"

    def test_get_plan_active_with_verified_po_maps_to_po_confirmed(
        self, db_session: Session, test_quote: Quote, test_user: User
    ):
        """V3 active with all PO lines pending_verify/verified → V1 'po_confirmed'."""
        plan, line, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.ACTIVE.value
        line.po_number = "PO-001"
        line.status = BuyPlanLineStatus.PENDING_VERIFY.value
        db_session.commit()

        c = _make_client(db_session, test_user)
        r = c.get(f"/api/buy-plans/{plan.id}")
        assert r.status_code == 200
        assert r.json()["status"] == "po_confirmed"

    def test_get_plan_completed_maps_to_complete(self, db_session: Session, test_quote: Quote, test_user: User):
        """V3 status 'completed' maps to V1 status 'complete'."""
        plan, _, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.COMPLETED.value
        db_session.commit()

        c = _make_client(db_session, test_user)
        r = c.get(f"/api/buy-plans/{plan.id}")
        assert r.status_code == 200
        assert r.json()["status"] == "complete"

    def test_get_plan_draft_with_rejection_maps_to_rejected(
        self, db_session: Session, test_quote: Quote, test_user: User
    ):
        """V3 draft with cancellation_reason → V1 'rejected'."""
        plan, _, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.DRAFT.value
        plan.cancellation_reason = "Fix margin"
        db_session.commit()

        c = _make_client(db_session, test_user)
        r = c.get(f"/api/buy-plans/{plan.id}")
        assert r.status_code == 200
        assert r.json()["status"] == "rejected"

    def test_get_not_found(self, db_session: Session, test_user: User):
        c = _make_client(db_session, test_user)
        r = c.get("/api/buy-plans/99999")
        assert r.status_code == 404

    def test_list_all(self, db_session: Session, test_quote: Quote, test_user: User):
        _make_draft_plan(db_session, test_quote, test_user)
        c = _make_client(db_session, test_user)
        r = c.get("/api/buy-plans")
        assert r.status_code == 200
        data = r.json()
        assert data["count"] >= 1
        assert len(data["items"]) >= 1

    def test_list_filter_status(self, db_session: Session, test_quote: Quote, test_user: User):
        """V1 status filter 'draft' works for draft plans."""
        plan, _, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        c = _make_client(db_session, test_user)
        r = c.get("/api/buy-plans?status=draft")
        assert r.status_code == 200
        assert r.json()["count"] >= 1

        r2 = c.get("/api/buy-plans?status=completed")
        assert r2.json()["count"] == 0

    def test_list_filter_v1_status_pending_approval(self, db_session: Session, test_quote: Quote, test_user: User):
        """V1 status filter 'pending_approval' translates to V3 'pending' for
        querying."""
        plan, _, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.PENDING.value
        db_session.commit()

        c = _make_client(db_session, test_user)
        r = c.get("/api/buy-plans?status=pending_approval")
        assert r.status_code == 200
        assert r.json()["count"] >= 1
        # Status in response should be V1-mapped
        assert r.json()["items"][0]["status"] == "pending_approval"

    def test_list_filter_quote_id(self, db_session: Session, test_quote: Quote, test_user: User):
        _make_draft_plan(db_session, test_quote, test_user)
        c = _make_client(db_session, test_user)
        r = c.get(f"/api/buy-plans?quote_id={test_quote.id}")
        assert r.status_code == 200
        assert r.json()["count"] >= 1

        r2 = c.get("/api/buy-plans?quote_id=99999")
        assert r2.json()["count"] == 0


# ── Verify SO (kept — operational) ──────────────────────────────────


class TestVerifySOEndpoint:
    def test_approve_so(
        self,
        db_session: Session,
        test_quote: Quote,
        test_user: User,
        admin_user: User,
    ):
        plan, _, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.ACTIVE.value
        _make_ops_member(db_session, admin_user)

        c = _make_client(db_session, admin_user)
        r = c.post(
            f"/api/buy-plans/{plan.id}/verify-so",
            json={"action": "approve"},
        )
        assert r.status_code == 200
        assert r.json()["so_status"] == "approved"

    def test_halt_so(
        self,
        db_session: Session,
        test_quote: Quote,
        test_user: User,
        admin_user: User,
    ):
        plan, _, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.ACTIVE.value
        _make_ops_member(db_session, admin_user)

        c = _make_client(db_session, admin_user)
        r = c.post(
            f"/api/buy-plans/{plan.id}/verify-so",
            json={"action": "halt", "rejection_note": "Fraud suspected"},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "halted"

    def test_non_ops_rejected(
        self,
        db_session: Session,
        test_quote: Quote,
        test_user: User,
    ):
        plan, _, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.ACTIVE.value
        db_session.commit()

        c = _make_client(db_session, test_user)
        r = c.post(
            f"/api/buy-plans/{plan.id}/verify-so",
            json={"action": "approve"},
        )
        assert r.status_code == 403


# ── Confirm PO (kept — operational) ─────────────────────────────────


class TestConfirmPOEndpoint:
    def test_confirm_po(self, db_session: Session, test_quote: Quote, test_user: User):
        plan, line, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.ACTIVE.value
        db_session.commit()

        c = _make_client(db_session, test_user)
        r = c.post(
            f"/api/buy-plans/{plan.id}/lines/{line.id}/confirm-po",
            json={
                "po_number": "PO-2026-042",
                "estimated_ship_date": "2026-03-15T00:00:00Z",
            },
        )
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["po_number"] == "PO-2026-042"
        assert data["status"] == "pending_verify"

    def test_confirm_po_wrong_status(self, db_session: Session, test_quote: Quote, test_user: User):
        plan, line, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        # plan is draft, not active
        c = _make_client(db_session, test_user)
        r = c.post(
            f"/api/buy-plans/{plan.id}/lines/{line.id}/confirm-po",
            json={
                "po_number": "PO-001",
                "estimated_ship_date": "2026-03-15T00:00:00Z",
            },
        )
        assert r.status_code == 400


# ── Verify PO (kept — operational) ──────────────────────────────────


class TestVerifyPOEndpoint:
    def test_verify_po(
        self,
        db_session: Session,
        test_quote: Quote,
        test_user: User,
        admin_user: User,
    ):
        plan, line, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.ACTIVE.value
        line.status = BuyPlanLineStatus.PENDING_VERIFY.value
        line.po_number = "PO-001"
        _make_ops_member(db_session, admin_user)

        c = _make_client(db_session, admin_user)
        r = c.post(
            f"/api/buy-plans/{plan.id}/lines/{line.id}/verify-po",
            json={"action": "approve"},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "verified"

    def test_reject_po(
        self,
        db_session: Session,
        test_quote: Quote,
        test_user: User,
        admin_user: User,
    ):
        plan, line, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.ACTIVE.value
        line.status = BuyPlanLineStatus.PENDING_VERIFY.value
        _make_ops_member(db_session, admin_user)

        c = _make_client(db_session, admin_user)
        r = c.post(
            f"/api/buy-plans/{plan.id}/lines/{line.id}/verify-po",
            json={"action": "reject", "rejection_note": "Wrong amount"},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "awaiting_po"


# ── Flag Issue (kept — operational) ─────────────────────────────────


class TestFlagIssueEndpoint:
    def test_flag_sold_out(self, db_session: Session, test_quote: Quote, test_user: User):
        plan, line, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.ACTIVE.value
        db_session.commit()

        c = _make_client(db_session, test_user)
        r = c.post(
            f"/api/buy-plans/{plan.id}/lines/{line.id}/issue",
            json={"issue_type": "sold_out"},
        )
        assert r.status_code == 200
        assert r.json()["issue_type"] == "sold_out"

    def test_other_requires_note(self, db_session: Session, test_quote: Quote, test_user: User):
        """'other' without note rejected by Pydantic — returns 422."""
        plan, line, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.ACTIVE.value
        db_session.commit()

        c = _make_client(db_session, test_user)
        resp = c.post(
            f"/api/buy-plans/{plan.id}/lines/{line.id}/issue",
            json={"issue_type": "other"},
        )
        assert resp.status_code == 422


# ── Offer Comparison ─────────────────────────────────────────────────


class TestOfferComparison:
    def test_offer_comparison(self, db_session: Session, test_quote: Quote, test_user: User):
        plan, line, offer, req = _make_draft_plan(db_session, test_quote, test_user)
        # Add a second offer for same requirement
        _make_offer(
            db_session,
            test_quote.requisition_id,
            req.id,
            vendor_name="Digi-Key",
            unit_price=0.55,
        )
        db_session.commit()

        c = _make_client(db_session, test_user)
        r = c.get(f"/api/buy-plans/{plan.id}/offers/{req.id}")
        assert r.status_code == 200
        data = r.json()
        assert data["requirement_id"] == req.id
        assert len(data["offers"]) == 2
        assert offer.id in data["selected_offer_ids"]


# ── Verification Group ───────────────────────────────────────────────


class TestVerificationGroup:
    def test_list_empty(self, db_session: Session, test_user: User):
        c = _make_client(db_session, test_user)
        r = c.get("/api/buy-plans/verification-group")
        assert r.status_code == 200
        assert r.json()["items"] == []

    def test_add_member(self, db_session: Session, admin_user: User, test_user: User):
        c = _make_client(db_session, admin_user)
        r = c.post(
            "/api/buy-plans/verification-group",
            json={"user_id": test_user.id, "action": "add"},
        )
        assert r.status_code == 200
        assert r.json()["action"] == "added"

        # Verify it appears in list
        r2 = c.get("/api/buy-plans/verification-group")
        assert len(r2.json()["items"]) == 1

    def test_remove_member(self, db_session: Session, admin_user: User, test_user: User):
        _make_ops_member(db_session, test_user)
        c = _make_client(db_session, admin_user)
        r = c.post(
            "/api/buy-plans/verification-group",
            json={"user_id": test_user.id, "action": "remove"},
        )
        assert r.status_code == 200
        assert r.json()["action"] == "removed"

    def test_non_admin_rejected(self, db_session: Session, test_user: User):
        c = _make_client(db_session, test_user)
        r = c.post(
            "/api/buy-plans/verification-group",
            json={"user_id": test_user.id, "action": "add"},
        )
        assert r.status_code == 403


# ── Intelligence: AI Flags ──────────────────────────────────────────


class TestEnhancedAIFlags:
    def test_stale_offer_flag(self, db_session: Session, test_quote: Quote, test_user: User):
        """Offers older than threshold trigger a stale_offer flag."""
        from app.services.buyplan_service import generate_ai_flags

        req = db_session.query(Requirement).filter_by(requisition_id=test_quote.requisition_id).first()
        old_offer = _make_offer(
            db_session,
            test_quote.requisition_id,
            req.id,
            created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        )
        plan = BuyPlan(
            quote_id=test_quote.id,
            requisition_id=test_quote.requisition_id,
            status=BuyPlanStatus.DRAFT.value,
        )
        db_session.add(plan)
        db_session.flush()
        line = BuyPlanLine(
            buy_plan_id=plan.id,
            requirement_id=req.id,
            offer_id=old_offer.id,
            quantity=100,
            unit_cost=0.50,
            unit_sell=0.75,
            margin_pct=33.33,
            status=BuyPlanLineStatus.AWAITING_PO.value,
        )
        db_session.add(line)
        db_session.flush()
        plan.lines = [line]
        line.offer = old_offer

        flags = generate_ai_flags(plan, db_session)
        stale = [f for f in flags if f["type"] == "stale_offer"]
        assert len(stale) == 1
        assert "days old" in stale[0]["message"]

    def test_low_margin_flag(self, db_session: Session, test_quote: Quote, test_user: User):
        """Lines with margin below threshold trigger a low_margin flag."""
        from app.services.buyplan_service import generate_ai_flags

        req = db_session.query(Requirement).filter_by(requisition_id=test_quote.requisition_id).first()
        offer = _make_offer(db_session, test_quote.requisition_id, req.id)
        plan = BuyPlan(
            quote_id=test_quote.id,
            requisition_id=test_quote.requisition_id,
            status=BuyPlanStatus.DRAFT.value,
        )
        db_session.add(plan)
        db_session.flush()
        line = BuyPlanLine(
            buy_plan_id=plan.id,
            requirement_id=req.id,
            offer_id=offer.id,
            quantity=100,
            unit_cost=0.95,
            unit_sell=1.00,
            margin_pct=5.0,
            status=BuyPlanLineStatus.AWAITING_PO.value,
        )
        db_session.add(line)
        db_session.flush()
        plan.lines = [line]
        line.offer = offer

        flags = generate_ai_flags(plan, db_session)
        low = [f for f in flags if f["type"] == "low_margin"]
        assert len(low) == 1
        assert "5.0%" in low[0]["message"]

    def test_better_offer_flag(self, db_session: Session, test_quote: Quote, test_user: User):
        """When a cheaper alternative exists, a better_offer flag is raised."""
        from app.services.buyplan_service import generate_ai_flags

        req = db_session.query(Requirement).filter_by(requisition_id=test_quote.requisition_id).first()
        expensive = _make_offer(
            db_session,
            test_quote.requisition_id,
            req.id,
            vendor_name="Expensive Co",
            unit_price=1.00,
        )
        _make_offer(
            db_session,
            test_quote.requisition_id,
            req.id,
            vendor_name="Cheap Co",
            unit_price=0.80,
        )
        plan = BuyPlan(
            quote_id=test_quote.id,
            requisition_id=test_quote.requisition_id,
            status=BuyPlanStatus.DRAFT.value,
        )
        db_session.add(plan)
        db_session.flush()
        line = BuyPlanLine(
            buy_plan_id=plan.id,
            requirement_id=req.id,
            offer_id=expensive.id,
            quantity=100,
            unit_cost=1.00,
            unit_sell=1.50,
            margin_pct=33.33,
            status=BuyPlanLineStatus.AWAITING_PO.value,
        )
        db_session.add(line)
        db_session.flush()
        plan.lines = [line]
        line.offer = expensive

        flags = generate_ai_flags(plan, db_session)
        better = [f for f in flags if f["type"] == "better_offer"]
        assert len(better) == 1
        assert "Cheap Co" in better[0]["message"]

    def test_quantity_gap_flag(self, db_session: Session, test_quote: Quote, test_user: User):
        """When allocated qty < required qty, a quantity_gap flag is raised."""
        from app.services.buyplan_service import generate_ai_flags

        req = db_session.query(Requirement).filter_by(requisition_id=test_quote.requisition_id).first()
        req.target_qty = 1000
        db_session.flush()

        offer = _make_offer(db_session, test_quote.requisition_id, req.id)
        plan = BuyPlan(
            quote_id=test_quote.id,
            requisition_id=test_quote.requisition_id,
            status=BuyPlanStatus.DRAFT.value,
        )
        db_session.add(plan)
        db_session.flush()
        line = BuyPlanLine(
            buy_plan_id=plan.id,
            requirement_id=req.id,
            offer_id=offer.id,
            quantity=500,  # less than 1000
            unit_cost=0.50,
            unit_sell=0.75,
            status=BuyPlanLineStatus.AWAITING_PO.value,
        )
        db_session.add(line)
        db_session.flush()
        plan.lines = [line]
        line.offer = offer
        line.requirement = req

        flags = generate_ai_flags(plan, db_session)
        gap = [f for f in flags if f["type"] == "quantity_gap"]
        assert len(gap) == 1
        assert "gap: 500" in gap[0]["message"]


# ── Intelligence: Favoritism ────────────────────────────────────────


class TestFavoritism:
    def test_favoritism_detected(
        self,
        db_session: Session,
        test_quote: Quote,
        test_user: User,
        admin_user: User,
    ):
        """When one buyer gets >60% of lines, favoritism is flagged."""
        from app.services.buyplan_service import detect_favoritism

        req = db_session.query(Requirement).filter_by(requisition_id=test_quote.requisition_id).first()
        offer = _make_offer(db_session, test_quote.requisition_id, req.id)

        # Create 3 plans all assigned to test_user (same buyer)
        for i in range(3):
            plan = BuyPlan(
                quote_id=test_quote.id,
                requisition_id=test_quote.requisition_id,
                status="active",
                submitted_by_id=admin_user.id,
            )
            db_session.add(plan)
            db_session.flush()
            line = BuyPlanLine(
                buy_plan_id=plan.id,
                requirement_id=req.id,
                offer_id=offer.id,
                quantity=100,
                unit_cost=0.50,
                buyer_id=test_user.id,
                status=BuyPlanLineStatus.AWAITING_PO.value,
            )
            db_session.add(line)

        db_session.commit()
        findings = detect_favoritism(admin_user.id, db_session)
        assert len(findings) == 1
        assert findings[0]["buyer_id"] == test_user.id
        assert findings[0]["pct"] == 100.0

    def test_favoritism_not_enough_data(
        self,
        db_session: Session,
        test_quote: Quote,
        admin_user: User,
    ):
        """Less than 3 plans returns no findings."""
        from app.services.buyplan_service import detect_favoritism

        findings = detect_favoritism(admin_user.id, db_session)
        assert findings == []

    def test_favoritism_endpoint(
        self,
        db_session: Session,
        test_user: User,
        admin_user: User,
    ):
        """GET /api/buy-plans/favoritism/{user_id} works for admins."""
        c = _make_client(db_session, admin_user)
        r = c.get(f"/api/buy-plans/favoritism/{test_user.id}")
        assert r.status_code == 200
        assert "findings" in r.json()

    def test_favoritism_non_admin_rejected(
        self,
        db_session: Session,
        test_user: User,
    ):
        """Non-admin/manager users cannot access favoritism report."""
        c = _make_client(db_session, test_user)
        r = c.get(f"/api/buy-plans/favoritism/{test_user.id}")
        assert r.status_code == 403


# ── Intelligence: Case Report ───────────────────────────────────────


class TestCaseReport:
    def test_case_report_on_completion(
        self,
        db_session: Session,
        test_quote: Quote,
        test_user: User,
    ):
        """Case report is generated when plan auto-completes."""
        from app.services.buyplan_service import check_completion

        req = db_session.query(Requirement).filter_by(requisition_id=test_quote.requisition_id).first()
        offer = _make_offer(db_session, test_quote.requisition_id, req.id)
        plan = BuyPlan(
            quote_id=test_quote.id,
            requisition_id=test_quote.requisition_id,
            status=BuyPlanStatus.ACTIVE.value,
            so_status=SOVerificationStatus.APPROVED.value,
            submitted_by_id=test_user.id,
            submitted_at=datetime.now(timezone.utc),
            total_cost=500.0,
            total_revenue=750.0,
            sales_order_number="SO-001",
        )
        db_session.add(plan)
        db_session.flush()
        line = BuyPlanLine(
            buy_plan_id=plan.id,
            requirement_id=req.id,
            offer_id=offer.id,
            quantity=100,
            unit_cost=0.50,
            unit_sell=0.75,
            buyer_id=test_user.id,
            status=BuyPlanLineStatus.VERIFIED.value,
        )
        db_session.add(line)
        db_session.commit()

        result = check_completion(plan.id, db_session)
        assert result.status == "completed"
        assert result.case_report is not None
        assert "CASE REPORT" in result.case_report
        assert "SO-001" in result.case_report

    def test_case_report_endpoint(
        self,
        db_session: Session,
        test_quote: Quote,
        test_user: User,
    ):
        """POST /api/buy-plans/{id}/case-report regenerates the report."""
        req = db_session.query(Requirement).filter_by(requisition_id=test_quote.requisition_id).first()
        offer = _make_offer(db_session, test_quote.requisition_id, req.id)
        plan = BuyPlan(
            quote_id=test_quote.id,
            requisition_id=test_quote.requisition_id,
            status="completed",
            so_status="approved",
            total_cost=500.0,
            total_revenue=750.0,
            submitted_by_id=test_user.id,
            completed_at=datetime.now(timezone.utc),
        )
        db_session.add(plan)
        db_session.flush()
        line = BuyPlanLine(
            buy_plan_id=plan.id,
            requirement_id=req.id,
            offer_id=offer.id,
            quantity=100,
            unit_cost=0.50,
            status="verified",
        )
        db_session.add(line)
        db_session.commit()

        c = _make_client(db_session, test_user)
        r = c.post(f"/api/buy-plans/{plan.id}/case-report")
        assert r.status_code == 200
        assert "CASE REPORT" in r.json()["case_report"]

    def test_case_report_not_completed_rejected(
        self,
        db_session: Session,
        test_quote: Quote,
        test_user: User,
    ):
        """Case report endpoint rejects non-completed plans."""
        plan = BuyPlan(
            quote_id=test_quote.id,
            requisition_id=test_quote.requisition_id,
            status="active",
        )
        db_session.add(plan)
        db_session.commit()

        c = _make_client(db_session, test_user)
        r = c.post(f"/api/buy-plans/{plan.id}/case-report")
        assert r.status_code == 400

    def test_case_report_not_found(
        self,
        db_session: Session,
        test_user: User,
    ):
        """Case report for nonexistent plan → 404 (line 307)."""
        c = _make_client(db_session, test_user)
        r = c.post("/api/buy-plans/99999/case-report")
        assert r.status_code == 404


# ── Verification Group Edge Cases ──────────────────────────────────


class TestVerificationGroupEdgeCases:
    """Cover lines 244 (user not found) and 253 (reactivate existing member)."""

    def test_add_nonexistent_user(self, db_session: Session, admin_user: User):
        """Add user_id that doesn't exist → 404 (line 244)."""
        c = _make_client(db_session, admin_user)
        r = c.post(
            "/api/buy-plans/verification-group",
            json={"user_id": 99999, "action": "add"},
        )
        assert r.status_code == 404

    def test_reactivate_existing_member(
        self,
        db_session: Session,
        admin_user: User,
        test_user: User,
    ):
        """Add a user who was previously removed → reactivates (line 253)."""
        member = _make_ops_member(db_session, test_user)
        member.is_active = False
        db_session.commit()

        c = _make_client(db_session, admin_user)
        r = c.post(
            "/api/buy-plans/verification-group",
            json={"user_id": test_user.id, "action": "add"},
        )
        assert r.status_code == 200
        assert r.json()["action"] == "added"
        db_session.refresh(member)
        assert member.is_active is True


# ── Favoritism Edge Cases ──────────────────────────────────────────


class TestFavoritismEdgeCases:
    def test_favoritism_user_not_found(
        self,
        db_session: Session,
        admin_user: User,
    ):
        """Favoritism report for nonexistent user → 404 (line 285)."""
        c = _make_client(db_session, admin_user)
        r = c.get("/api/buy-plans/favoritism/99999")
        assert r.status_code == 404


# ── List V1 Filter Edge Cases ──────────────────────────────────────


class TestListV1Filters:
    """Cover so_status, buyer_id, sales-user filters."""

    def test_filter_by_so_status(
        self,
        db_session: Session,
        test_quote: Quote,
        test_user: User,
    ):
        """Filter by so_status."""
        plan, _, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.so_status = "approved"
        db_session.commit()

        c = _make_client(db_session, test_user)
        r = c.get("/api/buy-plans?so_status=approved")
        assert r.status_code == 200
        assert r.json()["count"] >= 1

    def test_filter_by_buyer_id(
        self,
        db_session: Session,
        test_quote: Quote,
        test_user: User,
    ):
        """Filter by buyer_id."""
        plan, line, _, _ = _make_draft_plan(db_session, test_quote, test_user)

        c = _make_client(db_session, test_user)
        r = c.get(f"/api/buy-plans?buyer_id={test_user.id}")
        assert r.status_code == 200
        assert r.json()["count"] >= 1

    def test_sales_user_sees_own(
        self,
        db_session: Session,
        test_quote: Quote,
        test_user: User,
        sales_user,
    ):
        """Sales user only sees own plans."""
        plan, _, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.submitted_by_id = test_user.id
        db_session.commit()

        # Sales user with different id should see 0 plans
        sales = sales_user
        c = _make_client(db_session, sales)
        r = c.get("/api/buy-plans")
        assert r.status_code == 200
        assert r.json()["count"] == 0


# ── Verify SO Edge Cases ───────────────────────────────────────────


class TestVerifySOEdgeCases:
    def test_verify_so_value_error(
        self,
        db_session: Session,
        test_quote: Quote,
        test_user: User,
        admin_user: User,
    ):
        """Verify SO on already-verified SO → ValueError → 400."""
        plan, _, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.ACTIVE.value
        plan.so_status = "approved"  # Already verified
        _make_ops_member(db_session, admin_user)

        c = _make_client(db_session, admin_user)
        r = c.post(
            f"/api/buy-plans/{plan.id}/verify-so",
            json={"action": "approve"},
        )
        assert r.status_code == 400


# ── Verify PO Edge Cases ───────────────────────────────────────────


class TestVerifyPOEdgeCases:
    def test_verify_po_value_error(
        self,
        db_session: Session,
        test_quote: Quote,
        test_user: User,
        admin_user: User,
    ):
        """Verify PO on line not in pending_verify → ValueError → 400."""
        plan, line, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.ACTIVE.value
        line.status = BuyPlanLineStatus.AWAITING_PO.value  # not pending_verify
        _make_ops_member(db_session, admin_user)

        c = _make_client(db_session, admin_user)
        r = c.post(
            f"/api/buy-plans/{plan.id}/lines/{line.id}/verify-po",
            json={"action": "approve"},
        )
        assert r.status_code == 400

    def test_verify_po_permission_error(
        self,
        db_session: Session,
        test_quote: Quote,
        test_user: User,
    ):
        """Verify PO by non-ops user → PermissionError → 403."""
        plan, line, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.ACTIVE.value
        line.status = BuyPlanLineStatus.PENDING_VERIFY.value
        line.po_number = "PO-001"
        db_session.commit()

        c = _make_client(db_session, test_user)
        r = c.post(
            f"/api/buy-plans/{plan.id}/lines/{line.id}/verify-po",
            json={"action": "approve"},
        )
        assert r.status_code == 403

    def test_verify_po_triggers_auto_complete(
        self,
        db_session: Session,
        test_quote: Quote,
        test_user: User,
        admin_user: User,
    ):
        """Verify last PO → auto-complete plan."""
        plan, line, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.ACTIVE.value
        plan.so_status = SOVerificationStatus.APPROVED.value
        plan.submitted_by_id = test_user.id
        plan.submitted_at = datetime.now(timezone.utc)
        plan.sales_order_number = "SO-COMPLETE"
        plan.total_cost = 500.0
        plan.total_revenue = 750.0
        line.status = BuyPlanLineStatus.PENDING_VERIFY.value
        line.po_number = "PO-DONE"
        _make_ops_member(db_session, admin_user)

        c = _make_client(db_session, admin_user)
        r = c.post(
            f"/api/buy-plans/{plan.id}/lines/{line.id}/verify-po",
            json={"action": "approve"},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "verified"
        # Plan should auto-complete
        db_session.refresh(plan)
        assert plan.status == "completed"


# ── Flag Issue Edge Cases ──────────────────────────────────────────


class TestFlagIssueEdgeCases:
    def test_flag_issue_value_error(
        self,
        db_session: Session,
        test_quote: Quote,
        test_user: User,
    ):
        """Flag issue on non-active plan → ValueError → 400."""
        plan, line, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        # Plan is draft, not active

        c = _make_client(db_session, test_user)
        r = c.post(
            f"/api/buy-plans/{plan.id}/lines/{line.id}/issue",
            json={"issue_type": "sold_out"},
        )
        assert r.status_code == 400


# ── Offer Comparison Edge Cases ───────────────────────────────────


class TestOfferComparisonEdgeCases:
    def test_offer_comparison_plan_not_found(
        self,
        db_session: Session,
        test_user: User,
    ):
        """Offer comparison for nonexistent plan → 404."""
        c = _make_client(db_session, test_user)
        r = c.get("/api/buy-plans/99999/offers/1")
        assert r.status_code == 404

    def test_offer_comparison_requirement_not_found(
        self,
        db_session: Session,
        test_quote: Quote,
        test_user: User,
    ):
        """Offer comparison for nonexistent requirement → 404."""
        plan, _, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        c = _make_client(db_session, test_user)
        r = c.get(f"/api/buy-plans/{plan.id}/offers/99999")
        assert r.status_code == 404
