"""
test_buy_plan_v3_router.py — Buy Plan V3 API Endpoint Tests

Covers all V3 endpoints via TestClient: build, get, list, submit, approve,
verify-so, confirm-po, verify-po, flag-issue, resubmit, offer comparison,
and verification group CRUD.

Called by: pytest
Depends on: conftest.py fixtures, app.routers.crm.buy_plans_v3
"""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import require_buyer, require_user
from app.main import app
from app.models import Offer, Quote, Requirement, User
from app.models.buy_plan import (
    BuyPlanLine,
    BuyPlanLineStatus,
    BuyPlanStatus,
    BuyPlanV3,
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
    plan = BuyPlanV3(
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
    return plan, line, offer, req


def _make_ops_member(db, user):
    member = VerificationGroupMember(user_id=user.id, is_active=True)
    db.add(member)
    db.commit()
    return member


# ── Build ────────────────────────────────────────────────────────────


class TestBuildEndpoint:
    def test_build_creates_draft(self, db_session: Session, test_quote: Quote, test_user: User):
        """POST build → returns draft plan with lines."""
        req = db_session.query(Requirement).filter_by(requisition_id=test_quote.requisition_id).first()
        _make_offer(
            db_session,
            test_quote.requisition_id,
            req.id,
            entered_by_id=test_user.id,
        )
        db_session.commit()

        c = _make_client(db_session, test_user)
        r = c.post(f"/api/quotes/{test_quote.id}/buy-plan-v3/build")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "draft"
        assert data["quote_id"] == test_quote.id
        assert len(data["lines"]) >= 1

    def test_build_invalid_quote(self, db_session: Session, test_user: User):
        c = _make_client(db_session, test_user)
        r = c.post("/api/quotes/99999/buy-plan-v3/build")
        assert r.status_code == 400


# ── Get / List ───────────────────────────────────────────────────────


class TestGetListEndpoints:
    def test_get_plan(self, db_session: Session, test_quote: Quote, test_user: User):
        plan, _, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        c = _make_client(db_session, test_user)
        r = c.get(f"/api/buy-plans-v3/{plan.id}")
        assert r.status_code == 200
        data = r.json()
        assert data["id"] == plan.id
        assert len(data["lines"]) == 1

    def test_get_not_found(self, db_session: Session, test_user: User):
        c = _make_client(db_session, test_user)
        r = c.get("/api/buy-plans-v3/99999")
        assert r.status_code == 404

    def test_list_all(self, db_session: Session, test_quote: Quote, test_user: User):
        _make_draft_plan(db_session, test_quote, test_user)
        c = _make_client(db_session, test_user)
        r = c.get("/api/buy-plans-v3")
        assert r.status_code == 200
        data = r.json()
        assert data["count"] >= 1
        assert len(data["items"]) >= 1

    def test_list_filter_status(self, db_session: Session, test_quote: Quote, test_user: User):
        plan, _, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        c = _make_client(db_session, test_user)
        r = c.get("/api/buy-plans-v3?status=draft")
        assert r.status_code == 200
        assert r.json()["count"] >= 1

        r2 = c.get("/api/buy-plans-v3?status=completed")
        assert r2.json()["count"] == 0

    def test_list_filter_quote_id(self, db_session: Session, test_quote: Quote, test_user: User):
        _make_draft_plan(db_session, test_quote, test_user)
        c = _make_client(db_session, test_user)
        r = c.get(f"/api/buy-plans-v3?quote_id={test_quote.id}")
        assert r.status_code == 200
        assert r.json()["count"] >= 1

        r2 = c.get("/api/buy-plans-v3?quote_id=99999")
        assert r2.json()["count"] == 0


# ── Submit ───────────────────────────────────────────────────────────


class TestSubmitEndpoint:
    def test_submit_auto_approve(self, db_session: Session, test_quote: Quote, test_user: User):
        plan, _, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        c = _make_client(db_session, test_user)
        r = c.post(
            f"/api/buy-plans-v3/{plan.id}/submit",
            json={"sales_order_number": "SO-2026-001"},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["status"] == "active"
        assert data["auto_approved"] is True

    def test_submit_needs_approval(self, db_session: Session, test_quote: Quote, test_user: User):
        plan, _, _, _ = _make_draft_plan(
            db_session,
            test_quote,
            test_user,
            total_cost=10000.0,
        )
        c = _make_client(db_session, test_user)
        r = c.post(
            f"/api/buy-plans-v3/{plan.id}/submit",
            json={"sales_order_number": "SO-BIG"},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "pending"

    def test_submit_blank_so_rejected(self, db_session: Session, test_quote: Quote, test_user: User):
        """Blank SO# rejected by FastAPI schema validation (422)."""
        plan, _, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        c = _make_client(db_session, test_user)
        resp = c.post(
            f"/api/buy-plans-v3/{plan.id}/submit",
            json={"sales_order_number": ""},
        )
        assert resp.status_code in (400, 422)


# ── Approve ──────────────────────────────────────────────────────────


class TestApproveEndpoint:
    def test_approve(
        self,
        db_session: Session,
        test_quote: Quote,
        test_user: User,
        manager_user: User,
    ):
        plan, _, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.pending.value
        db_session.commit()

        c = _make_client(db_session, manager_user)
        r = c.post(
            f"/api/buy-plans-v3/{plan.id}/approve",
            json={"action": "approve", "notes": "LGTM"},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "active"

    def test_reject(
        self,
        db_session: Session,
        test_quote: Quote,
        test_user: User,
        manager_user: User,
    ):
        plan, _, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.pending.value
        db_session.commit()

        c = _make_client(db_session, manager_user)
        r = c.post(
            f"/api/buy-plans-v3/{plan.id}/approve",
            json={"action": "reject", "notes": "Fix margin"},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "draft"

    def test_non_manager_rejected(self, db_session: Session, test_quote: Quote, test_user: User):
        """Sales/buyer cannot approve."""
        plan, _, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.pending.value
        db_session.commit()

        c = _make_client(db_session, test_user)  # buyer role
        r = c.post(
            f"/api/buy-plans-v3/{plan.id}/approve",
            json={"action": "approve"},
        )
        assert r.status_code == 403


# ── Verify SO ────────────────────────────────────────────────────────


class TestVerifySOEndpoint:
    def test_approve_so(
        self,
        db_session: Session,
        test_quote: Quote,
        test_user: User,
        admin_user: User,
    ):
        plan, _, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.active.value
        _make_ops_member(db_session, admin_user)

        c = _make_client(db_session, admin_user)
        r = c.post(
            f"/api/buy-plans-v3/{plan.id}/verify-so",
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
        plan.status = BuyPlanStatus.active.value
        _make_ops_member(db_session, admin_user)

        c = _make_client(db_session, admin_user)
        r = c.post(
            f"/api/buy-plans-v3/{plan.id}/verify-so",
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
        plan.status = BuyPlanStatus.active.value
        db_session.commit()

        c = _make_client(db_session, test_user)
        r = c.post(
            f"/api/buy-plans-v3/{plan.id}/verify-so",
            json={"action": "approve"},
        )
        assert r.status_code == 403


# ── Confirm PO ───────────────────────────────────────────────────────


class TestConfirmPOEndpoint:
    def test_confirm_po(self, db_session: Session, test_quote: Quote, test_user: User):
        plan, line, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.active.value
        db_session.commit()

        c = _make_client(db_session, test_user)
        r = c.post(
            f"/api/buy-plans-v3/{plan.id}/lines/{line.id}/confirm-po",
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
            f"/api/buy-plans-v3/{plan.id}/lines/{line.id}/confirm-po",
            json={
                "po_number": "PO-001",
                "estimated_ship_date": "2026-03-15T00:00:00Z",
            },
        )
        assert r.status_code == 400


# ── Verify PO ────────────────────────────────────────────────────────


class TestVerifyPOEndpoint:
    def test_verify_po(
        self,
        db_session: Session,
        test_quote: Quote,
        test_user: User,
        admin_user: User,
    ):
        plan, line, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.active.value
        line.status = BuyPlanLineStatus.pending_verify.value
        line.po_number = "PO-001"
        _make_ops_member(db_session, admin_user)

        c = _make_client(db_session, admin_user)
        r = c.post(
            f"/api/buy-plans-v3/{plan.id}/lines/{line.id}/verify-po",
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
        plan.status = BuyPlanStatus.active.value
        line.status = BuyPlanLineStatus.pending_verify.value
        _make_ops_member(db_session, admin_user)

        c = _make_client(db_session, admin_user)
        r = c.post(
            f"/api/buy-plans-v3/{plan.id}/lines/{line.id}/verify-po",
            json={"action": "reject", "rejection_note": "Wrong amount"},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "awaiting_po"


# ── Flag Issue ───────────────────────────────────────────────────────


class TestFlagIssueEndpoint:
    def test_flag_sold_out(self, db_session: Session, test_quote: Quote, test_user: User):
        plan, line, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.active.value
        db_session.commit()

        c = _make_client(db_session, test_user)
        r = c.post(
            f"/api/buy-plans-v3/{plan.id}/lines/{line.id}/issue",
            json={"issue_type": "sold_out"},
        )
        assert r.status_code == 200
        assert r.json()["issue_type"] == "sold_out"

    def test_other_requires_note(self, db_session: Session, test_quote: Quote, test_user: User):
        """'other' without note rejected by Pydantic (schema validation in test_buy_plan_schemas.py)."""
        plan, line, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.active.value
        db_session.commit()

        c = _make_client(db_session, test_user)
        resp = c.post(
            f"/api/buy-plans-v3/{plan.id}/lines/{line.id}/issue",
            json={"issue_type": "other"},
        )
        assert resp.status_code in (400, 422)


# ── Resubmit ─────────────────────────────────────────────────────────


class TestResubmitEndpoint:
    def test_resubmit(self, db_session: Session, test_quote: Quote, test_user: User):
        plan, _, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        # Stays in draft (simulating manager rejection)
        c = _make_client(db_session, test_user)
        r = c.post(
            f"/api/buy-plans-v3/{plan.id}/resubmit",
            json={"sales_order_number": "SO-FIXED"},
        )
        assert r.status_code == 200
        assert r.json()["ok"] is True


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
        r = c.get(f"/api/buy-plans-v3/{plan.id}/offers/{req.id}")
        assert r.status_code == 200
        data = r.json()
        assert data["requirement_id"] == req.id
        assert len(data["offers"]) == 2
        assert offer.id in data["selected_offer_ids"]


# ── Verification Group ───────────────────────────────────────────────


class TestVerificationGroup:
    def test_list_empty(self, db_session: Session, test_user: User):
        c = _make_client(db_session, test_user)
        r = c.get("/api/buy-plans-v3/verification-group")
        assert r.status_code == 200
        assert r.json()["items"] == []

    def test_add_member(self, db_session: Session, admin_user: User, test_user: User):
        c = _make_client(db_session, admin_user)
        r = c.post(
            "/api/buy-plans-v3/verification-group",
            json={"user_id": test_user.id, "action": "add"},
        )
        assert r.status_code == 200
        assert r.json()["action"] == "added"

        # Verify it appears in list
        r2 = c.get("/api/buy-plans-v3/verification-group")
        assert len(r2.json()["items"]) == 1

    def test_remove_member(self, db_session: Session, admin_user: User, test_user: User):
        _make_ops_member(db_session, test_user)
        c = _make_client(db_session, admin_user)
        r = c.post(
            "/api/buy-plans-v3/verification-group",
            json={"user_id": test_user.id, "action": "remove"},
        )
        assert r.status_code == 200
        assert r.json()["action"] == "removed"

    def test_non_admin_rejected(self, db_session: Session, test_user: User):
        c = _make_client(db_session, test_user)
        r = c.post(
            "/api/buy-plans-v3/verification-group",
            json={"user_id": test_user.id, "action": "add"},
        )
        assert r.status_code == 403


# ── Intelligence: AI Flags ──────────────────────────────────────────


class TestEnhancedAIFlags:
    def test_stale_offer_flag(self, db_session: Session, test_quote: Quote, test_user: User):
        """Offers older than threshold trigger a stale_offer flag."""
        from app.services.buy_plan_v3_service import generate_ai_flags

        req = db_session.query(Requirement).filter_by(requisition_id=test_quote.requisition_id).first()
        old_offer = _make_offer(
            db_session,
            test_quote.requisition_id,
            req.id,
            created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        )
        plan = BuyPlanV3(
            quote_id=test_quote.id,
            requisition_id=test_quote.requisition_id,
            status=BuyPlanStatus.draft.value,
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
            status=BuyPlanLineStatus.awaiting_po.value,
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
        from app.services.buy_plan_v3_service import generate_ai_flags

        req = db_session.query(Requirement).filter_by(requisition_id=test_quote.requisition_id).first()
        offer = _make_offer(db_session, test_quote.requisition_id, req.id)
        plan = BuyPlanV3(
            quote_id=test_quote.id,
            requisition_id=test_quote.requisition_id,
            status=BuyPlanStatus.draft.value,
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
            status=BuyPlanLineStatus.awaiting_po.value,
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
        from app.services.buy_plan_v3_service import generate_ai_flags

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
        plan = BuyPlanV3(
            quote_id=test_quote.id,
            requisition_id=test_quote.requisition_id,
            status=BuyPlanStatus.draft.value,
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
            status=BuyPlanLineStatus.awaiting_po.value,
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
        from app.services.buy_plan_v3_service import generate_ai_flags

        req = db_session.query(Requirement).filter_by(requisition_id=test_quote.requisition_id).first()
        req.target_qty = 1000
        db_session.flush()

        offer = _make_offer(db_session, test_quote.requisition_id, req.id)
        plan = BuyPlanV3(
            quote_id=test_quote.id,
            requisition_id=test_quote.requisition_id,
            status=BuyPlanStatus.draft.value,
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
            status=BuyPlanLineStatus.awaiting_po.value,
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
        from app.services.buy_plan_v3_service import detect_favoritism

        req = db_session.query(Requirement).filter_by(requisition_id=test_quote.requisition_id).first()
        offer = _make_offer(db_session, test_quote.requisition_id, req.id)

        # Create 3 plans all assigned to test_user (same buyer)
        for i in range(3):
            plan = BuyPlanV3(
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
                status=BuyPlanLineStatus.awaiting_po.value,
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
        from app.services.buy_plan_v3_service import detect_favoritism

        findings = detect_favoritism(admin_user.id, db_session)
        assert findings == []

    def test_favoritism_endpoint(
        self,
        db_session: Session,
        test_user: User,
        admin_user: User,
    ):
        """GET /api/buy-plans-v3/favoritism/{user_id} works for admins."""
        c = _make_client(db_session, admin_user)
        r = c.get(f"/api/buy-plans-v3/favoritism/{test_user.id}")
        assert r.status_code == 200
        assert "findings" in r.json()

    def test_favoritism_non_admin_rejected(
        self,
        db_session: Session,
        test_user: User,
    ):
        """Non-admin/manager users cannot access favoritism report."""
        c = _make_client(db_session, test_user)
        r = c.get(f"/api/buy-plans-v3/favoritism/{test_user.id}")
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
        from app.services.buy_plan_v3_service import check_completion

        req = db_session.query(Requirement).filter_by(requisition_id=test_quote.requisition_id).first()
        offer = _make_offer(db_session, test_quote.requisition_id, req.id)
        plan = BuyPlanV3(
            quote_id=test_quote.id,
            requisition_id=test_quote.requisition_id,
            status=BuyPlanStatus.active.value,
            so_status=SOVerificationStatus.approved.value,
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
            status=BuyPlanLineStatus.verified.value,
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
        """POST /api/buy-plans-v3/{id}/case-report regenerates the report."""
        req = db_session.query(Requirement).filter_by(requisition_id=test_quote.requisition_id).first()
        offer = _make_offer(db_session, test_quote.requisition_id, req.id)
        plan = BuyPlanV3(
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
        r = c.post(f"/api/buy-plans-v3/{plan.id}/case-report")
        assert r.status_code == 200
        assert "CASE REPORT" in r.json()["case_report"]

    def test_case_report_not_completed_rejected(
        self,
        db_session: Session,
        test_quote: Quote,
        test_user: User,
    ):
        """Case report endpoint rejects non-completed plans."""
        plan = BuyPlanV3(
            quote_id=test_quote.id,
            requisition_id=test_quote.requisition_id,
            status="active",
        )
        db_session.add(plan)
        db_session.commit()

        c = _make_client(db_session, test_user)
        r = c.post(f"/api/buy-plans-v3/{plan.id}/case-report")
        assert r.status_code == 400

    def test_case_report_not_found(
        self,
        db_session: Session,
        test_user: User,
    ):
        """Case report for nonexistent plan → 404 (line 307)."""
        c = _make_client(db_session, test_user)
        r = c.post("/api/buy-plans-v3/99999/case-report")
        assert r.status_code == 404


# ── Verification Group Edge Cases ──────────────────────────────────


class TestVerificationGroupEdgeCases:
    """Cover lines 244 (user not found) and 253 (reactivate existing member)."""

    def test_add_nonexistent_user(self, db_session: Session, admin_user: User):
        """Add user_id that doesn't exist → 404 (line 244)."""
        c = _make_client(db_session, admin_user)
        r = c.post(
            "/api/buy-plans-v3/verification-group",
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
            "/api/buy-plans-v3/verification-group",
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
        r = c.get("/api/buy-plans-v3/favoritism/99999")
        assert r.status_code == 404


# ── List V3 Filter Edge Cases ──────────────────────────────────────


class TestListV3Filters:
    """Cover lines 366, 368, 374: status, so_status, buyer_id filters."""

    def test_filter_by_so_status(
        self,
        db_session: Session,
        test_quote: Quote,
        test_user: User,
    ):
        """Filter by so_status (line 366)."""
        plan, _, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.so_status = "approved"
        db_session.commit()

        c = _make_client(db_session, test_user)
        r = c.get("/api/buy-plans-v3?so_status=approved")
        assert r.status_code == 200
        assert r.json()["count"] >= 1

    def test_filter_by_buyer_id(
        self,
        db_session: Session,
        test_quote: Quote,
        test_user: User,
    ):
        """Filter by buyer_id (line 368)."""
        plan, line, _, _ = _make_draft_plan(db_session, test_quote, test_user)

        c = _make_client(db_session, test_user)
        r = c.get(f"/api/buy-plans-v3?buyer_id={test_user.id}")
        assert r.status_code == 200
        assert r.json()["count"] >= 1

    def test_sales_user_sees_own(
        self,
        db_session: Session,
        test_quote: Quote,
        test_user: User,
        sales_user,
    ):
        """Sales user only sees own plans (line 374)."""
        plan, _, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.submitted_by_id = test_user.id
        db_session.commit()

        # Sales user with different id should see 0 plans
        sales = sales_user
        c = _make_client(db_session, sales)
        r = c.get("/api/buy-plans-v3")
        assert r.status_code == 200
        assert r.json()["count"] == 0


# ── Submit V3 Edge Cases ──────────────────────────────────────────


class TestSubmitV3EdgeCases:
    """Cover lines 419, 428-429: line_edits, ValueError."""

    def test_submit_with_line_edits(
        self,
        db_session: Session,
        test_quote: Quote,
        test_user: User,
    ):
        """Submit with line_edits provided (line 419)."""
        plan, line, offer, req = _make_draft_plan(db_session, test_quote, test_user)
        c = _make_client(db_session, test_user)
        r = c.post(
            f"/api/buy-plans-v3/{plan.id}/submit",
            json={
                "sales_order_number": "SO-EDIT",
                "line_edits": [
                    {
                        "requirement_id": req.id,
                        "offer_id": offer.id,
                        "quantity": 500,
                    }
                ],
            },
        )
        assert r.status_code == 200
        assert r.json()["ok"] is True

    def test_submit_value_error(
        self,
        db_session: Session,
        test_quote: Quote,
        test_user: User,
    ):
        """Submit on non-draft plan → ValueError → 400 (lines 428-429)."""
        plan, _, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.active.value
        db_session.commit()

        c = _make_client(db_session, test_user)
        r = c.post(
            f"/api/buy-plans-v3/{plan.id}/submit",
            json={"sales_order_number": "SO-FAIL"},
        )
        assert r.status_code == 400


# ── Approve V3 Edge Cases ──────────────────────────────────────────


class TestApproveV3EdgeCases:
    """Cover lines 457, 464-465: line_overrides, ValueError."""

    def test_approve_with_line_overrides(
        self,
        db_session: Session,
        test_quote: Quote,
        test_user: User,
        manager_user: User,
    ):
        """Approve with line_overrides (line 457)."""
        plan, line, offer, req = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.pending.value
        db_session.commit()

        c = _make_client(db_session, manager_user)
        r = c.post(
            f"/api/buy-plans-v3/{plan.id}/approve",
            json={
                "action": "approve",
                "line_overrides": [
                    {
                        "line_id": line.id,
                        "quantity": 750,
                        "manager_note": "Adjusted quantity",
                    }
                ],
            },
        )
        assert r.status_code == 200
        assert r.json()["status"] == "active"

    def test_approve_value_error(
        self,
        db_session: Session,
        test_quote: Quote,
        test_user: User,
        manager_user: User,
    ):
        """Approve non-pending plan → ValueError → 400 (lines 464-465)."""
        plan, _, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        # Status is draft, not pending
        c = _make_client(db_session, manager_user)
        r = c.post(
            f"/api/buy-plans-v3/{plan.id}/approve",
            json={"action": "approve"},
        )
        assert r.status_code == 400


# ── Resubmit V3 Edge Cases ────────────────────────────────────────


class TestResubmitV3EdgeCases:
    """Cover lines 492-493, 499: ValueError, auto_approved=False."""

    def test_resubmit_value_error(
        self,
        db_session: Session,
        test_quote: Quote,
        test_user: User,
    ):
        """Resubmit non-draft plan → ValueError → 400 (lines 492-493)."""
        plan, _, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.active.value
        db_session.commit()

        c = _make_client(db_session, test_user)
        r = c.post(
            f"/api/buy-plans-v3/{plan.id}/resubmit",
            json={"sales_order_number": "SO-FAIL"},
        )
        assert r.status_code == 400

    def test_resubmit_needs_approval(
        self,
        db_session: Session,
        test_quote: Quote,
        test_user: User,
    ):
        """Resubmit where cost > threshold → auto_approved=False (line 499)."""
        plan, _, _, _ = _make_draft_plan(
            db_session,
            test_quote,
            test_user,
            total_cost=10000.0,
        )
        # Plan stays in draft for resubmit
        c = _make_client(db_session, test_user)
        r = c.post(
            f"/api/buy-plans-v3/{plan.id}/resubmit",
            json={"sales_order_number": "SO-BIG-RESUB"},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["auto_approved"] is False
        assert data["status"] == "pending"


# ── Verify SO V3 Edge Cases ───────────────────────────────────────


class TestVerifySOV3EdgeCases:
    """Cover line 521: ValueError handling."""

    def test_verify_so_value_error(
        self,
        db_session: Session,
        test_quote: Quote,
        test_user: User,
        admin_user: User,
    ):
        """Verify SO on non-active plan → ValueError → 400 (line 521)."""
        plan, _, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        # Plan is draft, not active — but SO is "pending", so the service
        # may raise for plan not in correct status. Let's use already-verified SO.
        plan.status = BuyPlanStatus.active.value
        plan.so_status = "approved"  # Already verified
        _make_ops_member(db_session, admin_user)

        c = _make_client(db_session, admin_user)
        r = c.post(
            f"/api/buy-plans-v3/{plan.id}/verify-so",
            json={"action": "approve"},
        )
        assert r.status_code == 400


# ── Verify PO V3 Edge Cases ───────────────────────────────────────


class TestVerifyPOV3EdgeCases:
    """Cover lines 577-580, 586-587: ValueError/PermissionError, auto-complete."""

    def test_verify_po_value_error(
        self,
        db_session: Session,
        test_quote: Quote,
        test_user: User,
        admin_user: User,
    ):
        """Verify PO on line not in pending_verify → ValueError → 400 (lines 577-578)."""
        plan, line, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.active.value
        line.status = BuyPlanLineStatus.awaiting_po.value  # not pending_verify
        _make_ops_member(db_session, admin_user)

        c = _make_client(db_session, admin_user)
        r = c.post(
            f"/api/buy-plans-v3/{plan.id}/lines/{line.id}/verify-po",
            json={"action": "approve"},
        )
        assert r.status_code == 400

    def test_verify_po_permission_error(
        self,
        db_session: Session,
        test_quote: Quote,
        test_user: User,
    ):
        """Verify PO by non-ops user → PermissionError → 403 (lines 579-580)."""
        plan, line, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.active.value
        line.status = BuyPlanLineStatus.pending_verify.value
        line.po_number = "PO-001"
        db_session.commit()
        # No ops member for test_user

        c = _make_client(db_session, test_user)
        r = c.post(
            f"/api/buy-plans-v3/{plan.id}/lines/{line.id}/verify-po",
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
        """Verify last PO → auto-complete plan (lines 586-587)."""
        plan, line, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.active.value
        plan.so_status = SOVerificationStatus.approved.value
        plan.submitted_by_id = test_user.id
        plan.submitted_at = datetime.now(timezone.utc)
        plan.sales_order_number = "SO-COMPLETE"
        plan.total_cost = 500.0
        plan.total_revenue = 750.0
        line.status = BuyPlanLineStatus.pending_verify.value
        line.po_number = "PO-DONE"
        _make_ops_member(db_session, admin_user)

        c = _make_client(db_session, admin_user)
        r = c.post(
            f"/api/buy-plans-v3/{plan.id}/lines/{line.id}/verify-po",
            json={"action": "approve"},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "verified"
        # Plan should auto-complete
        db_session.refresh(plan)
        assert plan.status == "completed"


# ── Flag Issue V3 Edge Cases ──────────────────────────────────────


class TestFlagIssueV3EdgeCases:
    """Cover lines 608-609: ValueError handling."""

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
            f"/api/buy-plans-v3/{plan.id}/lines/{line.id}/issue",
            json={"issue_type": "sold_out"},
        )
        assert r.status_code == 400


# ── Offer Comparison Edge Cases ───────────────────────────────────


class TestOfferComparisonEdgeCases:
    """Cover lines 630, 634: plan/requirement not found."""

    def test_offer_comparison_plan_not_found(
        self,
        db_session: Session,
        test_user: User,
    ):
        """Offer comparison for nonexistent plan → 404 (line 630)."""
        c = _make_client(db_session, test_user)
        r = c.get("/api/buy-plans-v3/99999/offers/1")
        assert r.status_code == 404

    def test_offer_comparison_requirement_not_found(
        self,
        db_session: Session,
        test_quote: Quote,
        test_user: User,
    ):
        """Offer comparison for nonexistent requirement → 404 (line 634)."""
        plan, _, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        c = _make_client(db_session, test_user)
        r = c.get(f"/api/buy-plans-v3/{plan.id}/offers/99999")
        assert r.status_code == 404
