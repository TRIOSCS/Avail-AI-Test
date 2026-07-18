"""test_manager_edit_anything.py — manager edit-anything at verify (Workspace 2.3).

Spec §7: at verify the manager may edit ANYTHING — qty, unit cost, PO number, dates —
(audit covers it); vendor stays offer-swap-only for everyone; buyers stay refused;
the prepayment payee snapshot is immune to manager money edits. Covers the relaxed
_apply_line_edit guards, the /lines/{id}/edit route pass-through + workspace
re-render, the pane's manager edit form + Acctivate warning + edited-by marker.

Called by: pytest
Depends on: conftest (db_session, test_user), tests.test_approvals_hub_tabs builders,
            app.services.buyplan_workflow.buyplan_lines, app.routers.htmx.buy_plans.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.constants import ActivityType, BuyPlanLineStatus, BuyPlanStatus, UserRole
from app.database import get_db
from app.dependencies import require_buyplan_approver, require_buyplan_po_approver, require_user
from app.models import ActivityLog, User
from app.models.quality_plan import Prepayment
from tests.test_approvals_hub_tabs import _line, _plan, _req_quote


@pytest.fixture()
def hub_client(db_session: Session, test_user: User):
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


def _verify_stage_line(db: Session, user: User):
    """An ACTIVE plan with a PENDING_VERIFY (cut-PO) line."""
    req, q, rq = _req_quote(db, user)
    bp = _plan(db, req, q, status=BuyPlanStatus.ACTIVE.value)
    line = _line(
        db,
        bp,
        rq,
        user,
        status=BuyPlanLineStatus.PENDING_VERIFY.value,
        po_number="PO-100",
        po_confirmed_at=datetime.now(UTC),
    )
    db.commit()
    return bp, line


def _audit_rows(db: Session) -> list[ActivityLog]:
    return (
        db.query(ActivityLog)
        .filter(ActivityLog.activity_type == ActivityType.FIELD_EDIT.value)
        .order_by(ActivityLog.id)
        .all()
    )


# ── Manager can ──────────────────────────────────────────────────────────


def test_manager_edits_qty_cost_po_and_date_at_verify(hub_client, db_session, test_user):
    test_user.role = UserRole.MANAGER.value
    db_session.commit()
    bp, line = _verify_stage_line(db_session, test_user)

    r = hub_client.post(
        f"/v2/partials/buy-plans/{bp.id}/lines/{line.id}/edit",
        data={
            "origin": "approvals_workspace",
            "quantity": "250",
            "unit_cost": "1.75",
            "po_number": "PO-100-REV",
            "estimated_ship_date": "2026-09-15",
        },
    )
    assert r.status_code == 200
    assert r.headers.get("HX-Trigger") == "awListRefresh"  # workspace re-render branch
    db_session.expire_all()
    assert line.quantity == 250
    assert float(line.unit_cost) == 1.75
    assert line.po_number == "PO-100-REV"
    assert line.estimated_ship_date.strftime("%Y-%m-%d") == "2026-09-15"
    assert line.status == BuyPlanLineStatus.PENDING_VERIFY.value  # editing never re-stages

    # Every relaxed edit is field-diff logged (one row per save).
    (row,) = _audit_rows(db_session)
    fields = {e["field"] for e in row.details["edits"]}
    assert fields == {"quantity", "unit_cost", "po_number", "estimated_ship_date"}
    assert row.buy_plan_line_id == line.id


def test_manager_edit_marks_line_edited_by_manager_on_pane(hub_client, db_session, test_user):
    test_user.role = UserRole.MANAGER.value
    db_session.commit()
    bp, line = _verify_stage_line(db_session, test_user)

    body = hub_client.get(f"/v2/partials/approvals/po/{line.id}/pane").text
    assert "Edits here do not change Acctivate." in body  # the one-line warning
    assert "Manager edit" in body
    assert "Edited by manager" not in body  # not yet edited

    r = hub_client.post(
        f"/v2/partials/buy-plans/{bp.id}/lines/{line.id}/edit",
        data={"origin": "approvals_workspace", "quantity": "300"},
    )
    assert r.status_code == 200
    assert "Edited by manager" in r.text  # the marker appears on the re-rendered pane


# ── Buyer still refused ──────────────────────────────────────────────────


def test_buyer_still_refused_at_verify(hub_client, db_session, test_user):
    """A non-manager (buyer) on an ACTIVE plan is refused by the role×status gate."""
    bp, line = _verify_stage_line(db_session, test_user)  # test_user stays a buyer

    r = hub_client.post(
        f"/v2/partials/buy-plans/{bp.id}/lines/{line.id}/edit",
        data={"quantity": "250"},
    )
    assert r.status_code == 403
    db_session.expire_all()
    assert line.quantity == 100
    assert _audit_rows(db_session) == []


def test_non_manager_service_call_cannot_touch_po_fields(db_session, test_user):
    """Defense in depth: even where the plan-level gate passes (draft, owner), the
    PO-stage fields stay manager-at-verify only."""
    from app.services.buyplan_workflow import edit_buy_plan_line

    req, q, rq = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.DRAFT.value)
    line = _line(db_session, bp, rq, test_user, status=BuyPlanLineStatus.AWAITING_PO.value)
    db_session.commit()

    with pytest.raises(ValueError, match="Only a manager"):
        edit_buy_plan_line(bp.id, line.id, test_user, db_session, po_number="PO-HACK")


def test_manager_qty_edit_refused_outside_pending_verify(db_session, test_user):
    """The override is scoped to PENDING_VERIFY — a VERIFIED cut-PO line stays locked
    even for a manager."""
    from app.services.buyplan_workflow import edit_buy_plan_line

    test_user.role = UserRole.MANAGER.value
    req, q, rq = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    line = _line(
        db_session,
        bp,
        rq,
        test_user,
        status=BuyPlanLineStatus.VERIFIED.value,
        po_number="PO-DONE",
        po_confirmed_at=datetime.now(UTC),
    )
    db_session.commit()

    with pytest.raises(ValueError, match="Cannot change the quantity"):
        edit_buy_plan_line(bp.id, line.id, test_user, db_session, quantity=999)


# ── Vendor stays offer-swap-only, cut-PO locked, for everyone ────────────


def test_vendor_change_still_refused_for_manager_at_verify(hub_client, db_session, test_user):
    test_user.role = UserRole.MANAGER.value
    db_session.commit()
    bp, line = _verify_stage_line(db_session, test_user)
    original_offer_id = line.offer_id

    r = hub_client.post(
        f"/v2/partials/buy-plans/{bp.id}/lines/{line.id}/edit",
        data={"offer_id": str(original_offer_id + 999)},
    )
    assert r.status_code == 400
    assert "vendor" in r.json()["error"].lower()
    db_session.expire_all()
    assert line.offer_id == original_offer_id


# ── Payee snapshot immune ────────────────────────────────────────────────


def test_payee_snapshot_immune_to_manager_money_edits(hub_client, db_session, test_user):
    test_user.role = UserRole.MANAGER.value
    db_session.commit()
    bp, line = _verify_stage_line(db_session, test_user)
    pp = Prepayment(
        buy_plan_id=bp.id,
        buy_plan_line_id=line.id,
        total_incl_fees=Decimal("500.00"),
        currency="USD",
        payment_method="wire",
        vendor_name="Acme Dist",  # the request-time payee snapshot
        created_by_id=test_user.id,
    )
    db_session.add(pp)
    db_session.commit()

    r = hub_client.post(
        f"/v2/partials/buy-plans/{bp.id}/lines/{line.id}/edit",
        data={"quantity": "400", "unit_cost": "9.99"},
    )
    assert r.status_code == 200
    db_session.expire_all()
    assert line.quantity == 400
    assert pp.vendor_name == "Acme Dist"  # snapshot untouched
    assert float(pp.total_incl_fees) == 500.00
