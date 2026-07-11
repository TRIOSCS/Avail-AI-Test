"""tests/test_buy_plan_epic.py — Buy-Plan Epic (G–K).

Covers the money-governing buy-plan/sales-order workflow additions:
  G  buy_plan_tracking_rows exposes customer / Revenue / Sales-GP ($ + %) / part count.
  H  the Approvals Buy-Plans tab surfaces a "New Buy Plan" entry point that reuses the
     existing Sales-Order origination flow (?new=1 lands the hub on the picker).
  I  editable buy-plan lines (add / edit / remove) recompute the header rollups, gated by
     the role×status matrix (pre-approval owner-or-manager; post-approval manager-only;
     terminal locked) — enforced server-side.
  J  the active Sales Order number is editable at any non-terminal status; persists.
  K  Cancel (owner/manager, reason required), Halt (manager, reason required, stored on
     so_rejection_note), Resume (manager-only, HALTED→ACTIVE preserving the halt audit).

Called by: pytest
Depends on: conftest fixtures (client, test_user, sales_user, manager_user, admin_user,
    test_requisition, test_offer), FastAPI TestClient.
"""

import os

os.environ["TESTING"] = "1"

from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.constants import BuyPlanLineStatus, BuyPlanStatus
from app.dependencies import require_user
from app.main import app
from app.models import Requirement, Requisition, User
from app.models.buy_plan import BuyPlan, BuyPlanLine

# ── Helpers ──────────────────────────────────────────────────────────


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
    """A requisition (owned by *owner*) with one requirement."""
    req = Requisition(
        name="REQ-EPIC",
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


# ══ G — list fields ══════════════════════════════════════════════════


def test_tracking_row_exposes_customer_revenue_gp_and_part_count(db_session, test_user):
    from app.services.approvals.queue import buy_plan_tracking_rows

    req = _req(db_session, test_user, customer="Globex Corp")
    plan = _plan(
        db_session,
        req,
        sales_order_number="TS-777",
        total_cost=100.00,
        total_revenue=250.00,
        total_margin_pct=60.00,
    )
    _line(db_session, plan)
    _line(db_session, plan)

    rows = buy_plan_tracking_rows(db_session, test_user, scope="all")
    row = next(r for r in rows if r.plan_id == plan.id)

    assert row.customer_name == "Globex Corp"
    assert row.so_number == "TS-777"
    assert float(row.amount) == 100.0  # cost
    assert float(row.revenue) == 250.0
    assert float(row.gross_profit) == 150.0  # revenue − cost
    assert float(row.margin_pct) == 60.0
    assert row.part_count == 2


# ══ H — Create Buy Plan entry point ══════════════════════════════════


def test_new_param_lands_hub_on_create_flow(client: TestClient):
    resp = client.get("/v2/partials/buy-plans?new=1")
    assert resp.status_code == 200
    # The hub body lazy-loads the Sales-Order origination picker (the create flow) directly.
    assert "/v2/partials/buy-plans/sales-orders/new" in resp.text


def test_approvals_buy_plan_tab_has_new_buy_plan_link(client: TestClient):
    resp = client.get("/v2/partials/approvals/buy-plan")
    assert resp.status_code == 200
    assert "New Buy Plan" in resp.text
    assert "buy-plans?new=1" in resp.text


# ══ I — role×status edit gate ════════════════════════════════════════


def test_gate_pre_approval_owner_and_manager_can_edit(db_session, sales_user, manager_user, admin_user):
    from app.services.buyplan_workflow import can_edit_buy_plan_lines

    req = _req(db_session, sales_user)
    for status in (BuyPlanStatus.DRAFT.value, BuyPlanStatus.PENDING.value):
        plan = _plan(db_session, req, status=status)
        assert can_edit_buy_plan_lines(sales_user, plan) is True  # owner
        assert can_edit_buy_plan_lines(manager_user, plan) is True
        assert can_edit_buy_plan_lines(admin_user, plan) is True


def test_gate_pre_approval_non_owner_non_manager_cannot_edit(db_session, sales_user, test_user):
    from app.services.buyplan_workflow import can_edit_buy_plan_lines

    req = _req(db_session, sales_user)  # owned by sales_user
    plan = _plan(db_session, req, status=BuyPlanStatus.DRAFT.value)
    # test_user is a buyer who does NOT own this plan → not sales-or-manager.
    assert can_edit_buy_plan_lines(test_user, plan) is False


@pytest.mark.parametrize(
    "status", [BuyPlanStatus.ACTIVE.value, BuyPlanStatus.INBOUND.value, BuyPlanStatus.HALTED.value]
)
def test_gate_post_approval_is_manager_only(db_session, sales_user, manager_user, status):
    from app.services.buyplan_workflow import can_edit_buy_plan_lines

    req = _req(db_session, sales_user)
    plan = _plan(db_session, req, status=status)
    assert can_edit_buy_plan_lines(sales_user, plan) is False  # owner but post-approval
    assert can_edit_buy_plan_lines(manager_user, plan) is True


@pytest.mark.parametrize("status", [BuyPlanStatus.COMPLETED.value, BuyPlanStatus.CANCELLED.value])
def test_gate_terminal_locked_for_everyone(db_session, sales_user, manager_user, admin_user, status):
    from app.services.buyplan_workflow import can_edit_buy_plan_lines

    req = _req(db_session, sales_user)
    plan = _plan(db_session, req, status=status)
    assert can_edit_buy_plan_lines(sales_user, plan) is False
    assert can_edit_buy_plan_lines(manager_user, plan) is False
    assert can_edit_buy_plan_lines(admin_user, plan) is False


# ══ I — add / edit / remove recompute the header ═════════════════════


def test_add_line_recomputes_header(db_session, test_user, test_requisition, test_offer):
    from app.services.buyplan_workflow import add_buy_plan_line

    plan = _plan(db_session, test_requisition, status=BuyPlanStatus.DRAFT.value)
    _line(db_session, plan, quantity=100, unit_cost=1.00, unit_sell=2.00)  # cost 100, rev 200
    requirement = _requirement_of(db_session, test_requisition)

    # test_offer: unit_price 0.50; add 1000 @ sell 0.50 → cost 500, rev 500.
    add_buy_plan_line(plan.id, requirement.id, test_offer.id, 1000, test_user, db_session, unit_sell=0.50)
    db_session.commit()
    db_session.refresh(plan)

    assert len(plan.lines) == 2
    assert float(plan.total_cost) == 600.0
    assert float(plan.total_revenue) == 700.0


def test_add_line_foreign_requirement_rejected(db_session, test_user, test_requisition, test_offer):
    from app.services.buyplan_workflow import add_buy_plan_line

    plan = _plan(db_session, test_requisition, status=BuyPlanStatus.DRAFT.value)
    other = _req(db_session, test_user)  # a different requisition
    foreign_req = _requirement_of(db_session, other)

    with pytest.raises(ValueError, match="requisition"):
        add_buy_plan_line(plan.id, foreign_req.id, test_offer.id, 10, test_user, db_session)


def test_edit_line_unit_sell_recomputes_margin(db_session, test_user, test_requisition):
    from app.services.buyplan_workflow import edit_buy_plan_line

    plan = _plan(db_session, test_requisition, status=BuyPlanStatus.DRAFT.value)
    line = _line(db_session, plan, quantity=100, unit_cost=1.00, unit_sell=2.00)

    edit_buy_plan_line(plan.id, line.id, test_user, db_session, unit_sell=5.00)
    db_session.commit()
    db_session.refresh(plan)
    db_session.refresh(line)

    assert float(line.unit_sell) == 5.00
    assert float(plan.total_revenue) == 500.0  # 5 * 100
    assert float(plan.total_margin_pct) == 80.0  # (500-100)/500


def test_remove_line_recomputes_header(db_session, test_user, test_requisition):
    from app.services.buyplan_workflow import remove_buy_plan_line

    plan = _plan(db_session, test_requisition, status=BuyPlanStatus.DRAFT.value)
    keep = _line(db_session, plan, quantity=100, unit_cost=1.00, unit_sell=2.00)  # cost 100 rev 200
    drop = _line(db_session, plan, quantity=50, unit_cost=4.00, unit_sell=6.00)  # cost 200 rev 300

    remove_buy_plan_line(plan.id, drop.id, test_user, db_session)
    db_session.commit()
    db_session.refresh(plan)

    assert [ln.id for ln in plan.lines] == [keep.id]
    assert float(plan.total_cost) == 100.0
    assert float(plan.total_revenue) == 200.0


def test_remove_cut_po_line_rejected(db_session, manager_user, test_requisition):
    from app.services.buyplan_workflow import remove_buy_plan_line

    plan = _plan(db_session, test_requisition, status=BuyPlanStatus.ACTIVE.value)
    line = _line(db_session, plan, status=BuyPlanLineStatus.PENDING_VERIFY.value)

    # A manager passes the post-approval edit gate but STILL cannot remove a cut-PO line.
    with pytest.raises(ValueError, match="cut PO|PO is cut"):
        remove_buy_plan_line(plan.id, line.id, manager_user, db_session)


# ── I — endpoint gate (HTTP 403/200) ─────────────────────────────────


def test_edit_endpoint_post_approval_sales_forbidden(client, db_session, sales_user):
    req = _req(db_session, sales_user)
    plan = _plan(db_session, req, status=BuyPlanStatus.ACTIVE.value)
    line = _line(db_session, plan)

    with _acting_as(sales_user):
        resp = client.post(
            f"/v2/partials/buy-plans/{plan.id}/lines/{line.id}/edit",
            data={"unit_sell": "9.00"},
        )
    assert resp.status_code == 403


def test_edit_endpoint_post_approval_manager_ok(client, db_session, sales_user, manager_user):
    req = _req(db_session, sales_user)
    plan = _plan(db_session, req, status=BuyPlanStatus.ACTIVE.value)
    line = _line(db_session, plan)

    with _acting_as(manager_user):
        resp = client.post(
            f"/v2/partials/buy-plans/{plan.id}/lines/{line.id}/edit",
            data={"unit_sell": "9.00"},
        )
    assert resp.status_code == 200
    db_session.expire_all()
    assert float(db_session.get(BuyPlanLine, line.id).unit_sell) == 9.00


def test_edit_endpoint_terminal_forbidden(client, db_session, sales_user, manager_user):
    req = _req(db_session, sales_user)
    plan = _plan(db_session, req, status=BuyPlanStatus.COMPLETED.value)
    line = _line(db_session, plan)

    with _acting_as(manager_user):
        resp = client.post(
            f"/v2/partials/buy-plans/{plan.id}/lines/{line.id}/edit",
            data={"unit_sell": "9.00"},
        )
    assert resp.status_code == 403


def test_detail_renders_editable_line_ui_for_editor(client, db_session, manager_user, test_requisition, test_offer):
    """Manager viewing an ACTIVE plan sees the whole-plan editable line UI (Edit plan
    toggle, the bulk-save endpoint reference, add-line/add-vendor affordances) render
    end-to-end without a Jinja error."""
    # Anchor the offer to the plan's requirement so it appears in the vendor picker + add form.
    requirement = _requirement_of(db_session, test_requisition)
    test_offer.requirement_id = requirement.id
    db_session.commit()

    plan = _plan(db_session, test_requisition, status=BuyPlanStatus.ACTIVE.value)
    _line(db_session, plan, offer_id=test_offer.id)

    with _acting_as(manager_user):
        resp = client.get(f"/v2/partials/buy-plans/{plan.id}")
    assert resp.status_code == 200
    body = resp.text
    assert f"buyPlanLinesEditor({plan.id}," in body  # whole-plan editor seeded with this plan id
    assert "Edit plan" in body  # whole-table edit toggle (replaces per-row Edit)
    assert "Save all" in body
    assert "+ Add line" in body
    assert "+ Add vendor" in body  # split-vendor add, one per part group
    assert test_offer.vendor_name in body  # offer surfaced in the vendor picker


# ══ J — Sales Order number ═══════════════════════════════════════════


def test_set_so_number_persists(db_session, test_user, test_requisition):
    from app.services.buyplan_workflow import set_sales_order_number

    plan = _plan(db_session, test_requisition, status=BuyPlanStatus.ACTIVE.value, sales_order_number=None)
    set_sales_order_number(plan.id, "TS00190738", test_user, db_session)
    db_session.commit()
    db_session.refresh(plan)
    assert plan.sales_order_number == "TS00190738"


@pytest.mark.parametrize("status", [BuyPlanStatus.COMPLETED.value, BuyPlanStatus.CANCELLED.value])
def test_set_so_number_rejected_on_terminal(db_session, test_user, test_requisition, status):
    from app.services.buyplan_workflow import set_sales_order_number

    plan = _plan(db_session, test_requisition, status=status)
    with pytest.raises(ValueError, match="Sales Order"):
        set_sales_order_number(plan.id, "TS-1", test_user, db_session)


def test_so_endpoint_owner_persists(client, db_session, test_user, test_requisition):
    plan = _plan(db_session, test_requisition, status=BuyPlanStatus.ACTIVE.value, sales_order_number=None)
    resp = client.post(f"/v2/partials/buy-plans/{plan.id}/so-number", data={"sales_order_number": "TS-999"})
    assert resp.status_code == 200
    db_session.expire_all()
    assert db_session.get(BuyPlan, plan.id).sales_order_number == "TS-999"


def test_so_endpoint_terminal_400(client, db_session, test_requisition):
    plan = _plan(db_session, test_requisition, status=BuyPlanStatus.COMPLETED.value)
    resp = client.post(f"/v2/partials/buy-plans/{plan.id}/so-number", data={"sales_order_number": "TS-1"})
    assert resp.status_code == 400


def test_so_endpoint_non_owner_non_manager_403(client, db_session, sales_user, test_user):
    # Plan owned by sales_user; the acting user (test_user, a buyer) is neither owner nor mgr.
    req = _req(db_session, sales_user)
    plan = _plan(db_session, req, status=BuyPlanStatus.ACTIVE.value)
    resp = client.post(f"/v2/partials/buy-plans/{plan.id}/so-number", data={"sales_order_number": "X"})
    assert resp.status_code == 403


# ══ K — Cancel / Halt / Resume ═══════════════════════════════════════


def test_cancel_blank_reason_400(client, db_session, test_user, test_requisition):
    plan = _plan(db_session, test_requisition, status=BuyPlanStatus.ACTIVE.value)
    resp = client.post(f"/v2/partials/buy-plans/{plan.id}/cancel", data={"reason": "   "})
    assert resp.status_code == 400
    db_session.expire_all()
    assert db_session.get(BuyPlan, plan.id).status == BuyPlanStatus.ACTIVE.value


def test_cancel_with_reason_cancels(client, db_session, test_user, test_requisition):
    plan = _plan(db_session, test_requisition, status=BuyPlanStatus.ACTIVE.value)
    resp = client.post(f"/v2/partials/buy-plans/{plan.id}/cancel", data={"reason": "customer pulled out"})
    assert resp.status_code == 200
    db_session.expire_all()
    fresh = db_session.get(BuyPlan, plan.id)
    assert fresh.status == BuyPlanStatus.CANCELLED.value
    assert fresh.cancellation_reason == "customer pulled out"


def test_cancel_non_owner_non_manager_403(client, db_session, sales_user):
    req = _req(db_session, sales_user)
    plan = _plan(db_session, req, status=BuyPlanStatus.ACTIVE.value)
    # acting user is test_user (buyer, non-owner, non-manager) via default client override.
    resp = client.post(f"/v2/partials/buy-plans/{plan.id}/cancel", data={"reason": "nope"})
    assert resp.status_code == 403


def test_halt_blank_reason_400(client, db_session, manager_user, test_requisition):
    plan = _plan(db_session, test_requisition, status=BuyPlanStatus.ACTIVE.value)
    with _acting_as(manager_user):
        resp = client.post(f"/v2/partials/buy-plans/{plan.id}/halt", data={"reason": ""})
    assert resp.status_code == 400


def test_halt_manager_only(client, db_session, sales_user, test_requisition):
    plan = _plan(db_session, test_requisition, status=BuyPlanStatus.ACTIVE.value)
    with _acting_as(sales_user):
        resp = client.post(f"/v2/partials/buy-plans/{plan.id}/halt", data={"reason": "stop"})
    assert resp.status_code == 403


def test_halt_with_reason_stored_on_so_rejection_note(client, db_session, manager_user, test_requisition):
    plan = _plan(db_session, test_requisition, status=BuyPlanStatus.ACTIVE.value)
    with _acting_as(manager_user):
        resp = client.post(f"/v2/partials/buy-plans/{plan.id}/halt", data={"reason": "supplier fraud"})
    assert resp.status_code == 200
    db_session.expire_all()
    fresh = db_session.get(BuyPlan, plan.id)
    assert fresh.status == BuyPlanStatus.HALTED.value
    assert fresh.so_rejection_note == "supplier fraud"
    assert fresh.halted_by_id == manager_user.id


def test_resume_manager_halted_to_active_preserves_audit(db_session, manager_user, test_requisition):
    from app.services.buyplan_workflow import halt_plan, resume_plan

    plan = _plan(db_session, test_requisition, status=BuyPlanStatus.ACTIVE.value)
    halt_plan(plan.id, manager_user, db_session, reason="pause")
    db_session.commit()
    db_session.refresh(plan)
    halted_by, halted_at = plan.halted_by_id, plan.halted_at
    assert plan.status == BuyPlanStatus.HALTED.value
    assert halted_by is not None and halted_at is not None

    resume_plan(plan.id, manager_user, db_session)
    db_session.commit()
    db_session.refresh(plan)

    assert plan.status == BuyPlanStatus.ACTIVE.value
    # Halt audit is PRESERVED (resume is not a reset).
    assert plan.halted_by_id == halted_by
    assert plan.halted_at == halted_at


def test_resume_non_manager_forbidden(db_session, sales_user, test_requisition):
    from app.services.buyplan_workflow import resume_plan

    plan = _plan(
        db_session,
        test_requisition,
        status=BuyPlanStatus.HALTED.value,
        halted_by_id=sales_user.id,
        halted_at=datetime.now(UTC),
    )
    with pytest.raises(PermissionError):
        resume_plan(plan.id, sales_user, db_session)


def test_resume_non_halted_rejected(db_session, manager_user, test_requisition):
    from app.services.buyplan_workflow import resume_plan

    plan = _plan(db_session, test_requisition, status=BuyPlanStatus.ACTIVE.value)
    with pytest.raises(ValueError, match="halted"):
        resume_plan(plan.id, manager_user, db_session)


def test_resume_endpoint_manager_activates(client, db_session, manager_user, test_requisition):
    plan = _plan(
        db_session,
        test_requisition,
        status=BuyPlanStatus.HALTED.value,
        halted_by_id=manager_user.id,
        halted_at=datetime.now(UTC),
    )
    with _acting_as(manager_user):
        resp = client.post(f"/v2/partials/buy-plans/{plan.id}/resume")
    assert resp.status_code == 200
    db_session.expire_all()
    assert db_session.get(BuyPlan, plan.id).status == BuyPlanStatus.ACTIVE.value
