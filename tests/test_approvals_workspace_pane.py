"""test_approvals_workspace_pane.py — the SO/BP detail pane (Approvals Workspace 1.2).

Covers the one-anatomy pane (spec §8): header (SO# copy chip · order-type badge ·
status), the approval block in each state (awaiting-your-approval with inline
Approve/Reject, the approved-by stamp, draft), the Quality — sales section, the lines
table rendered in the PO display vocabulary, and the origin=approvals_workspace decide
branch (re-renders the pane + fires awListRefresh; engine decides, plan transitions).

Called by: pytest
Depends on: conftest (db_session, test_user), tests.test_approvals_hub_tabs builders,
            app.routers.htmx.{approvals_hub,buy_plans}.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.constants import BuyPlanStatus, SalesOrderType
from app.database import get_db
from app.dependencies import require_buyplan_approver, require_buyplan_po_approver, require_user
from app.models import User
from app.models.quality_plan import QualityPlan
from tests.test_approvals_hub_tabs import (
    _pending_buy_plan_request,
    _pending_verify_line,
    _plan,
    _req_quote,
)


@pytest.fixture()
def hub_client(db_session: Session, test_user: User):
    """TestClient authed as test_user with both decide rights (mirrors the hub-tabs
    fixture — duplicated locally because importing a fixture trips ruff F811)."""
    from app.main import app

    test_user.can_approve_buy_plans = True
    test_user.can_approve_purchase_orders = True
    db_session.commit()

    app.dependency_overrides[get_db] = lambda: (yield db_session)  # type: ignore[misc]
    app.dependency_overrides[require_user] = lambda: test_user
    app.dependency_overrides[require_buyplan_approver] = lambda: test_user
    app.dependency_overrides[require_buyplan_po_approver] = lambda: test_user
    try:
        with TestClient(app) as c:
            yield c
    finally:
        for dep in (get_db, require_user, require_buyplan_approver, require_buyplan_po_approver):
            app.dependency_overrides.pop(dep, None)


# ── Rendering ────────────────────────────────────────────────────────────


def test_pane_header_has_copy_chip_type_badge_and_status(hub_client: TestClient, db_session: Session, test_user: User):
    req, q, _ = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value, order_type=SalesOrderType.STOCK_SALE.value)
    bp.sales_order_number = "SO-9001"
    db_session.commit()

    r = hub_client.get(f"/v2/partials/approvals/plan/{bp.id}/pane")
    assert r.status_code == 200
    body = r.text
    assert "AcmeCo" in body  # customer header
    assert 'data-copy-value="SO-9001"' in body  # one-tap Acctivate copy chip
    assert "Stock Sale" in body  # order-type badge
    assert f"Plan #{bp.id}" in body


def test_pane_awaiting_your_approval_block_when_decidable(hub_client: TestClient, db_session: Session, test_user: User):
    req, q, _ = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.PENDING.value)
    _pending_buy_plan_request(db_session, bp, test_user)
    db_session.commit()

    body = hub_client.get(f"/v2/partials/approvals/plan/{bp.id}/pane").text
    assert "Awaiting your approval" in body
    assert f"/v2/partials/buy-plans/{bp.id}/approve" in body  # decides via the EXISTING route
    assert 'value="approvals_workspace"' in body
    assert "Confirm reject" in body  # reject prompts for a note to the fixer


def test_pane_pending_but_not_decidable_shows_waiting(hub_client: TestClient, db_session: Session, test_user: User):
    req, q, _ = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.PENDING.value)
    db_session.commit()  # no open request → viewer cannot decide

    body = hub_client.get(f"/v2/partials/approvals/plan/{bp.id}/pane").text
    assert "Awaiting manager approval" in body
    assert "Awaiting your approval" not in body


def test_pane_approved_stamp(hub_client: TestClient, db_session: Session, test_user: User):
    req, q, _ = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    bp.approved_by_id = test_user.id
    bp.approved_at = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    db_session.commit()

    body = hub_client.get(f"/v2/partials/approvals/plan/{bp.id}/pane").text
    assert "Approved by Test Buyer" in body


def test_pane_qp_sales_section_renders_fields(hub_client: TestClient, db_session: Session, test_user: User):
    req, q, _ = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    db_session.add(
        QualityPlan(
            buy_plan_id=bp.id,
            created_by_id=test_user.id,
            sales_condition="NEW SEALED",
            sales_testing_required=True,
            sales_pkg_requirements="ESD trays only",
        )
    )
    db_session.commit()

    body = hub_client.get(f"/v2/partials/approvals/plan/{bp.id}/pane").text
    assert "Quality — sales section" in body
    assert "NEW SEALED" in body
    assert "ESD trays only" in body


def test_pane_without_qp_shows_empty_state(hub_client: TestClient, db_session: Session, test_user: User):
    req, q, _ = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.DRAFT.value)
    db_session.commit()

    body = hub_client.get(f"/v2/partials/approvals/plan/{bp.id}/pane").text
    assert "Quality — sales section" in body
    assert "No quality-plan data recorded yet." in body


def test_pane_lines_use_display_vocabulary(hub_client: TestClient, db_session: Session, test_user: User):
    req, q, rq = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    _pending_verify_line(db_session, bp, rq, test_user)
    db_session.commit()

    body = hub_client.get(f"/v2/partials/approvals/plan/{bp.id}/pane").text
    assert "Pending approval" in body  # spec §5 vocabulary
    assert "pending_verify" not in body
    assert 'data-copy-value="PO-9"' in body  # line PO# copy chip


def test_pane_lens_buy_plans_threads_lens(hub_client: TestClient, db_session: Session, test_user: User):
    req, q, _ = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.PENDING.value)
    _pending_buy_plan_request(db_session, bp, test_user)
    db_session.commit()

    body = hub_client.get(f"/v2/partials/approvals/plan/{bp.id}/pane?lens=buy-plans").text
    assert 'name="lens" value="buy-plans"' in body  # decide re-renders into the same lens


def test_pane_missing_plan_404s(hub_client: TestClient):
    assert hub_client.get("/v2/partials/approvals/plan/999999/pane").status_code == 404


# ── Decide from the pane (origin=approvals_workspace) ────────────────────


def test_approve_from_pane_rerenders_pane_and_refreshes_list(
    hub_client: TestClient, db_session: Session, test_user: User
):
    req, q, _ = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.PENDING.value)
    _pending_buy_plan_request(db_session, bp, test_user)
    db_session.commit()

    with patch("app.services.buyplan_notifications.run_notify_bg", new_callable=AsyncMock):
        r = hub_client.post(
            f"/v2/partials/buy-plans/{bp.id}/approve",
            data={"action": "approve", "origin": "approvals_workspace", "lens": "sales-orders"},
        )
    assert r.status_code == 200
    assert "Approved by Test Buyer" in r.text  # the refreshed PANE, not a tab body
    assert r.headers.get("HX-Trigger") == "awListRefresh"  # the left list repaints
    db_session.expire(bp)
    assert bp.status == BuyPlanStatus.ACTIVE.value


def test_reject_from_pane_returns_draft_pane(hub_client: TestClient, db_session: Session, test_user: User):
    req, q, _ = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.PENDING.value)
    _pending_buy_plan_request(db_session, bp, test_user)
    db_session.commit()

    with patch("app.services.buyplan_notifications.run_notify_bg", new_callable=AsyncMock):
        r = hub_client.post(
            f"/v2/partials/buy-plans/{bp.id}/approve",
            data={
                "action": "reject",
                "origin": "approvals_workspace",
                "lens": "buy-plans",
                "notes": "wrong sell price — fix line 1",
            },
        )
    assert r.status_code == 200
    assert "Draft — not yet submitted" in r.text  # reject → back to draft, pane re-rendered
    assert "wrong sell price" in r.text  # the note to the fixer surfaces on the pane
    db_session.expire(bp)
    assert bp.status == BuyPlanStatus.DRAFT.value


# ── Lifecycle controls (2.5) — manager-only, origin=approvals_workspace ──


def _manager(db_session: Session, user: User) -> None:
    from app.constants import UserRole

    user.role = UserRole.MANAGER.value
    db_session.commit()


def test_lifecycle_controls_manager_only(hub_client: TestClient, db_session: Session, test_user: User):
    req, q, _ = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    db_session.commit()

    body = hub_client.get(f"/v2/partials/approvals/plan/{bp.id}/pane").text
    assert "Plan controls" not in body  # buyer viewer → no lifecycle block

    _manager(db_session, test_user)
    body = hub_client.get(f"/v2/partials/approvals/plan/{bp.id}/pane").text
    assert "Plan controls" in body
    assert f"/v2/partials/buy-plans/{bp.id}/halt" in body
    assert f"/v2/partials/buy-plans/{bp.id}/cancel" in body
    assert "/resume" not in body  # not halted
    assert "/reset" not in body


def test_halt_from_pane_rerenders_halted_pane(hub_client: TestClient, db_session: Session, test_user: User):
    _manager(db_session, test_user)
    req, q, _ = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    db_session.commit()

    with patch("app.services.buyplan_notifications.run_notify_bg", new_callable=AsyncMock):
        r = hub_client.post(
            f"/v2/partials/buy-plans/{bp.id}/halt",
            data={"origin": "approvals_workspace", "lens": "buy-plans", "reason": "customer credit hold"},
        )
    assert r.status_code == 200
    assert r.headers.get("HX-Trigger") == "awListRefresh"
    assert "Halted" in r.text and "customer credit hold" in r.text
    db_session.expire(bp)
    assert bp.status == BuyPlanStatus.HALTED.value


def test_resume_from_pane(hub_client: TestClient, db_session: Session, test_user: User):
    _manager(db_session, test_user)
    req, q, _ = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.HALTED.value)
    db_session.commit()

    body = hub_client.get(f"/v2/partials/approvals/plan/{bp.id}/pane").text
    assert f"/v2/partials/buy-plans/{bp.id}/resume" in body
    assert f"/v2/partials/buy-plans/{bp.id}/reset" in body

    r = hub_client.post(
        f"/v2/partials/buy-plans/{bp.id}/resume",
        data={"origin": "approvals_workspace", "lens": "buy-plans"},
    )
    assert r.status_code == 200
    assert r.headers.get("HX-Trigger") == "awListRefresh"
    db_session.expire(bp)
    assert bp.status == BuyPlanStatus.ACTIVE.value


def test_cancel_from_pane(hub_client: TestClient, db_session: Session, test_user: User):
    _manager(db_session, test_user)
    req, q, _ = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    db_session.commit()

    with patch("app.services.buyplan_notifications.run_notify_bg", new_callable=AsyncMock):
        r = hub_client.post(
            f"/v2/partials/buy-plans/{bp.id}/cancel",
            data={"origin": "approvals_workspace", "lens": "sales-orders", "reason": "deal lost"},
        )
    assert r.status_code == 200
    assert "Cancelled" in r.text and "deal lost" in r.text
    db_session.expire(bp)
    assert bp.status == BuyPlanStatus.CANCELLED.value


def test_reset_from_pane_returns_draft(hub_client: TestClient, db_session: Session, test_user: User):
    _manager(db_session, test_user)
    req, q, _ = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.HALTED.value)
    db_session.commit()

    r = hub_client.post(
        f"/v2/partials/buy-plans/{bp.id}/reset",
        data={"origin": "approvals_workspace", "lens": "buy-plans"},
    )
    assert r.status_code == 200
    assert "Draft — not yet submitted" in r.text
    db_session.expire(bp)
    assert bp.status == BuyPlanStatus.DRAFT.value


# ── Stall warning (2.5) — plan_needs_approver_reason ─────────────────────


def test_stalled_pending_row_warns_on_bp_tab_list(hub_client: TestClient, db_session: Session, test_user: User):
    req, q, _ = _req_quote(db_session, test_user)
    _plan(db_session, req, q, status=BuyPlanStatus.PENDING.value)
    # Nobody holds the buy-plan approval right → the pending plan is stalled.
    test_user.can_approve_buy_plans = False
    db_session.commit()

    body = hub_client.get("/v2/partials/approvals/buy-plans/list").text
    assert "No approver configured — stalled" in body

    test_user.can_approve_buy_plans = True
    db_session.commit()
    body = hub_client.get("/v2/partials/approvals/buy-plans/list").text
    assert "No approver configured — stalled" not in body


def test_stalled_pane_shows_warning(hub_client: TestClient, db_session: Session, test_user: User):
    req, q, _ = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.PENDING.value)
    test_user.can_approve_buy_plans = False
    db_session.commit()

    body = hub_client.get(f"/v2/partials/approvals/plan/{bp.id}/pane").text
    assert "Stalled — no active user holds the" in body
