"""test_kanban_render.py — the SO pane's PO kanban rendering (Workspace 3.3).

_pane_kanban.html via GET /v2/partials/approvals/plan/{id}/pane: the five spec-§6
lanes render (with empty states), risk cards show amount + payee and age on paid_at
(rose at 7d), COD-paid lines never enter the risk lane, cards are keyboard-navigable
tap targets swapping #aw-pane to the PO-line pane (explicit hx-target), the
Re-sourcing lane carries the claim button, eligible cards a Mark received action,
no drag anywhere, and lite (non-sourcing) plans render no kanban at all.

Called by: pytest
Depends on: conftest (db_session, test_user), tests.test_approvals_hub_tabs
            builders, tests.test_kanban_lanes._prepayment.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.constants import (
    BuyPlanLineStatus,
    BuyPlanStatus,
    PaymentMethod,
    PrepaymentStatus,
    SalesOrderType,
)
from app.database import get_db
from app.dependencies import require_user
from app.models import User
from tests.test_approvals_hub_tabs import _line, _plan, _req_quote
from tests.test_kanban_lanes import _prepayment

LANE_LABELS = ("Awaiting PO", "Pending approval", "Paid · awaiting delivery", "Approved", "Received")


@pytest.fixture()
def ws_client(db_session: Session, test_user: User):
    """TestClient authed as test_user (pane GETs are plain require_user)."""
    from app.main import app

    app.dependency_overrides[get_db] = lambda: (yield db_session)  # type: ignore[misc]
    app.dependency_overrides[require_user] = lambda: test_user
    try:
        with TestClient(app) as c:
            yield c
    finally:
        for dep in (get_db, require_user):
            app.dependency_overrides.pop(dep, None)


def _pane(client: TestClient, plan_id: int) -> str:
    r = client.get(f"/v2/partials/approvals/plan/{plan_id}/pane")
    assert r.status_code == 200
    return r.text


def _lane_slice(body: str, lane: str) -> str:
    """The HTML between this lane's column marker and the next one (or the end)."""
    start = body.index(f'data-lane="{lane}"')
    rest = body[start + 1 :]
    nxt = rest.find("data-lane=")
    return body[start : start + 1 + nxt] if nxt != -1 else body[start:]


# ── Lanes render ─────────────────────────────────────────────────────────


def test_active_sourcing_plan_renders_five_lanes_with_empty_states(
    ws_client: TestClient, db_session: Session, test_user: User
):
    req, q, rq = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    db_session.commit()

    body = _pane(ws_client, bp.id)
    for label in LANE_LABELS:
        assert label in body
    assert body.count("Nothing here yet") == 5  # every empty lane shows its state
    assert 'data-lane="resourcing"' not in body  # empty claim pool → no dead column
    assert "draggable" not in body  # no drag anywhere


def test_cards_land_in_their_lanes_with_face_fields(ws_client: TestClient, db_session: Session, test_user: User):
    req, q, rq = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    _line(db_session, bp, rq, test_user, status=BuyPlanLineStatus.AWAITING_PO.value)
    pending = _line(
        db_session,
        bp,
        rq,
        test_user,
        status=BuyPlanLineStatus.PENDING_VERIFY.value,
        po_number="PO-K1",
        po_confirmed_at=datetime.now(UTC),
        payment_method=PaymentMethod.ACH.value,
        estimated_ship_date=datetime(2026, 8, 1, tzinfo=UTC),
    )
    db_session.commit()

    body = _pane(ws_client, bp.id)
    slice_ = _lane_slice(body, "pending_approval")
    assert f"/v2/partials/approvals/po/{pending.id}/pane" in slice_  # the card lives here
    assert "PO-K1" in slice_  # PO# copy chip
    assert 'data-copy-value="PO-K1"' in slice_
    assert "ach" in slice_  # payment-method chip
    assert "ship Aug 01" in slice_  # est ship
    assert "line 2 of 2" in slice_  # sibling context
    assert "100 × $1.0000" in slice_  # qty × unit cost


# ── Risk lane ────────────────────────────────────────────────────────────


def test_risk_card_shows_amount_payee_and_red_aging(ws_client: TestClient, db_session: Session, test_user: User):
    req, q, rq = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    line = _line(
        db_session,
        bp,
        rq,
        test_user,
        status=BuyPlanLineStatus.VERIFIED.value,
        po_number="PO-RISK",
        po_confirmed_at=datetime.now(UTC),
        po_verified_at=datetime.now(UTC),
        payment_method=PaymentMethod.WIRE.value,
    )
    _prepayment(
        db_session,
        bp,
        line,
        test_user,
        status=PrepaymentStatus.PAID.value,
        paid_at=datetime.now(UTC) - timedelta(days=8),
        vendor_name="WireVendor Inc",
        total_incl_fees=2500,
    )
    db_session.commit()

    body = _pane(ws_client, bp.id)
    risk = _lane_slice(body, "paid_awaiting_delivery")
    assert "$2,500.00 paid" in risk  # amount on the card face
    assert "to WireVendor Inc" in risk  # payee on the card face
    assert "bg-rose-50" in risk  # 8 days past paid_at → red aging chip
    approved = _lane_slice(body, "approved")
    assert f"/po/{line.id}/pane" not in approved  # risk outranks verified


def test_risk_card_fresh_paid_ages_green(ws_client: TestClient, db_session: Session, test_user: User):
    req, q, rq = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    line = _line(
        db_session,
        bp,
        rq,
        test_user,
        status=BuyPlanLineStatus.VERIFIED.value,
        po_number="PO-FRESH",
        po_confirmed_at=datetime.now(UTC),
        po_verified_at=datetime.now(UTC),
        payment_method=PaymentMethod.CC.value,
    )
    _prepayment(db_session, bp, line, test_user, status=PrepaymentStatus.PAID.value)
    db_session.commit()

    risk = _lane_slice(_pane(ws_client, bp.id), "paid_awaiting_delivery")
    assert "bg-emerald-50" in risk  # paid today → green


def test_cod_paid_line_excluded_from_risk_lane(ws_client: TestClient, db_session: Session, test_user: User):
    req, q, rq = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    line = _line(
        db_session,
        bp,
        rq,
        test_user,
        status=BuyPlanLineStatus.VERIFIED.value,
        po_number="PO-COD",
        po_confirmed_at=datetime.now(UTC),
        po_verified_at=datetime.now(UTC),
        payment_method=PaymentMethod.COD.value,
    )
    _prepayment(db_session, bp, line, test_user, status=PrepaymentStatus.PAID.value)
    db_session.commit()

    body = _pane(ws_client, bp.id)
    risk = _lane_slice(body, "paid_awaiting_delivery")
    assert f"/po/{line.id}/pane" not in risk  # COD never enters the risk lane
    assert "Nothing here yet" in risk
    approved = _lane_slice(body, "approved")
    assert f"/po/{line.id}/pane" in approved  # defensive: it stays a plain Approved card


# ── Tap targets + actions ────────────────────────────────────────────────


def test_card_is_keyboard_navigable_tap_target_with_explicit_hx_target(
    ws_client: TestClient, db_session: Session, test_user: User
):
    req, q, rq = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    line = _line(db_session, bp, rq, test_user, status=BuyPlanLineStatus.AWAITING_PO.value)
    db_session.commit()

    slice_ = _lane_slice(_pane(ws_client, bp.id), "awaiting_po")
    assert f'hx-get="/v2/partials/approvals/po/{line.id}/pane"' in slice_
    assert 'hx-target="#aw-pane"' in slice_  # explicit — never the inherited target
    assert 'role="link"' in slice_ and 'tabindex="0"' in slice_


def test_resourcing_lane_card_carries_claim_button(ws_client: TestClient, db_session: Session, test_user: User):
    req, q, rq = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    line = _line(db_session, bp, rq, test_user, status=BuyPlanLineStatus.RESOURCING.value, buyer_id=None)
    db_session.commit()

    body = _pane(ws_client, bp.id)
    assert 'data-lane="resourcing"' in body  # the lane appears once populated
    slice_ = _lane_slice(body, "resourcing")
    assert f"/v2/partials/buy-plans/{bp.id}/lines/{line.id}/claim" in slice_
    assert "Claim this line" in slice_


def test_verified_card_offers_mark_received_pending_does_not(
    ws_client: TestClient, db_session: Session, test_user: User
):
    req, q, rq = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    verified = _line(
        db_session,
        bp,
        rq,
        test_user,
        status=BuyPlanLineStatus.VERIFIED.value,
        po_number="PO-V",
        po_confirmed_at=datetime.now(UTC),
        po_verified_at=datetime.now(UTC),
    )
    _line(
        db_session,
        bp,
        rq,
        test_user,
        status=BuyPlanLineStatus.PENDING_VERIFY.value,
        po_number="PO-P",
        po_confirmed_at=datetime.now(UTC),
    )
    db_session.commit()

    body = _pane(ws_client, bp.id)
    approved = _lane_slice(body, "approved")
    assert f"/lines/{verified.id}/receive" in approved
    assert "Mark received" in approved
    pending = _lane_slice(body, "pending_approval")
    assert "/receive" not in pending  # not eligible before approve


def test_received_card_shows_no_receive_action(ws_client: TestClient, db_session: Session, test_user: User):
    req, q, rq = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    _line(
        db_session,
        bp,
        rq,
        test_user,
        status=BuyPlanLineStatus.VERIFIED.value,
        po_number="PO-DONE",
        po_confirmed_at=datetime.now(UTC),
        po_verified_at=datetime.now(UTC),
        received_at=datetime.now(UTC),
        received_by_id=test_user.id,
    )
    db_session.commit()

    received = _lane_slice(_pane(ws_client, bp.id), "received")
    assert "PO-DONE" in received
    assert "/receive" not in received  # idempotent UI — no re-mark


# ── No kanban outside its home ───────────────────────────────────────────


def test_lite_plan_renders_no_kanban(ws_client: TestClient, db_session: Session, test_user: User):
    req, q, rq = _req_quote(db_session, test_user)
    bp = _plan(
        db_session,
        req,
        q,
        status=BuyPlanStatus.ACTIVE.value,
        order_type=SalesOrderType.COMPS.value,
    )
    db_session.commit()

    body = _pane(ws_client, bp.id)
    assert "data-lane=" not in body
    assert "Awaiting PO" not in body  # lite plans keep no kanban (spec §8)


def test_pending_sourcing_plan_renders_no_kanban_yet(ws_client: TestClient, db_session: Session, test_user: User):
    req, q, rq = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.PENDING.value)
    db_session.commit()

    body = _pane(ws_client, bp.id)
    assert "data-lane=" not in body  # the board is an active/inbound centerpiece only
