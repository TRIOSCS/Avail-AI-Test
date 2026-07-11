"""tests/test_buyplan_bulk_edit.py — Buy-Plan bulk "save all" line editing.

Covers ``bulk_edit_buy_plan_lines`` (app/services/buyplan_workflow/buyplan_lines.py)
and its route (POST /v2/partials/buy-plans/{plan_id}/lines/bulk in
app/routers/htmx/buy_plans.py): editing multiple lines' qty/sell/vendor, adding new
lines, removing lines by omission, the PO-cut guard, and the role×status edit gate —
all in a single POST.

Called by: pytest
Depends on: conftest fixtures (client, test_user, sales_user, manager_user,
    test_requisition, test_offer, and the shared buy-plan line-editing factories
    _buyplan_req/_buyplan_requirement_of/_buyplan_plan/_buyplan_line/_buyplan_offer —
    also consumed by test_buy_plan_epic.py), FastAPI TestClient.
"""

import json
import os

os.environ["TESTING"] = "1"

from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.constants import BuyPlanLineStatus, BuyPlanStatus, OfferStatus
from app.dependencies import require_user
from app.main import app
from app.models import User
from app.models.buy_plan import BuyPlan, BuyPlanLine
from tests.conftest import _buyplan_line as _line
from tests.conftest import _buyplan_offer as _offer
from tests.conftest import _buyplan_plan as _plan
from tests.conftest import _buyplan_req as _req
from tests.conftest import _buyplan_requirement_of as _requirement_of


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
    requirement = _requirement_of(db_session, test_requisition)
    test_offer.requirement_id = requirement.id
    db_session.commit()
    line = _line(db_session, plan, quantity=100, unit_cost=1.00, unit_sell=2.00, requirement_id=requirement.id)

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
    test_offer.requirement_id = requirement.id
    db_session.commit()

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
        bulk_edit_buy_plan_lines(plan.id, [{"line_id": line.id, "offer_id": test_offer.id}], manager_user, db_session)


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


# ══ Fix 1 — removal scoped to known_line_ids ══════════════════════════


def test_bulk_edit_concurrent_line_absent_from_known_ids_survives(db_session, test_user, test_requisition):
    from app.services.buyplan_workflow import bulk_edit_buy_plan_lines

    plan = _plan(db_session, test_requisition, status=BuyPlanStatus.DRAFT.value)
    seen = _line(db_session, plan, quantity=100, unit_cost=1.00, unit_sell=2.00)
    # Added by someone else AFTER the client's form loaded — never in known_line_ids.
    concurrent = _line(db_session, plan, quantity=50, unit_cost=4.00, unit_sell=6.00)

    payload = [{"line_id": seen.id, "unit_sell": 3.00}]
    plan = bulk_edit_buy_plan_lines(plan.id, payload, test_user, db_session, known_line_ids=[seen.id])
    db_session.commit()
    db_session.refresh(plan)

    line_ids = {ln.id for ln in plan.lines}
    assert seen.id in line_ids
    assert concurrent.id in line_ids  # NOT removed — client never saw it


def test_bulk_edit_known_line_ids_absent_uses_legacy_removal(db_session, test_user, test_requisition):
    from app.services.buyplan_workflow import bulk_edit_buy_plan_lines

    plan = _plan(db_session, test_requisition, status=BuyPlanStatus.DRAFT.value)
    seen = _line(db_session, plan, quantity=100, unit_cost=1.00, unit_sell=2.00)
    other = _line(db_session, plan, quantity=50, unit_cost=4.00, unit_sell=6.00)

    payload = [{"line_id": seen.id, "unit_sell": 3.00}]
    # No known_line_ids kwarg at all -> legacy unscoped removal-by-omission.
    plan = bulk_edit_buy_plan_lines(plan.id, payload, test_user, db_session)
    db_session.commit()
    db_session.refresh(plan)

    assert [ln.id for ln in plan.lines] == [seen.id]
    assert other.id not in [ln.id for ln in plan.lines]


def test_bulk_edit_known_line_ids_scopes_editable_omission_removal(db_session, test_user, test_requisition):
    from app.services.buyplan_workflow import bulk_edit_buy_plan_lines

    plan = _plan(db_session, test_requisition, status=BuyPlanStatus.DRAFT.value)
    seen = _line(db_session, plan, quantity=100, unit_cost=1.00, unit_sell=2.00)
    known_but_dropped = _line(db_session, plan, quantity=50, unit_cost=4.00, unit_sell=6.00)

    payload = [{"line_id": seen.id, "unit_sell": 3.00}]
    plan = bulk_edit_buy_plan_lines(
        plan.id, payload, test_user, db_session, known_line_ids=[seen.id, known_but_dropped.id]
    )
    db_session.commit()
    db_session.refresh(plan)

    # known_but_dropped WAS in known_line_ids and is omitted from the payload -> removed.
    assert [ln.id for ln in plan.lines] == [seen.id]


# ══ Fix 2a — unit_sell key-presence semantics ═════════════════════════


def test_bulk_edit_unit_sell_null_clears(db_session, test_user, test_requisition):
    from app.services.buyplan_workflow import bulk_edit_buy_plan_lines

    plan = _plan(db_session, test_requisition, status=BuyPlanStatus.DRAFT.value)
    line = _line(db_session, plan, quantity=100, unit_cost=1.00, unit_sell=2.00)

    bulk_edit_buy_plan_lines(plan.id, [{"line_id": line.id, "unit_sell": None}], test_user, db_session)
    db_session.commit()
    db_session.refresh(line)

    assert line.unit_sell is None
    assert line.margin_pct is None


def test_bulk_edit_unit_sell_absent_leaves_unchanged(db_session, test_user, test_requisition):
    from app.services.buyplan_workflow import bulk_edit_buy_plan_lines

    plan = _plan(db_session, test_requisition, status=BuyPlanStatus.DRAFT.value)
    line = _line(db_session, plan, quantity=100, unit_cost=1.00, unit_sell=2.00)

    # unit_sell key entirely absent -> unchanged (only qty changes).
    bulk_edit_buy_plan_lines(plan.id, [{"line_id": line.id, "quantity": 150}], test_user, db_session)
    db_session.commit()
    db_session.refresh(line)

    assert line.quantity == 150
    assert float(line.unit_sell) == 2.00


# ══ Fix 2b — unchanged qty/offer on a PO-cut line is a no-op ══════════


def test_bulk_edit_unchanged_qty_offer_on_po_cut_line_is_noop(db_session, manager_user, test_requisition, test_offer):
    from app.services.buyplan_workflow import bulk_edit_buy_plan_lines

    plan = _plan(db_session, test_requisition, status=BuyPlanStatus.ACTIVE.value)
    line = _line(
        db_session,
        plan,
        quantity=100,
        unit_cost=0.50,
        unit_sell=2.00,
        offer_id=test_offer.id,
        status=BuyPlanLineStatus.PENDING_VERIFY.value,
    )

    # Resending the SAME quantity and SAME offer_id must not trip the cut-PO guard —
    # only the sell price (which is always editable) actually changes.
    payload = [{"line_id": line.id, "quantity": 100, "offer_id": test_offer.id, "unit_sell": 3.00}]
    bulk_edit_buy_plan_lines(plan.id, payload, manager_user, db_session)
    db_session.commit()
    db_session.refresh(line)

    assert line.quantity == 100
    assert line.offer_id == test_offer.id
    assert float(line.unit_sell) == 3.00


def test_bulk_edit_actual_qty_change_on_po_cut_line_still_rejected(db_session, manager_user, test_requisition):
    from app.services.buyplan_workflow import bulk_edit_buy_plan_lines

    plan = _plan(db_session, test_requisition, status=BuyPlanStatus.ACTIVE.value)
    line = _line(db_session, plan, quantity=100, status=BuyPlanLineStatus.PENDING_VERIFY.value)

    with pytest.raises(ValueError, match="quantity"):
        bulk_edit_buy_plan_lines(plan.id, [{"line_id": line.id, "quantity": 101}], manager_user, db_session)


# ══ Fix 2c — fractional quantity rejected ══════════════════════════════


def test_bulk_edit_fractional_quantity_rejected_on_edit(db_session, test_user, test_requisition):
    from app.services.buyplan_workflow import bulk_edit_buy_plan_lines

    plan = _plan(db_session, test_requisition, status=BuyPlanStatus.DRAFT.value)
    line = _line(db_session, plan, quantity=100)

    with pytest.raises(ValueError, match="whole number"):
        bulk_edit_buy_plan_lines(plan.id, [{"line_id": line.id, "quantity": 3.5}], test_user, db_session)


def test_bulk_edit_fractional_quantity_rejected_on_add(db_session, test_user, test_requisition, test_offer):
    from app.services.buyplan_workflow import bulk_edit_buy_plan_lines

    plan = _plan(db_session, test_requisition, status=BuyPlanStatus.DRAFT.value)
    requirement = _requirement_of(db_session, test_requisition)

    payload = [{"requirement_id": requirement.id, "offer_id": test_offer.id, "quantity": 12.5}]
    with pytest.raises(ValueError, match="whole number"):
        bulk_edit_buy_plan_lines(plan.id, payload, test_user, db_session)


# ══ Fix 4 — offer must belong to the plan's requisition ════════════════


def test_bulk_edit_foreign_offer_rejected_on_add(db_session, test_user, test_requisition):
    from app.services.buyplan_workflow import bulk_edit_buy_plan_lines

    plan = _plan(db_session, test_requisition, status=BuyPlanStatus.DRAFT.value)
    requirement = _requirement_of(db_session, test_requisition)
    other_req = _req(db_session, test_user)
    foreign_offer = _offer(db_session, other_req, test_user)

    payload = [{"requirement_id": requirement.id, "offer_id": foreign_offer.id, "quantity": 10}]
    with pytest.raises(ValueError, match="requisition"):
        bulk_edit_buy_plan_lines(plan.id, payload, test_user, db_session)


def test_bulk_edit_foreign_offer_rejected_on_vendor_change(db_session, test_user, test_requisition):
    from app.services.buyplan_workflow import bulk_edit_buy_plan_lines

    plan = _plan(db_session, test_requisition, status=BuyPlanStatus.DRAFT.value)
    line = _line(db_session, plan, quantity=100, unit_cost=1.00, unit_sell=2.00)
    other_req = _req(db_session, test_user)
    foreign_offer = _offer(db_session, other_req, test_user)

    payload = [{"line_id": line.id, "offer_id": foreign_offer.id}]
    with pytest.raises(ValueError, match="requisition"):
        bulk_edit_buy_plan_lines(plan.id, payload, test_user, db_session)


# ══ Fix 1/9 — offer must also match the requirement/part and be ACTIVE ═


def test_bulk_edit_wrong_part_offer_rejected_on_add(db_session, test_user, test_requisition):
    from app.services.buyplan_workflow import bulk_edit_buy_plan_lines

    plan = _plan(db_session, test_requisition, status=BuyPlanStatus.DRAFT.value)
    requirement = _requirement_of(db_session, test_requisition)
    other_req = _req(db_session, test_user)
    other_requirement = _requirement_of(db_session, other_req)
    # Right requisition, but tagged for a DIFFERENT part.
    wrong_part_offer = _offer(db_session, test_requisition, test_user, requirement_id=other_requirement.id)

    payload = [{"requirement_id": requirement.id, "offer_id": wrong_part_offer.id, "quantity": 10}]
    with pytest.raises(ValueError, match="part"):
        bulk_edit_buy_plan_lines(plan.id, payload, test_user, db_session)


def test_bulk_edit_wrong_part_offer_rejected_on_vendor_change(db_session, test_user, test_requisition):
    from app.services.buyplan_workflow import bulk_edit_buy_plan_lines

    plan = _plan(db_session, test_requisition, status=BuyPlanStatus.DRAFT.value)
    requirement = _requirement_of(db_session, test_requisition)
    line = _line(db_session, plan, quantity=100, unit_cost=1.00, unit_sell=2.00, requirement_id=requirement.id)
    other_req = _req(db_session, test_user)
    other_requirement = _requirement_of(db_session, other_req)
    wrong_part_offer = _offer(db_session, test_requisition, test_user, requirement_id=other_requirement.id)

    payload = [{"line_id": line.id, "offer_id": wrong_part_offer.id}]
    with pytest.raises(ValueError, match="part"):
        bulk_edit_buy_plan_lines(plan.id, payload, test_user, db_session)


def test_bulk_edit_non_active_offer_rejected_on_add(db_session, test_user, test_requisition):
    from app.services.buyplan_workflow import bulk_edit_buy_plan_lines

    plan = _plan(db_session, test_requisition, status=BuyPlanStatus.DRAFT.value)
    requirement = _requirement_of(db_session, test_requisition)
    sold_offer = _offer(
        db_session, test_requisition, test_user, requirement_id=requirement.id, status=OfferStatus.SOLD.value
    )

    payload = [{"requirement_id": requirement.id, "offer_id": sold_offer.id, "quantity": 10}]
    with pytest.raises(ValueError, match="not active"):
        bulk_edit_buy_plan_lines(plan.id, payload, test_user, db_session)


def test_bulk_edit_non_active_offer_rejected_on_vendor_change(db_session, test_user, test_requisition):
    from app.services.buyplan_workflow import bulk_edit_buy_plan_lines

    plan = _plan(db_session, test_requisition, status=BuyPlanStatus.DRAFT.value)
    requirement = _requirement_of(db_session, test_requisition)
    line = _line(db_session, plan, quantity=100, unit_cost=1.00, unit_sell=2.00, requirement_id=requirement.id)
    sold_offer = _offer(
        db_session, test_requisition, test_user, requirement_id=requirement.id, status=OfferStatus.SOLD.value
    )

    payload = [{"line_id": line.id, "offer_id": sold_offer.id}]
    with pytest.raises(ValueError, match="not active"):
        bulk_edit_buy_plan_lines(plan.id, payload, test_user, db_session)


def test_bulk_edit_unchanged_resend_of_now_sold_offer_still_succeeds(db_session, test_user, test_requisition):
    """The offer-attachability revalidation NEVER runs on a no-op resend — a line's
    already-attached offer may have legitimately gone SOLD since it was cut, and
    resending the SAME unchanged offer_id must stay a no-op success regardless."""
    from app.services.buyplan_workflow import bulk_edit_buy_plan_lines

    plan = _plan(db_session, test_requisition, status=BuyPlanStatus.DRAFT.value)
    requirement = _requirement_of(db_session, test_requisition)
    offer = _offer(db_session, test_requisition, test_user, requirement_id=requirement.id)
    line = _line(
        db_session, plan, quantity=100, unit_cost=0.40, unit_sell=2.00, offer_id=offer.id, requirement_id=requirement.id
    )

    offer.status = OfferStatus.SOLD.value
    db_session.commit()

    payload = [{"line_id": line.id, "offer_id": offer.id, "unit_sell": 3.00}]
    bulk_edit_buy_plan_lines(plan.id, payload, test_user, db_session)
    db_session.commit()
    db_session.refresh(line)

    assert line.offer_id == offer.id
    assert float(line.unit_sell) == 3.00


# ══ Fix 2 — falsy-zero unit_cost/unit_sell and stale ai_score ══════════


def test_bulk_edit_zero_price_offer_keeps_unit_cost_zero_on_add(db_session, test_user, test_requisition):
    from app.services.buyplan_workflow import bulk_edit_buy_plan_lines

    plan = _plan(db_session, test_requisition, status=BuyPlanStatus.DRAFT.value)
    requirement = _requirement_of(db_session, test_requisition)
    free_offer = _offer(db_session, test_requisition, test_user, requirement_id=requirement.id, unit_price=0.0)

    payload = [{"requirement_id": requirement.id, "offer_id": free_offer.id, "quantity": 100, "unit_sell": 5.00}]
    plan = bulk_edit_buy_plan_lines(plan.id, payload, test_user, db_session)
    db_session.commit()
    db_session.refresh(plan)

    new_line = plan.lines[0]
    assert float(new_line.unit_cost) == 0.0  # a real $0 cost, NOT None
    assert float(plan.total_cost) == 0.0
    assert float(plan.total_revenue) == 500.0  # 5.00 * 100


def test_bulk_edit_vendor_change_to_zero_price_offer_keeps_unit_cost_zero(db_session, test_user, test_requisition):
    from app.services.buyplan_workflow import bulk_edit_buy_plan_lines

    plan = _plan(db_session, test_requisition, status=BuyPlanStatus.DRAFT.value)
    requirement = _requirement_of(db_session, test_requisition)
    line = _line(db_session, plan, quantity=100, unit_cost=1.00, unit_sell=2.00, requirement_id=requirement.id)
    free_offer = _offer(db_session, test_requisition, test_user, requirement_id=requirement.id, unit_price=0.0)

    bulk_edit_buy_plan_lines(plan.id, [{"line_id": line.id, "offer_id": free_offer.id}], test_user, db_session)
    db_session.commit()
    db_session.refresh(line)

    assert line.offer_id == free_offer.id
    assert float(line.unit_cost) == 0.0


def test_bulk_edit_vendor_change_recomputes_ai_score(db_session, test_user, test_requisition):
    from app.services.buyplan_scoring import score_offer
    from app.services.buyplan_workflow import bulk_edit_buy_plan_lines

    plan = _plan(db_session, test_requisition, status=BuyPlanStatus.DRAFT.value)
    requirement = _requirement_of(db_session, test_requisition)
    line = _line(
        db_session, plan, quantity=100, unit_cost=1.00, unit_sell=2.00, requirement_id=requirement.id, ai_score=0.0
    )
    new_offer = _offer(db_session, test_requisition, test_user, requirement_id=requirement.id, unit_price=0.30)

    bulk_edit_buy_plan_lines(plan.id, [{"line_id": line.id, "offer_id": new_offer.id}], test_user, db_session)
    db_session.commit()
    db_session.refresh(line)
    db_session.refresh(new_offer)

    expected_score = score_offer(new_offer, requirement, new_offer.vendor_card)
    assert line.ai_score == pytest.approx(expected_score)


# ══ Fix 4 — offer_id/quantity key-presence (explicit null is an error) ═


def test_bulk_edit_explicit_null_offer_id_rejected(db_session, test_user, test_requisition):
    from app.services.buyplan_workflow import bulk_edit_buy_plan_lines

    plan = _plan(db_session, test_requisition, status=BuyPlanStatus.DRAFT.value)
    line = _line(db_session, plan)

    with pytest.raises(ValueError, match="must not be null"):
        bulk_edit_buy_plan_lines(plan.id, [{"line_id": line.id, "offer_id": None}], test_user, db_session)


def test_bulk_edit_explicit_null_quantity_rejected(db_session, test_user, test_requisition):
    from app.services.buyplan_workflow import bulk_edit_buy_plan_lines

    plan = _plan(db_session, test_requisition, status=BuyPlanStatus.DRAFT.value)
    line = _line(db_session, plan)

    with pytest.raises(ValueError, match="must not be null"):
        bulk_edit_buy_plan_lines(plan.id, [{"line_id": line.id, "quantity": None}], test_user, db_session)


# ══ Fix 5 — known_line_ids coerced to set[int] at service depth ════════


def test_bulk_edit_known_line_ids_rejects_bool(db_session, test_user, test_requisition):
    from app.services.buyplan_workflow import bulk_edit_buy_plan_lines

    plan = _plan(db_session, test_requisition, status=BuyPlanStatus.DRAFT.value)
    line = _line(db_session, plan)

    with pytest.raises(ValueError, match="known_line_ids"):
        bulk_edit_buy_plan_lines(
            plan.id, [{"line_id": line.id, "unit_sell": 1.0}], test_user, db_session, known_line_ids=[True]
        )


def test_bulk_edit_known_line_ids_rejects_non_int(db_session, test_user, test_requisition):
    from app.services.buyplan_workflow import bulk_edit_buy_plan_lines

    plan = _plan(db_session, test_requisition, status=BuyPlanStatus.DRAFT.value)
    line = _line(db_session, plan)

    with pytest.raises(ValueError, match="known_line_ids"):
        bulk_edit_buy_plan_lines(
            plan.id, [{"line_id": line.id, "unit_sell": 1.0}], test_user, db_session, known_line_ids=["abc"]
        )


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


def test_route_bulk_edit_known_line_ids_wrong_type_400(client: TestClient, db_session, test_requisition):
    plan = _plan(db_session, test_requisition, status=BuyPlanStatus.DRAFT.value)
    line = _line(db_session, plan)

    payload = {"lines": [{"line_id": line.id, "unit_sell": 9.0}], "known_line_ids": ["not-an-int"]}
    resp = client.post(
        f"/v2/partials/buy-plans/{plan.id}/lines/bulk",
        data={"payload": json.dumps(payload)},
    )
    assert resp.status_code == 400


# ══ Fix 3 — auto-completion after removing the last open line ═════════


def test_route_bulk_edit_removing_last_open_line_completes_active_plan(
    client: TestClient, db_session, manager_user, test_requisition
):
    # ACTIVE-plan line edits are manager-only, so act as manager_user.
    plan = _plan(db_session, test_requisition, status=BuyPlanStatus.ACTIVE.value, so_status="approved")
    verified = _line(db_session, plan, quantity=100, status=BuyPlanLineStatus.VERIFIED.value)
    open_line = _line(db_session, plan, quantity=50, status=BuyPlanLineStatus.AWAITING_PO.value)

    # Omit open_line from both the payload and known_line_ids -> removed by omission,
    # leaving only the already-VERIFIED line -> plan should auto-complete.
    payload = {"lines": [], "known_line_ids": [verified.id, open_line.id]}
    with _acting_as(manager_user):
        resp = client.post(
            f"/v2/partials/buy-plans/{plan.id}/lines/bulk",
            data={"payload": json.dumps(payload)},
        )
    assert resp.status_code == 200
    db_session.expire_all()
    assert db_session.get(BuyPlan, plan.id).status == BuyPlanStatus.COMPLETED.value


def test_route_remove_line_completes_active_plan_when_last_open_line_removed(
    client: TestClient, db_session, manager_user, test_requisition
):
    plan = _plan(db_session, test_requisition, status=BuyPlanStatus.ACTIVE.value, so_status="approved")
    verified = _line(db_session, plan, quantity=100, status=BuyPlanLineStatus.VERIFIED.value)
    open_line = _line(db_session, plan, quantity=50, status=BuyPlanLineStatus.AWAITING_PO.value)

    with _acting_as(manager_user):
        resp = client.post(f"/v2/partials/buy-plans/{plan.id}/lines/{open_line.id}/remove")
    assert resp.status_code == 200
    db_session.expire_all()
    assert db_session.get(BuyPlan, plan.id).status == BuyPlanStatus.COMPLETED.value
    assert db_session.get(BuyPlanLine, verified.id) is not None
