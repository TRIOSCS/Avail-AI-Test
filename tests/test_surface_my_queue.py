"""Route + render tests for the My Queue surface (Approvals rework Phase B).

Covers GET /v2/partials/buy-plans/my-queue → _render_my_queue_body → _surface_my_queue.html:
- 200 for a buyer / supervisor / sales fixture, with the correct per-role rows;
- ONE hero card + the calm header + live-count filter chips;
- the 3-band risk dot (rose / accent / brand) per kind;
- inline action rows (Approve / Verify) carry hx-target="#bp-hub-body", hx-push-url="false",
  and origin=my_queue; navigation rows render a "{action} →" hint + whole-row detail link;
- the empty / all-caught-up state.

Reuses the buy-plan builders from tests/test_buyplan_hub_supervise.py and the grant/ops
helpers from tests/test_my_queue.py, plus conftest fixtures (client, db_session, test_user,
sales_user, manager_user, test_quote, test_requisition).

Depends on: app/routers/htmx/buy_plans (my-queue lens dispatch),
            app/templates/htmx/partials/approvals/_surface_my_queue.html.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.constants import BuyPlanLineStatus, BuyPlanStatus
from app.models.auth import User
from tests.test_buyplan_hub_supervise import _make_line, _make_plan
from tests.test_my_queue import _grant

MY_QUEUE_URL = "/v2/partials/buy-plans/my-queue"


@contextmanager
def _acting_as(user: User):
    """Override require_user for the block (the my-queue tab route is require_user-
    gated)."""
    from app.dependencies import require_user
    from app.main import app

    app.dependency_overrides[require_user] = lambda: user
    try:
        yield
    finally:
        app.dependency_overrides.pop(require_user, None)


# ── Empty / all-caught-up ──────────────────────────────────────────────────


def test_my_queue_surface_empty_state(client: TestClient):
    """A buyer with nothing to do gets 200 + the all-caught-up copy and no hero rows."""
    resp = client.get(MY_QUEUE_URL)
    assert resp.status_code == 200
    body = resp.text
    assert "You're all caught up. Nothing needs you right now." in body
    # No queue rows → no risk-band dots rendered.
    assert "bg-rose-500" not in body
    assert "bg-accent-500" not in body


# ── Buyer: navigation rows (halted + cut_po) ───────────────────────────────


def test_my_queue_surface_buyer_nav_rows(
    client: TestClient, db_session: Session, test_user: User, test_quote, test_requisition
):
    """A buyer's halted (At-risk/rose) + cut_po (Routine/brand) rows render as ONE hero
    card with the calm header, chips, the right dots, and whole-row detail links."""
    # Owner-scope halted: submitted_by the buyer so it surfaces for them.
    _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.HALTED,
        submitted_by_id=test_user.id,
    )
    active = _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.ACTIVE,
    )
    _make_line(
        db_session,
        buy_plan_id=active.id,
        buyer_id=test_user.id,
        status=BuyPlanLineStatus.AWAITING_PO,
    )

    resp = client.get(MY_QUEUE_URL)
    assert resp.status_code == 200
    body = resp.text

    # Calm header + chips
    assert "need" in body and "you" in body  # "{n} items need you"
    assert "in play" in body  # money subline
    assert "All (" in body  # the All chip with a live count
    # Exactly ONE hero card (the divide-y container).
    assert body.count("divide-y divide-line-subtle") == 1
    # 3-band dots: halted = rose (At-risk); cut_po = brand (Routine).
    assert "bg-rose-500" in body
    assert "bg-brand-400" in body
    # Navigation rows: a whole-row detail link + the "{action} →" hint, no inline POST.
    assert f'hx-get="/v2/partials/buy-plans/{active.id}"' in body
    assert "Cut PO →" in body
    assert 'name="origin" value="my_queue"' not in body


# ── Buyer: inline action rows (Approve + Verify) ───────────────────────────


def test_my_queue_surface_inline_actions(
    client: TestClient, db_session: Session, test_user: User, test_quote, test_requisition
):
    """plan_approve (Decide/accent) + po_verify (Routine/brand) render inline action
    forms that target #bp-hub-body, set hx-push-url="false", and carry
    origin=my_queue."""
    _grant(db_session, test_user, can_approve_buy_plans=True, can_approve_purchase_orders=True)

    pending = _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.PENDING,
    )
    active = _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.ACTIVE,
    )
    verify_line = _make_line(
        db_session,
        buy_plan_id=active.id,
        status=BuyPlanLineStatus.PENDING_VERIFY,
    )

    resp = client.get(MY_QUEUE_URL)
    assert resp.status_code == 200
    body = resp.text

    # Decide dot (plan_approve) + Routine dot (po_verify).
    assert "bg-accent-500" in body
    assert "bg-brand-400" in body
    # Inline action contract — R6: every inline hx-post sets push-url=false + explicit target.
    assert 'hx-target="#bp-hub-body"' in body
    assert 'hx-push-url="false"' in body
    assert 'name="origin" value="my_queue"' in body
    # The two inline verbs + the approve route + verify route.
    assert ">Approve</button>" in body
    assert ">Verify</button>" in body
    assert f'hx-post="/v2/partials/buy-plans/{pending.id}/approve"' in body
    assert f'hx-post="/v2/partials/buy-plans/{active.id}/lines/{verify_line.id}/verify-po"' in body


# ── Buyer gating: no approval rights → no inline rows ───────────────────────


def test_my_queue_surface_buyer_without_rights_sees_no_approve(
    client: TestClient, db_session: Session, test_quote, test_requisition
):
    """A plain buyer (no approve rights) never sees a plan_approve row for a pending
    plan."""
    _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.PENDING,
    )
    resp = client.get(MY_QUEUE_URL)
    assert resp.status_code == 200
    # Pending plan routes to approvers only → no Decide dot, no approve form.
    assert "bg-accent-500" not in resp.text
    assert ">Approve</button>" not in resp.text


# ── Supervisor: sees all halted ────────────────────────────────────────────


def test_my_queue_surface_supervisor_sees_all_halted(
    client: TestClient, db_session: Session, manager_user: User, test_user: User, test_quote, test_requisition
):
    """A supervisor (manager) hitting My Queue sees a halted plan they don't own (rose
    dot)."""
    _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.HALTED,
        submitted_by_id=test_user.id,  # owned by the buyer, NOT the manager
    )
    with _acting_as(manager_user):
        resp = client.get(MY_QUEUE_URL)
    assert resp.status_code == 200
    assert "bg-rose-500" in resp.text  # At-risk halted dot
    assert "Halted (" in resp.text  # the Halted chip with a live count


# ── Sales: only owner-scoped draft, navigation only ────────────────────────


def test_my_queue_surface_sales_sees_own_draft(
    client: TestClient, db_session: Session, sales_user: User, test_quote, test_requisition
):
    """A sales user sees only their own DRAFT (plan_draft) as a navigation row ("Submit
    →")."""
    draft = _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.DRAFT,
        submitted_by_id=sales_user.id,
    )
    with _acting_as(sales_user):
        resp = client.get(MY_QUEUE_URL)
    assert resp.status_code == 200
    body = resp.text
    assert "Submit →" in body
    assert f'hx-get="/v2/partials/buy-plans/{draft.id}"' in body
    # Sales is not a PO-cutter/approver → no inline action forms.
    assert 'name="origin" value="my_queue"' not in body


# ── Flagged triage row (supervisor, Phase F-1 gap-fill) ────────────────────


def test_my_queue_surface_flagged_row_supervisor(
    client: TestClient, db_session: Session, manager_user: User, test_user: User, test_quote, test_requisition
):
    """A supervisor's My Queue shows a flagged (rose At-risk) row with the issue reason
    and a Flagged filter chip."""
    plan = _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.ACTIVE,
    )
    line = _make_line(
        db_session,
        buy_plan_id=plan.id,
        buyer_id=test_user.id,
        status=BuyPlanLineStatus.ISSUE,
        issue_type="sold_out",
    )
    line.issue_note = "Vendor sold the lot"
    db_session.flush()

    with _acting_as(manager_user):
        resp = client.get(MY_QUEUE_URL)
    assert resp.status_code == 200
    body = resp.text
    assert "Flagged (" in body  # the Flagged filter chip with a live count
    assert "Vendor sold the lot" in body  # the issue reason surfaced (rose)
    assert "bg-rose-500" in body  # At-risk band dot
    # A flagged row is a whole-row link to detail (no inline action form).
    assert f'hx-get="/v2/partials/buy-plans/{plan.id}"' in body


# ── Prepay inline action (Phase F-1 gap-fill) ──────────────────────────────


def test_my_queue_surface_prepay_inline_action(
    client: TestClient, db_session: Session, manager_user: User, test_quote, test_requisition
):
    """A routed prepay row renders an inline Approve form + a Reject reveal posting the
    prepay decide route into #bp-hub-body (push-url off)."""
    from tests.test_my_queue import _grant, _make_prepay_request

    plan = _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.ACTIVE,
        total_cost="5000.00",
    )
    _grant(db_session, manager_user, can_approve_prepayments=True)
    ar, _pp = _make_prepay_request(db_session, recipient=manager_user, buy_plan_id=plan.id, amount="2500.00")

    with _acting_as(manager_user):
        resp = client.get(MY_QUEUE_URL)
    assert resp.status_code == 200
    body = resp.text
    assert f"/v2/partials/approvals/prepay-requests/{ar.id}/decide" in body
    assert 'hx-target="#bp-hub-body"' in body
    assert 'hx-push-url="false"' in body
    assert "rejectOpen" in body  # the reject reveal toggle
    # The authorised amount is currency-aware with cents (parity with the tab, finding #10).
    assert "USD 2,500.00 prepay" in body
    # A Review affordance drills to the plan detail — never a lower-fidelity one-click cash OK.
    assert f'hx-get="/v2/partials/buy-plans/{plan.id}"' in body
    assert ">Review<" in body


def test_my_queue_surface_prepay_shows_test_report_warning_and_fields(
    client: TestClient, db_session: Session, manager_user: User, test_quote, test_requisition, test_vendor_card
):
    """A My-Queue prepay row surfaces the decision-critical fields — the LOUD test-
    report warning when it was NOT sent to management, the payment method, PO#,
    beneficiary (legal name), remarks — reaching parity with the Prepayment tab (finding
    #10)."""
    from app.constants import PaymentMethod
    from app.models.buy_plan import BuyPlanLine
    from tests.test_my_queue import _grant, _make_prepay_request

    plan = _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.ACTIVE,
        total_cost="5000.00",
    )
    line = BuyPlanLine(buy_plan_id=plan.id, quantity=10, unit_cost="200.00", po_number="PO-4471")
    db_session.add(line)
    db_session.flush()
    test_vendor_card.legal_name = "Northwind Components LLC"
    _grant(db_session, manager_user, can_approve_prepayments=True)
    ar, pp = _make_prepay_request(
        db_session,
        recipient=manager_user,
        buy_plan_id=plan.id,
        amount="2500.00",
        vendor_card_id=test_vendor_card.id,
    )
    pp.buy_plan_line_id = line.id
    pp.payment_method = PaymentMethod.WIRE
    pp.test_report_sent = False
    pp.buyer_remarks = "Vendor requires 50% upfront before build slot"
    db_session.commit()

    with _acting_as(manager_user):
        body = client.get(MY_QUEUE_URL).text
    assert "Test report NOT sent to management" in body  # the loud amber warning
    assert "Northwind Components LLC" in body  # legal beneficiary
    assert "PO-4471" in body
    assert "wire" in body  # payment method (uppercased in-view)
    assert "Vendor requires 50% upfront" in body  # buyer remarks


# ── Header avg-margin + kicked-back surfacing (Phase F-1 gap-fill) ─────────


def test_my_queue_surface_header_shows_avg_margin(
    client: TestClient, db_session: Session, test_user: User, test_quote, test_requisition
):
    """The My Queue header money subline appends the open-book avg margin."""
    plan = _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.ACTIVE,
        total_cost="5000.00",
        total_margin_pct=25,
        approved_at=datetime.now(timezone.utc),
    )
    _make_line(db_session, buy_plan_id=plan.id, buyer_id=test_user.id, status=BuyPlanLineStatus.AWAITING_PO)

    resp = client.get(MY_QUEUE_URL)
    assert resp.status_code == 200
    assert "avg margin" in resp.text


def test_my_queue_surface_kicked_back_surfacing(
    client: TestClient, db_session: Session, test_user: User, test_quote, test_requisition
):
    """A kicked-back cut_po row surfaces a rose 'kicked back' header line, the rejection
    note, and a rose-tinted row."""
    plan = _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.ACTIVE,
        approved_at=datetime.now(timezone.utc),
    )
    line = _make_line(db_session, buy_plan_id=plan.id, buyer_id=test_user.id, status=BuyPlanLineStatus.AWAITING_PO)
    line.po_rejection_note = "Wrong vendor — re-cut to Arrow"
    db_session.flush()

    resp = client.get(MY_QUEUE_URL)
    assert resp.status_code == 200
    body = resp.text
    assert "kicked back" in body
    assert "Wrong vendor — re-cut to Arrow" in body
    assert "bg-rose-50" in body  # the row gets a rose tint
