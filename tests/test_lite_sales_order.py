"""test_lite_sales_order.py — order type at create + the lite path (Workspace 1.3).

Covers create_lite_sales_order / create_sales_order_from_offers(order_type=...), the
picker's order-type fork (non-sourcing lists offer-less requisitions; builder collapses
to a create-only confirm), the create route's lite branch, and the CRITICAL zero-line
lifecycle guarantees: submitting keeps the declared stock-sale flag; approving a
zero-line plan goes ACTIVE, creates NO buyer tasks, and does NOT auto-complete.

Called by: pytest
Depends on: conftest (db_session, test_user), app.services.buyplan_builder,
            app.services.buyplan_workflow, app.routers.htmx.buy_plans.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.constants import BuyPlanStatus, SalesOrderType
from app.database import get_db
from app.dependencies import require_buyplan_approver, require_user
from app.models import Requirement, User
from app.models.buy_plan import BuyPlan, BuyPlanLine
from app.models.sourcing import Requisition
from app.models.task import RequisitionTask
from app.services.buyplan_builder import (
    DuplicateSalesOrderError,
    create_lite_sales_order,
    create_sales_order_from_offers,
)


@pytest.fixture()
def lite_client(db_session: Session, test_user: User):
    """TestClient authed as test_user with the buy-plan approve right (the lite plan's
    engine request must route to someone)."""
    from app.main import app

    test_user.can_approve_buy_plans = True
    db_session.commit()

    app.dependency_overrides[get_db] = lambda: (yield db_session)  # type: ignore[misc]
    app.dependency_overrides[require_user] = lambda: test_user
    app.dependency_overrides[require_buyplan_approver] = lambda: test_user
    try:
        with TestClient(app) as c:
            yield c
    finally:
        for dep in (get_db, require_user, require_buyplan_approver):
            app.dependency_overrides.pop(dep, None)


def _open_req(db: Session, user: User, *, with_requirement: bool = True) -> Requisition:
    """An OPEN_PIPELINE requisition with (optionally) one requirement and NO offers."""
    req = Requisition(
        name=f"REQ-{uuid.uuid4().hex[:6]}",
        customer_name="LiteCo",
        status="open",
        created_by=user.id,
        created_at=datetime.now(UTC),
    )
    db.add(req)
    db.flush()
    if with_requirement:
        db.add(Requirement(requisition_id=req.id, primary_mpn="LM317", created_at=datetime.now(UTC)))
    db.commit()
    return req


# ── Service: create_lite_sales_order ─────────────────────────────────────


def test_lite_creates_zero_line_draft(db_session: Session, test_user: User):
    req = _open_req(db_session, test_user)
    plan = create_lite_sales_order(req.id, SalesOrderType.TESTING_SERVICE.value, db_session, test_user)

    assert plan.status == BuyPlanStatus.DRAFT.value
    assert plan.order_type == SalesOrderType.TESTING_SERVICE.value
    assert plan.quote_id is None
    assert plan.submitted_by_id == test_user.id
    assert db_session.query(BuyPlanLine).filter(BuyPlanLine.buy_plan_id == plan.id).count() == 0


def test_lite_stock_sale_sets_flag(db_session: Session, test_user: User):
    req = _open_req(db_session, test_user)
    plan = create_lite_sales_order(req.id, SalesOrderType.STOCK_SALE.value, db_session, test_user)
    assert plan.is_stock_sale is True


def test_lite_rejects_sourcing_and_unknown_types(db_session: Session, test_user: User):
    req = _open_req(db_session, test_user)
    with pytest.raises(ValueError, match="sources through offers"):
        create_lite_sales_order(req.id, SalesOrderType.NEW.value, db_session, test_user)
    with pytest.raises(ValueError, match="Invalid order type"):
        create_lite_sales_order(req.id, "bogus", db_session, test_user)


def test_lite_duplicate_open_so_raises(db_session: Session, test_user: User):
    req = _open_req(db_session, test_user)
    create_lite_sales_order(req.id, SalesOrderType.COMPS.value, db_session, test_user)
    with pytest.raises(DuplicateSalesOrderError):
        create_lite_sales_order(req.id, SalesOrderType.COMPS.value, db_session, test_user)


def test_lite_missing_requisition_raises(db_session: Session, test_user: User):
    with pytest.raises(ValueError, match="not found"):
        create_lite_sales_order(999999, SalesOrderType.COMPS.value, db_session, test_user)


def test_offers_builder_rejects_non_sourcing_type(db_session: Session, test_user: User):
    req = _open_req(db_session, test_user)
    with pytest.raises(ValueError, match="does not source through offers"):
        create_sales_order_from_offers(req.id, {}, {}, db_session, test_user, order_type="stock_sale")


# ── Picker + create route ────────────────────────────────────────────────


def test_picker_non_sourcing_lists_offerless_requisitions(
    lite_client: TestClient, db_session: Session, test_user: User
):
    req = _open_req(db_session, test_user)  # no offers at all

    sourcing_txt = lite_client.get("/v2/partials/buy-plans/sales-orders/new").text
    assert f"REQ #{req.id}" not in sourcing_txt  # sourcing path requires offers

    lite_txt = lite_client.get("/v2/partials/buy-plans/sales-orders/new?order_type=stock_sale").text
    assert f"REQ #{req.id}" in lite_txt  # lite path lists it
    assert "Lite path" in lite_txt


def test_picker_has_order_type_select(lite_client: TestClient):
    txt = lite_client.get("/v2/partials/buy-plans/sales-orders/new").text
    assert 'name="order_type"' in txt
    for val in ("new", "revision", "testing_service", "comps", "stock_sale"):
        assert f'value="{val}"' in txt


def test_lite_builder_mode_is_create_only(lite_client: TestClient, db_session: Session, test_user: User):
    req = _open_req(db_session, test_user)
    txt = lite_client.get(
        f"/v2/partials/buy-plans/sales-orders/new?requisition_id={req.id}&order_type=testing_service"
    ).text
    assert "no buy-plan lines" in txt
    assert "Create Sales Order" in txt
    assert "Sell price" not in txt  # no offer table on the lite path


def test_create_route_lite_branch(lite_client: TestClient, db_session: Session, test_user: User):
    req = _open_req(db_session, test_user)
    r = lite_client.post(
        "/v2/partials/buy-plans/sales-orders/create",
        data={"requisition_id": req.id, "order_type": "stock_sale"},
    )
    assert r.status_code == 200

    plan = db_session.query(BuyPlan).filter(BuyPlan.requisition_id == req.id).one()
    assert plan.order_type == SalesOrderType.STOCK_SALE.value
    assert plan.is_stock_sale is True
    assert len(plan.lines or []) == 0


# ── The zero-line lifecycle guarantees (MUST) ────────────────────────────


def _submitted_lite_plan(db: Session, user: User) -> BuyPlan:
    """A lite stock-sale plan submitted for approval (engine request open)."""
    from app.services.buyplan_workflow import submit_buy_plan

    req = _open_req(db, user)
    plan = create_lite_sales_order(req.id, SalesOrderType.STOCK_SALE.value, db, user)
    submit_buy_plan(plan.id, "SO-LITE-1", user, db)
    db.commit()
    return plan


def test_submit_keeps_declared_stock_sale_flag(db_session: Session, test_user: User):
    test_user.can_approve_buy_plans = True
    db_session.commit()
    plan = _submitted_lite_plan(db_session, test_user)
    assert plan.status == BuyPlanStatus.PENDING.value
    # The vendor-name inference must NOT clobber the declared type on a zero-line plan.
    assert plan.is_stock_sale is True


def test_approve_zero_line_plan_goes_active_no_tasks_no_autocomplete(
    lite_client: TestClient, db_session: Session, test_user: User
):
    """THE lite-path contract: approving a zero-line plan activates it, generates ZERO
    buyer 'Cut PO' tasks, and does NOT auto-complete it (check_completion's empty-lines
    early return)."""
    plan = _submitted_lite_plan(db_session, test_user)

    with patch("app.services.buyplan_notifications.run_notify_bg", new_callable=AsyncMock):
        r = lite_client.post(
            f"/v2/partials/buy-plans/{plan.id}/approve",
            data={"action": "approve"},
        )
    assert r.status_code == 200

    db_session.expire_all()
    plan = db_session.get(BuyPlan, plan.id)
    assert plan.status == BuyPlanStatus.ACTIVE.value  # ACTIVE — approved
    assert plan.status != BuyPlanStatus.COMPLETED.value  # explicitly NOT auto-completed
    assert plan.completed_at is None
    # Zero lines ⇒ zero buyer tasks (the untouched engine's per-line task loop).
    tasks = (
        db_session.query(RequisitionTask)
        .filter(
            RequisitionTask.requisition_id == plan.requisition_id,
            RequisitionTask.source_ref.like("buyline:%"),
        )
        .count()
    )
    assert tasks == 0
