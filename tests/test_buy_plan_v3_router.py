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
from app.models import Offer, Quote, Requirement, Requisition, User
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
    req = db.query(Requirement).filter_by(
        requisition_id=test_quote.requisition_id
    ).first()
    offer = _make_offer(
        db, test_quote.requisition_id, req.id,
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
    def test_build_creates_draft(
        self, db_session: Session, test_quote: Quote, test_user: User
    ):
        """POST build → returns draft plan with lines."""
        req = db_session.query(Requirement).filter_by(
            requisition_id=test_quote.requisition_id
        ).first()
        _make_offer(
            db_session, test_quote.requisition_id, req.id,
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
    def test_get_plan(
        self, db_session: Session, test_quote: Quote, test_user: User
    ):
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

    def test_list_all(
        self, db_session: Session, test_quote: Quote, test_user: User
    ):
        _make_draft_plan(db_session, test_quote, test_user)
        c = _make_client(db_session, test_user)
        r = c.get("/api/buy-plans-v3")
        assert r.status_code == 200
        data = r.json()
        assert data["count"] >= 1
        assert len(data["items"]) >= 1

    def test_list_filter_status(
        self, db_session: Session, test_quote: Quote, test_user: User
    ):
        plan, _, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        c = _make_client(db_session, test_user)
        r = c.get("/api/buy-plans-v3?status=draft")
        assert r.status_code == 200
        assert r.json()["count"] >= 1

        r2 = c.get("/api/buy-plans-v3?status=completed")
        assert r2.json()["count"] == 0


# ── Submit ───────────────────────────────────────────────────────────


class TestSubmitEndpoint:
    def test_submit_auto_approve(
        self, db_session: Session, test_quote: Quote, test_user: User
    ):
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

    def test_submit_needs_approval(
        self, db_session: Session, test_quote: Quote, test_user: User
    ):
        plan, _, _, _ = _make_draft_plan(
            db_session, test_quote, test_user, total_cost=10000.0,
        )
        c = _make_client(db_session, test_user)
        r = c.post(
            f"/api/buy-plans-v3/{plan.id}/submit",
            json={"sales_order_number": "SO-BIG"},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "pending"

    def test_submit_blank_so_rejected(
        self, db_session: Session, test_quote: Quote, test_user: User
    ):
        """Blank SO# rejected by Pydantic (schema validation tested in test_buy_plan_schemas.py)."""
        plan, _, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        c = _make_client(db_session, test_user)
        with pytest.raises(Exception):
            c.post(
                f"/api/buy-plans-v3/{plan.id}/submit",
                json={"sales_order_number": ""},
            )


# ── Approve ──────────────────────────────────────────────────────────


class TestApproveEndpoint:
    def test_approve(
        self, db_session: Session, test_quote: Quote,
        test_user: User, manager_user: User,
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
        self, db_session: Session, test_quote: Quote,
        test_user: User, manager_user: User,
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

    def test_non_manager_rejected(
        self, db_session: Session, test_quote: Quote, test_user: User
    ):
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
        self, db_session: Session, test_quote: Quote,
        test_user: User, admin_user: User,
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
        self, db_session: Session, test_quote: Quote,
        test_user: User, admin_user: User,
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
        self, db_session: Session, test_quote: Quote,
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
    def test_confirm_po(
        self, db_session: Session, test_quote: Quote, test_user: User
    ):
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

    def test_confirm_po_wrong_status(
        self, db_session: Session, test_quote: Quote, test_user: User
    ):
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
        self, db_session: Session, test_quote: Quote,
        test_user: User, admin_user: User,
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
        self, db_session: Session, test_quote: Quote,
        test_user: User, admin_user: User,
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
    def test_flag_sold_out(
        self, db_session: Session, test_quote: Quote, test_user: User
    ):
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

    def test_other_requires_note(
        self, db_session: Session, test_quote: Quote, test_user: User
    ):
        """'other' without note rejected by Pydantic (schema validation in test_buy_plan_schemas.py)."""
        plan, line, _, _ = _make_draft_plan(db_session, test_quote, test_user)
        plan.status = BuyPlanStatus.active.value
        db_session.commit()

        c = _make_client(db_session, test_user)
        with pytest.raises(Exception):
            c.post(
                f"/api/buy-plans-v3/{plan.id}/lines/{line.id}/issue",
                json={"issue_type": "other"},
            )


# ── Resubmit ─────────────────────────────────────────────────────────


class TestResubmitEndpoint:
    def test_resubmit(
        self, db_session: Session, test_quote: Quote, test_user: User
    ):
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
    def test_offer_comparison(
        self, db_session: Session, test_quote: Quote, test_user: User
    ):
        plan, line, offer, req = _make_draft_plan(db_session, test_quote, test_user)
        # Add a second offer for same requirement
        _make_offer(
            db_session, test_quote.requisition_id, req.id,
            vendor_name="Digi-Key", unit_price=0.55,
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

    def test_remove_member(
        self, db_session: Session, admin_user: User, test_user: User
    ):
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
