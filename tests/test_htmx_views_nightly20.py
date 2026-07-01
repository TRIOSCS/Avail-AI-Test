"""tests/test_htmx_views_nightly20.py — Coverage for buy plan workflow routes.

Targets:
  - buy_plan_submit_partial (POST)
  - buy_plan_approve_partial (POST, 403 path)
  - buy_plan_confirm_po_partial (POST, missing PO#)
  - buy_plan_verify_po_partial (POST, not-found path)
  - buy_plan_flag_issue_partial (POST)
  - buy_plan_detail_partial (GET, not found)

Called by: pytest autodiscovery
Depends on: conftest.py fixtures, app.routers.htmx_views
"""

import os

os.environ["TESTING"] = "1"

import uuid

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.constants import BuyPlanLineStatus, BuyPlanStatus, SOVerificationStatus
from app.models import Requisition
from app.models.buy_plan import BuyPlan, BuyPlanLine
from app.models.quotes import Quote

# ── Helpers ───────────────────────────────────────────────────────────────


def _make_buy_plan(db: Session, req: Requisition, **kw) -> BuyPlan:
    quote = Quote(
        requisition_id=req.id,
        quote_number=f"Q-{uuid.uuid4().hex[:8]}",
        status="draft",
    )
    db.add(quote)
    db.flush()

    defaults = dict(
        quote_id=quote.id,
        requisition_id=req.id,
        status=BuyPlanStatus.DRAFT,
        so_status=SOVerificationStatus.PENDING,
    )
    defaults.update(kw)
    bp = BuyPlan(**defaults)
    db.add(bp)
    db.commit()
    db.refresh(bp)
    return bp


def _make_line(db: Session, buy_plan: BuyPlan, **kw) -> BuyPlanLine:
    defaults = dict(
        buy_plan_id=buy_plan.id,
        quantity=10,
        status=BuyPlanLineStatus.AWAITING_PO,
    )
    defaults.update(kw)
    line = BuyPlanLine(**defaults)
    db.add(line)
    db.commit()
    db.refresh(line)
    return line


# ── Buy Plan Detail ───────────────────────────────────────────────────────


class TestBuyPlanDetailNotFound:
    def test_detail_not_found(self, client: TestClient):
        resp = client.get("/v2/partials/buy-plans/99999")
        assert resp.status_code == 404


# ── Buy Plan Submit ───────────────────────────────────────────────────────


class TestBuyPlanSubmit:
    def test_submit_no_so_number(self, client: TestClient, db_session: Session, test_requisition: Requisition):
        bp = _make_buy_plan(db_session, test_requisition)
        resp = client.post(
            f"/v2/partials/buy-plans/{bp.id}/submit",
            data={"sales_order_number": ""},
        )
        assert resp.status_code == 400

    def test_submit_success(self, client: TestClient, db_session: Session, test_requisition: Requisition):
        bp = _make_buy_plan(db_session, test_requisition)
        resp = client.post(
            f"/v2/partials/buy-plans/{bp.id}/submit",
            data={"sales_order_number": "SO-12345"},
        )
        assert resp.status_code == 200
        db_session.refresh(bp)
        assert bp.sales_order_number == "SO-12345"

    def test_submit_plan_not_in_draft(self, client: TestClient, db_session: Session, test_requisition: Requisition):
        bp = _make_buy_plan(db_session, test_requisition, status=BuyPlanStatus.PENDING)
        resp = client.post(
            f"/v2/partials/buy-plans/{bp.id}/submit",
            data={"sales_order_number": "SO-99999"},
        )
        assert resp.status_code == 400


# ── Buy Plan Approve ──────────────────────────────────────────────────────


class TestBuyPlanApprove:
    def test_approve_requires_approval_right(
        self, client: TestClient, db_session: Session, test_user, test_requisition: Requisition, monkeypatch
    ):
        """A user without the can_approve_buy_plans right cannot approve — 403 from the
        require_buyplan_approver dependency (role is irrelevant)."""
        bp = _make_buy_plan(db_session, test_requisition, status=BuyPlanStatus.PENDING)
        db_session.commit()
        # The REAL require_buyplan_approver runs against test_user (no right) → 403.
        monkeypatch.setattr("app.dependencies.require_user", lambda request, db: test_user)
        resp = client.post(
            f"/v2/partials/buy-plans/{bp.id}/approve",
            data={"action": "approve"},
        )
        assert resp.status_code == 403


# Phase D removed the separate verify-SO route (folded into the single approval); its
# replacement, the standalone halt route, is covered in test_htmx_views_nightly7.py.


# ── Buy Plan Confirm PO ───────────────────────────────────────────────────


class TestBuyPlanConfirmPo:
    def test_confirm_po_missing_po_number(self, client: TestClient, db_session: Session, test_requisition: Requisition):
        bp = _make_buy_plan(db_session, test_requisition)
        line = _make_line(db_session, bp)
        resp = client.post(
            f"/v2/partials/buy-plans/{bp.id}/lines/{line.id}/confirm-po",
            data={"po_number": ""},
        )
        assert resp.status_code == 400

    def test_confirm_po_plan_not_found(self, client: TestClient):
        # Phase 1 authz: the route resolves the plan via get_buyplan_for_user BEFORE any
        # mutation. A non-existent plan id 404s at the ownership gate (the canonical
        # not-found response) — it no longer falls through to the service's ValueError→400.
        resp = client.post(
            "/v2/partials/buy-plans/99999/lines/1/confirm-po",
            data={"po_number": "PO-001"},
        )
        assert resp.status_code == 404


# ── Buy Plan Verify PO ────────────────────────────────────────────────────


class TestBuyPlanVerifyPo:
    def test_verify_po_line_not_found(self, client: TestClient, db_session: Session, test_user, test_requisition):
        from app.dependencies import require_buyplan_po_approver
        from app.main import app

        bp = _make_buy_plan(db_session, test_requisition)
        # Phase D: the route is gated by require_buyplan_po_approver — override it so the
        # handler runs and the service raises the line-not-found ValueError (→ 400).
        app.dependency_overrides[require_buyplan_po_approver] = lambda: test_user
        try:
            resp = client.post(
                f"/v2/partials/buy-plans/{bp.id}/lines/99999/verify-po",
                data={"action": "approve"},
            )
        finally:
            app.dependency_overrides.pop(require_buyplan_po_approver, None)
        assert resp.status_code == 400


# ── Buy Plan Flag Issue ───────────────────────────────────────────────────


class TestBuyPlanFlagIssue:
    def test_flag_issue_line_not_found(self, client: TestClient, db_session: Session, test_requisition: Requisition):
        bp = _make_buy_plan(db_session, test_requisition)
        resp = client.post(
            f"/v2/partials/buy-plans/{bp.id}/lines/99999/issue",
            data={"issue_type": "out_of_stock", "note": "Vendor ran out"},
        )
        assert resp.status_code == 400
