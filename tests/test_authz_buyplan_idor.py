"""tests/test_authz_buyplan_idor.py — Buy-plan per-record ownership (IDOR) guard.

Phase 1d: the buy-plan action routes in app/routers/htmx_views.py must enforce
per-record ownership via app.dependencies.get_buyplan_for_user, which scopes
RESTRICTED_ROLES (SALES, TRADER) to plans whose parent Requisition.created_by is
the caller. A non-owner SALES/TRADER must get 404 BEFORE any mutation; the owner
(the requisition creator) and supervisors (manager/admin) must pass.

Routes covered: detail (GET), submit, confirm-po, flag-issue, cancel, reset.

Called by: pytest
Depends on: conftest.py fixtures (client, db_session, test_user, sales_user,
            trader_user, manager_user, admin_user, test_quote), the buy-plan
            workflow service, app.routers.htmx_views, app.dependencies.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.constants import BuyPlanLineStatus, BuyPlanStatus, SOVerificationStatus
from app.models.buy_plan import BuyPlan, BuyPlanLine
from app.models.quotes import Quote

# ── helpers (mirror tests/test_buyplan_hub_routes.py) ───────────────────────


def _make_quote(db: Session, req_id: int) -> Quote:
    q = Quote(
        requisition_id=req_id,
        quote_number=f"Q-{uuid.uuid4().hex[:8]}",
        status="draft",
    )
    db.add(q)
    db.flush()
    return q


def _make_plan(db: Session, *, quote_id: int, req_id: int, **kw) -> BuyPlan:
    defaults = dict(
        quote_id=quote_id,
        requisition_id=req_id,
        status=BuyPlanStatus.ACTIVE.value,
        so_status=SOVerificationStatus.APPROVED.value,
    )
    defaults.update(kw)
    plan = BuyPlan(**defaults)
    db.add(plan)
    db.flush()
    return plan


def _make_line(db: Session, *, plan_id: int, **kw) -> BuyPlanLine:
    defaults = dict(
        buy_plan_id=plan_id,
        quantity=10,
        status=BuyPlanLineStatus.AWAITING_PO.value,
    )
    defaults.update(kw)
    line = BuyPlanLine(**defaults)
    db.add(line)
    db.flush()
    return line


# The `test_quote` fixture hangs off `test_requisition`, which is created_by test_user.
# So test_user is the OWNER; sales_user / trader_user are NON-OWNERS (different ids).


# ── GET detail ──────────────────────────────────────────────────────────────


class TestBuyPlanDetailIDOR:
    @pytest.mark.parametrize("role_fixture", ["sales_user", "trader_user"])
    def test_non_owner_restricted_gets_404(
        self, client: TestClient, db_session: Session, test_quote, request, role_fixture
    ):
        non_owner = request.getfixturevalue(role_fixture)
        plan = _make_plan(db_session, quote_id=test_quote.id, req_id=test_quote.requisition_id)
        db_session.commit()

        from app.dependencies import require_user
        from app.main import app

        app.dependency_overrides[require_user] = lambda: non_owner
        try:
            resp = client.get(f"/v2/partials/buy-plans/{plan.id}")
        finally:
            app.dependency_overrides.pop(require_user, None)
        assert resp.status_code == 404

    def test_owner_gets_200(self, client: TestClient, db_session: Session, test_quote):
        plan = _make_plan(db_session, quote_id=test_quote.id, req_id=test_quote.requisition_id)
        db_session.commit()
        resp = client.get(f"/v2/partials/buy-plans/{plan.id}")
        assert resp.status_code == 200

    @pytest.mark.parametrize("role_fixture", ["manager_user", "admin_user"])
    def test_supervisor_gets_200(self, client: TestClient, db_session: Session, test_quote, request, role_fixture):
        supervisor = request.getfixturevalue(role_fixture)
        plan = _make_plan(db_session, quote_id=test_quote.id, req_id=test_quote.requisition_id)
        db_session.commit()

        from app.dependencies import require_user
        from app.main import app

        app.dependency_overrides[require_user] = lambda: supervisor
        try:
            resp = client.get(f"/v2/partials/buy-plans/{plan.id}")
        finally:
            app.dependency_overrides.pop(require_user, None)
        assert resp.status_code == 200


# ── POST submit ──────────────────────────────────────────────────────────────


class TestBuyPlanSubmitIDOR:
    @pytest.mark.parametrize("role_fixture", ["sales_user", "trader_user"])
    def test_non_owner_restricted_gets_404(
        self, client: TestClient, db_session: Session, test_quote, request, role_fixture
    ):
        non_owner = request.getfixturevalue(role_fixture)
        plan = _make_plan(
            db_session,
            quote_id=test_quote.id,
            req_id=test_quote.requisition_id,
            status=BuyPlanStatus.DRAFT.value,
        )
        db_session.commit()

        from app.dependencies import require_user
        from app.main import app

        app.dependency_overrides[require_user] = lambda: non_owner
        try:
            resp = client.post(
                f"/v2/partials/buy-plans/{plan.id}/submit",
                data={"sales_order_number": "SO-HACK"},
            )
        finally:
            app.dependency_overrides.pop(require_user, None)
        assert resp.status_code == 404
        # 404 must precede the mutation — SO# must NOT have been written.
        db_session.expire_all()
        assert db_session.get(BuyPlan, plan.id).sales_order_number is None

    def test_owner_gets_200(self, client: TestClient, db_session: Session, test_quote):
        plan = _make_plan(
            db_session,
            quote_id=test_quote.id,
            req_id=test_quote.requisition_id,
            status=BuyPlanStatus.DRAFT.value,
        )
        db_session.commit()
        resp = client.post(
            f"/v2/partials/buy-plans/{plan.id}/submit",
            data={"sales_order_number": "SO-OWNER-1"},
        )
        assert resp.status_code == 200

    def test_manager_passes(self, client: TestClient, db_session: Session, test_quote, manager_user):
        plan = _make_plan(
            db_session,
            quote_id=test_quote.id,
            req_id=test_quote.requisition_id,
            status=BuyPlanStatus.DRAFT.value,
        )
        db_session.commit()

        from app.dependencies import require_user
        from app.main import app

        app.dependency_overrides[require_user] = lambda: manager_user
        try:
            resp = client.post(
                f"/v2/partials/buy-plans/{plan.id}/submit",
                data={"sales_order_number": "SO-MGR-1"},
            )
        finally:
            app.dependency_overrides.pop(require_user, None)
        assert resp.status_code == 200


# ── POST confirm-po ──────────────────────────────────────────────────────────


class TestBuyPlanConfirmPoIDOR:
    @pytest.mark.parametrize("role_fixture", ["sales_user", "trader_user"])
    def test_non_owner_restricted_gets_404(
        self, client: TestClient, db_session: Session, test_quote, request, role_fixture
    ):
        non_owner = request.getfixturevalue(role_fixture)
        plan = _make_plan(db_session, quote_id=test_quote.id, req_id=test_quote.requisition_id)
        line = _make_line(db_session, plan_id=plan.id)
        db_session.commit()

        from app.dependencies import require_user
        from app.main import app

        app.dependency_overrides[require_user] = lambda: non_owner
        try:
            resp = client.post(
                f"/v2/partials/buy-plans/{plan.id}/lines/{line.id}/confirm-po",
                data={"po_number": "PO-HACK"},
            )
        finally:
            app.dependency_overrides.pop(require_user, None)
        assert resp.status_code == 404
        db_session.expire_all()
        assert db_session.get(BuyPlanLine, line.id).po_number is None

    def test_owner_gets_200(self, client: TestClient, db_session: Session, test_user, test_quote):
        plan = _make_plan(db_session, quote_id=test_quote.id, req_id=test_quote.requisition_id)
        line = _make_line(db_session, plan_id=plan.id, buyer_id=test_user.id)
        db_session.commit()
        resp = client.post(
            f"/v2/partials/buy-plans/{plan.id}/lines/{line.id}/confirm-po",
            data={"po_number": "PO-OWNER"},
        )
        assert resp.status_code == 200

    def test_admin_passes(self, client: TestClient, db_session: Session, test_quote, admin_user):
        plan = _make_plan(db_session, quote_id=test_quote.id, req_id=test_quote.requisition_id)
        line = _make_line(db_session, plan_id=plan.id)
        db_session.commit()

        from app.dependencies import require_user
        from app.main import app

        app.dependency_overrides[require_user] = lambda: admin_user
        try:
            resp = client.post(
                f"/v2/partials/buy-plans/{plan.id}/lines/{line.id}/confirm-po",
                data={"po_number": "PO-ADMIN"},
            )
        finally:
            app.dependency_overrides.pop(require_user, None)
        assert resp.status_code == 200


# ── POST flag-issue ──────────────────────────────────────────────────────────


class TestBuyPlanFlagIssueIDOR:
    @pytest.mark.parametrize("role_fixture", ["sales_user", "trader_user"])
    def test_non_owner_restricted_gets_404(
        self, client: TestClient, db_session: Session, test_quote, request, role_fixture
    ):
        non_owner = request.getfixturevalue(role_fixture)
        plan = _make_plan(db_session, quote_id=test_quote.id, req_id=test_quote.requisition_id)
        line = _make_line(db_session, plan_id=plan.id)
        db_session.commit()

        from app.dependencies import require_user
        from app.main import app

        app.dependency_overrides[require_user] = lambda: non_owner
        try:
            resp = client.post(
                f"/v2/partials/buy-plans/{plan.id}/lines/{line.id}/issue",
                data={"issue_type": "other", "note": "hack"},
            )
        finally:
            app.dependency_overrides.pop(require_user, None)
        assert resp.status_code == 404
        db_session.expire_all()
        assert db_session.get(BuyPlanLine, line.id).status == BuyPlanLineStatus.AWAITING_PO.value

    def test_owner_gets_200(self, client: TestClient, db_session: Session, test_quote):
        plan = _make_plan(db_session, quote_id=test_quote.id, req_id=test_quote.requisition_id)
        line = _make_line(db_session, plan_id=plan.id)
        db_session.commit()
        resp = client.post(
            f"/v2/partials/buy-plans/{plan.id}/lines/{line.id}/issue",
            data={"issue_type": "other", "note": "real issue"},
        )
        assert resp.status_code == 200

    def test_manager_passes(self, client: TestClient, db_session: Session, test_quote, manager_user):
        plan = _make_plan(db_session, quote_id=test_quote.id, req_id=test_quote.requisition_id)
        line = _make_line(db_session, plan_id=plan.id)
        db_session.commit()

        from app.dependencies import require_user
        from app.main import app

        app.dependency_overrides[require_user] = lambda: manager_user
        try:
            resp = client.post(
                f"/v2/partials/buy-plans/{plan.id}/lines/{line.id}/issue",
                data={"issue_type": "other"},
            )
        finally:
            app.dependency_overrides.pop(require_user, None)
        assert resp.status_code == 200


# ── POST cancel ──────────────────────────────────────────────────────────────


class TestBuyPlanCancelIDOR:
    @pytest.mark.parametrize("role_fixture", ["sales_user", "trader_user"])
    def test_non_owner_restricted_gets_404(
        self, client: TestClient, db_session: Session, test_quote, request, role_fixture
    ):
        non_owner = request.getfixturevalue(role_fixture)
        plan = _make_plan(db_session, quote_id=test_quote.id, req_id=test_quote.requisition_id)
        db_session.commit()

        from app.dependencies import require_user
        from app.main import app

        app.dependency_overrides[require_user] = lambda: non_owner
        try:
            resp = client.post(
                f"/v2/partials/buy-plans/{plan.id}/cancel",
                data={"reason": "hack"},
            )
        finally:
            app.dependency_overrides.pop(require_user, None)
        assert resp.status_code == 404
        db_session.expire_all()
        assert db_session.get(BuyPlan, plan.id).status == BuyPlanStatus.ACTIVE.value

    def test_owner_gets_200(self, client: TestClient, db_session: Session, test_quote):
        plan = _make_plan(db_session, quote_id=test_quote.id, req_id=test_quote.requisition_id)
        db_session.commit()
        resp = client.post(f"/v2/partials/buy-plans/{plan.id}/cancel", data={"reason": "legit"})
        assert resp.status_code == 200

    def test_admin_passes(self, client: TestClient, db_session: Session, test_quote, admin_user):
        plan = _make_plan(db_session, quote_id=test_quote.id, req_id=test_quote.requisition_id)
        db_session.commit()

        from app.dependencies import require_user
        from app.main import app

        app.dependency_overrides[require_user] = lambda: admin_user
        try:
            resp = client.post(f"/v2/partials/buy-plans/{plan.id}/cancel", data={"reason": "legit"})
        finally:
            app.dependency_overrides.pop(require_user, None)
        assert resp.status_code == 200


# ── POST reset ───────────────────────────────────────────────────────────────


class TestBuyPlanResetIDOR:
    @pytest.mark.parametrize("role_fixture", ["sales_user", "trader_user"])
    def test_non_owner_restricted_gets_404(
        self, client: TestClient, db_session: Session, test_quote, request, role_fixture
    ):
        non_owner = request.getfixturevalue(role_fixture)
        plan = _make_plan(
            db_session,
            quote_id=test_quote.id,
            req_id=test_quote.requisition_id,
            status=BuyPlanStatus.CANCELLED.value,
        )
        db_session.commit()

        from app.dependencies import require_user
        from app.main import app

        app.dependency_overrides[require_user] = lambda: non_owner
        try:
            resp = client.post(f"/v2/partials/buy-plans/{plan.id}/reset")
        finally:
            app.dependency_overrides.pop(require_user, None)
        assert resp.status_code == 404
        db_session.expire_all()
        assert db_session.get(BuyPlan, plan.id).status == BuyPlanStatus.CANCELLED.value

    def test_owner_gets_200(self, client: TestClient, db_session: Session, test_quote):
        plan = _make_plan(
            db_session,
            quote_id=test_quote.id,
            req_id=test_quote.requisition_id,
            status=BuyPlanStatus.CANCELLED.value,
        )
        db_session.commit()
        resp = client.post(f"/v2/partials/buy-plans/{plan.id}/reset")
        assert resp.status_code == 200

    def test_manager_passes(self, client: TestClient, db_session: Session, test_quote, manager_user):
        plan = _make_plan(
            db_session,
            quote_id=test_quote.id,
            req_id=test_quote.requisition_id,
            status=BuyPlanStatus.HALTED.value,
        )
        db_session.commit()

        from app.dependencies import require_user
        from app.main import app

        app.dependency_overrides[require_user] = lambda: manager_user
        try:
            resp = client.post(f"/v2/partials/buy-plans/{plan.id}/reset")
        finally:
            app.dependency_overrides.pop(require_user, None)
        assert resp.status_code == 200
