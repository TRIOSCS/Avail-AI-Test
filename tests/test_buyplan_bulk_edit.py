"""tests/test_buyplan_bulk_edit.py — Buy-Plan bulk "save all" line editing.

Covers ``bulk_edit_buy_plan_lines`` (app/services/buyplan_workflow/buyplan_lines.py)
and its route (POST /v2/partials/buy-plans/{plan_id}/lines/bulk in
app/routers/htmx/buy_plans.py): editing multiple lines' qty/sell/vendor, adding new
lines, removing lines by omission, the PO-cut guard, and the role×status edit gate —
all in a single POST.

Called by: pytest
Depends on: conftest fixtures (client, test_user, sales_user, manager_user,
    test_requisition, test_offer), FastAPI TestClient.
"""

import json
import os

os.environ["TESTING"] = "1"

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.constants import BuyPlanLineStatus, BuyPlanStatus
from app.dependencies import require_user
from app.main import app
from app.models import Requirement, Requisition, User
from app.models.buy_plan import BuyPlan, BuyPlanLine

from contextlib import contextmanager


@contextmanager
def _acting_as(user: User):
    """Temporarily route require_user to *user* (restore prior override on exit)."""
    prior = app.dependency_overrides.get(require_user)
    app.dependency_overrides[require_user] = lambda: user
    try:
        yield
    finally:
        if prior is not None:
            app.dependency_overrides[require_user] = prior
        else:
            app.dependency_overrides.pop(require_user, None)


def _req(db: Session, owner: User, *, customer: str = "Acme Electronics") -> Requisition:
    req = Requisition(
        name="REQ-BULK",
        customer_name=customer,
        status="open",
        created_by=owner.id,
        created_at=datetime.now(UTC),
    )
    db.add(req)
    db.flush()
    db.add(
        Requirement(
            requisition_id=req.id,
            primary_mpn="LM317T",
            target_qty=1000,
            target_price=0.75,
            created_at=datetime.now(UTC),
        )
    )
    db.commit()
    db.refresh(req)
    return req


def _requirement_of(db: Session, req: Requisition) -> Requirement:
    return db.query(Requirement).filter(Requirement.requisition_id == req.id).first()


def _plan(db: Session, req: Requisition, *, status=BuyPlanStatus.DRAFT.value, **ov) -> BuyPlan:
    defaults = dict(
        requisition_id=req.id,
        status=status,
        so_status="pending",
        total_cost=100.00,
        total_revenue=200.00,
        total_margin_pct=50.00,
        ai_flags=[],
        created_at=datetime.now(UTC),
    )
    defaults.update(ov)
    plan = BuyPlan(**defaults)
    db.add(plan)
    db.commit()
    db.refresh(plan)
    return plan


def _line(db: Session, plan: BuyPlan, **ov) -> BuyPlanLine:
    defaults = dict(
        buy_plan_id=plan.id,
        quantity=100,
        unit_cost=1.00,
        unit_sell=2.00,
        status=BuyPlanLineStatus.AWAITING_PO.value,
    )
    defaults.update(ov)
    line = BuyPlanLine(**defaults)
    db.add(line)
    db.commit()
    db.refresh(line)
    return line


# ══ Service-level ══════════════════════════════════════════════════════


def test_bulk_edit_multiple_lines_recomputes_header(db_session, test_user, test_requisition):
    from app.services.buyplan_workflow import bulk_edit_buy_plan_lines

    plan = _plan(db_session, test_requisition, status=BuyPlanStatus.DRAFT.value)
    a = _line(db_session, plan, quantity=100, unit_cost=1.00, unit_sell=2.00)  # cost 100 rev 200
    b = _line(db_session, plan, quantity=50, unit_cost=4.00, unit_sell=6.00)  # cost 200 rev 300

    payload = [
        {"line_id": a.id, "quantity": 200, "unit_sell": 3.00},  # cost 200 rev 600
        {"line_id": b.id, "unit_sell": 8.00},  # cost 200 rev 400
    ]
    bulk_edit_buy_plan_lines(plan.id, payload, test_user, db_session)
    db_session.commit()
    db_session.refresh(plan)
    db_session.refresh(a)
    db_session.refresh(b)

    assert a.quantity == 200
    assert float(a.unit_sell) == 3.00
    assert float(b.unit_sell) == 8.00
    assert float(plan.total_cost) == 400.0
    assert float(plan.total_revenue) == 1000.0


def test_bulk_edit_vendor_change_recomputes_cost(db_session, test_user, test_requisition, test_offer):
    from app.services.buyplan_workflow import bulk_edit_buy_plan_lines

    plan = _plan(db_session, test_requisition, status=BuyPlanStatus.DRAFT.value)
    line = _line(db_session, plan, quantity=100, unit_cost=1.00, unit_sell=2.00)

    bulk_edit_buy_plan_lines(plan.id, [{"line_id": line.id, "offer_id": test_offer.id}], test_user, db_session)
    db_session.commit()
    db_session.refresh(line)

    assert line.offer_id == test_offer.id
    assert float(line.unit_cost) == 0.50  # test_offer.unit_price
    assert line.buyer_id == test_user.id  # vendor_ownership cascade (test_offer.entered_by_id)


def test_bulk_edit_adds_new_line_with_buyer_assignment(db_session, test_user, test_requisition, test_offer):
    from app.services.buyplan_workflow import bulk_edit_buy_plan_lines

    plan = _plan(db_session, test_requisition, status=BuyPlanStatus.DRAFT.value)
    requirement = _requirement_of(db_session, test_requisition)

    payload = [
        {"requirement_id": requirement.id, "offer_id": test_offer.id, "quantity": 1000, "unit_sell": 0.60},
    ]
    plan = bulk_edit_buy_plan_lines(plan.id, payload, test_user, db_session)
    db_session.commit()
    db_session.refresh(plan)

    assert len(plan.lines) == 1
    new_line = plan.lines[0]
    assert new_line.quantity == 1000
    assert float(new_line.unit_sell) == 0.60
    assert float(new_line.unit_cost) == 0.50
    assert new_line.buyer_id == test_user.id  # assign_buyer vendor_ownership cascade


def test_bulk_edit_omitted_editable_line_is_removed(db_session, test_user, test_requisition):
    from app.services.buyplan_workflow import bulk_edit_buy_plan_lines

    plan = _plan(db_session, test_requisition, status=BuyPlanStatus.DRAFT.value)
    keep = _line(db_session, plan, quantity=100, unit_cost=1.00, unit_sell=2.00)
    drop = _line(db_session, plan, quantity=50, unit_cost=4.00, unit_sell=6.00)

    payload = [{"line_id": keep.id, "unit_sell": 2.00}]
    plan = bulk_edit_buy_plan_lines(plan.id, payload, test_user, db_session)
    db_session.commit()
    db_session.refresh(plan)

    assert [ln.id for ln in plan.lines] == [keep.id]
    assert drop.id not in [ln.id for ln in plan.lines]


def test_bulk_edit_omitted_po_cut_line_untouched(db_session, manager_user, test_requisition):
    from app.services.buyplan_workflow import bulk_edit_buy_plan_lines

    plan = _plan(db_session, test_requisition, status=BuyPlanStatus.ACTIVE.value)
    kept_editable = _line(db_session, plan, quantity=10, unit_cost=1.00, unit_sell=2.00)
    po_cut = _line(
        db_session,
        plan,
        quantity=50,
        unit_cost=4.00,
        unit_sell=6.00,
        status=BuyPlanLineStatus.PENDING_VERIFY.value,
    )

    # Only the editable line is submitted; the PO-cut line is omitted entirely.
    payload = [{"line_id": kept_editable.id, "unit_sell": 3.00}]
    plan = bulk_edit_buy_plan_lines(plan.id, payload, manager_user, db_session)
    db_session.commit()
    db_session.refresh(plan)

    line_ids = {ln.id for ln in plan.lines}
    assert kept_editable.id in line_ids
    assert po_cut.id in line_ids  # left untouched, NOT removed


def test_bulk_edit_qty_change_on_po_cut_line_rejected(db_session, manager_user, test_requisition):
    from app.services.buyplan_workflow import bulk_edit_buy_plan_lines

    plan = _plan(db_session, test_requisition, status=BuyPlanStatus.ACTIVE.value)
    line = _line(db_session, plan, status=BuyPlanLineStatus.PENDING_VERIFY.value)

    with pytest.raises(ValueError, match="quantity"):
        bulk_edit_buy_plan_lines(plan.id, [{"line_id": line.id, "quantity": 999}], manager_user, db_session)


def test_bulk_edit_vendor_change_on_po_cut_line_rejected(db_session, manager_user, test_requisition, test_offer):
    from app.services.buyplan_workflow import bulk_edit_buy_plan_lines

    plan = _plan(db_session, test_requisition, status=BuyPlanStatus.ACTIVE.value)
    line = _line(db_session, plan, status=BuyPlanLineStatus.PENDING_VERIFY.value)

    with pytest.raises(ValueError, match="vendor"):
        bulk_edit_buy_plan_lines(
            plan.id, [{"line_id": line.id, "offer_id": test_offer.id}], manager_user, db_session
        )


def test_bulk_edit_qty_zero_rejected(db_session, test_user, test_requisition):
    from app.services.buyplan_workflow import bulk_edit_buy_plan_lines

    plan = _plan(db_session, test_requisition, status=BuyPlanStatus.DRAFT.value)
    line = _line(db_session, plan)

    with pytest.raises(ValueError, match="positive"):
        bulk_edit_buy_plan_lines(plan.id, [{"line_id": line.id, "quantity": 0}], test_user, db_session)


def test_bulk_edit_new_line_foreign_requirement_rejected(db_session, test_user, test_requisition, test_offer):
    from app.services.buyplan_workflow import bulk_edit_buy_plan_lines

    plan = _plan(db_session, test_requisition, status=BuyPlanStatus.DRAFT.value)
    other = _req(db_session, test_user)
    foreign_req = _requirement_of(db_session, other)

    payload = [{"requirement_id": foreign_req.id, "offer_id": test_offer.id, "quantity": 10}]
    with pytest.raises(ValueError, match="requisition"):
        bulk_edit_buy_plan_lines(plan.id, payload, test_user, db_session)


def test_bulk_edit_unknown_offer_rejected(db_session, test_user, test_requisition):
    from app.services.buyplan_workflow import bulk_edit_buy_plan_lines

    plan = _plan(db_session, test_requisition, status=BuyPlanStatus.DRAFT.value)
    requirement = _requirement_of(db_session, test_requisition)

    payload = [{"requirement_id": requirement.id, "offer_id": 999999, "quantity": 10}]
    with pytest.raises(ValueError, match="Offer"):
        bulk_edit_buy_plan_lines(plan.id, payload, test_user, db_session)


def test_bulk_edit_unknown_line_id_rejected(db_session, test_user, test_requisition):
    from app.services.buyplan_workflow import bulk_edit_buy_plan_lines

    plan = _plan(db_session, test_requisition, status=BuyPlanStatus.DRAFT.value)

    with pytest.raises(ValueError, match="Line"):
        bulk_edit_buy_plan_lines(plan.id, [{"line_id": 999999, "unit_sell": 1.0}], test_user, db_session)


# ══ Route-level ══════════════════════════════════════════════════════


def test_route_bulk_edit_happy_path_returns_200(client: TestClient, db_session, test_user, test_requisition):
    plan = _plan(db_session, test_requisition, status=BuyPlanStatus.DRAFT.value)
    line = _line(db_session, plan, quantity=100, unit_cost=1.00, unit_sell=2.00)

    payload = {"lines": [{"line_id": line.id, "quantity": 150, "unit_sell": 2.50}]}
    resp = client.post(
        f"/v2/partials/buy-plans/{plan.id}/lines/bulk",
        data={"payload": json.dumps(payload)},
    )
    assert resp.status_code == 200
    db_session.expire_all()
    assert db_session.get(BuyPlanLine, line.id).quantity == 150


def test_route_bulk_edit_malformed_json_400(client: TestClient, db_session, test_requisition):
    plan = _plan(db_session, test_requisition, status=BuyPlanStatus.DRAFT.value)

    resp = client.post(
        f"/v2/partials/buy-plans/{plan.id}/lines/bulk",
        data={"payload": "{not valid json"},
    )
    assert resp.status_code == 400
    assert "error" in resp.json()


def test_route_bulk_edit_wrong_shape_400(client: TestClient, db_session, test_requisition):
    plan = _plan(db_session, test_requisition, status=BuyPlanStatus.DRAFT.value)

    resp = client.post(
        f"/v2/partials/buy-plans/{plan.id}/lines/bulk",
        data={"payload": json.dumps({"not_lines": []})},
    )
    assert resp.status_code == 400


def test_route_bulk_edit_non_owner_sales_draft_403(client: TestClient, db_session, sales_user, test_user):
    req = _req(db_session, sales_user)
    plan = _plan(db_session, req, status=BuyPlanStatus.DRAFT.value)
    line = _line(db_session, plan)

    # Default client acts as test_user, a buyer (non-restricted role, so the router's
    # per-record ownership check does not 404 it) who is neither the plan owner nor a
    # manager on a pre-approval DRAFT plan → the service gate rejects with 403.
    payload = {"lines": [{"line_id": line.id, "unit_sell": 9.0}]}
    resp = client.post(
        f"/v2/partials/buy-plans/{plan.id}/lines/bulk",
        data={"payload": json.dumps(payload)},
    )
    assert resp.status_code == 403


def test_route_bulk_edit_sales_on_active_plan_403(client: TestClient, db_session, sales_user):
    # Plan owned by sales_user (passes the router's per-record ownership check) but
    # ACTIVE — post-approval line edits are manager-only, so the service gate rejects.
    req = _req(db_session, sales_user)
    plan = _plan(db_session, req, status=BuyPlanStatus.ACTIVE.value)
    line = _line(db_session, plan)

    payload = {"lines": [{"line_id": line.id, "unit_sell": 9.0}]}
    with _acting_as(sales_user):
        resp = client.post(
            f"/v2/partials/buy-plans/{plan.id}/lines/bulk",
            data={"payload": json.dumps(payload)},
        )
    assert resp.status_code == 403
