"""test_kanban_lanes.py — kanban lane placement + board assembly (Workspace 3.1).

Full truth table for kanban_lane's precedence (cancelled hidden; resourcing >
received > paid-risk > verified > pending_verify > awaiting_po/issue; COD never
enters the risk lane) and build_kanban's card DTOs (prepay badge amount/payee,
paid_at risk aging, edited-by-manager marker, note/file counts, line N of M,
conditional Re-sourcing lane).

Called by: pytest
Depends on: conftest (db_session, test_user), tests.test_approvals_hub_tabs
            builders, app.services.kanban_lanes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from app.constants import (
    BuyPlanLineStatus,
    BuyPlanStatus,
    KanbanLane,
    PaymentMethod,
    PrepaymentStatus,
    UserRole,
)
from app.models import User
from app.models.buy_plan import BuyPlanAttachment
from app.models.quality_plan import Prepayment, QualityPlan
from app.services.kanban_lanes import LANE_ORDER, build_kanban, kanban_lane
from tests.test_approvals_hub_tabs import _line, _plan, _req_quote

AW = BuyPlanLineStatus.AWAITING_PO.value
PV = BuyPlanLineStatus.PENDING_VERIFY.value
VF = BuyPlanLineStatus.VERIFIED.value


# ── kanban_lane truth table ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("line_status", "prepay", "method", "received", "expected"),
    [
        # 1) cancelled → hidden, regardless of anything else
        ("cancelled", None, None, False, None),
        ("cancelled", "paid", "wire", False, None),
        ("cancelled", "paid", "wire", True, None),
        # 2) resourcing → its own lane, outranking prepay and received
        ("resourcing", None, None, False, KanbanLane.RESOURCING),
        ("resourcing", "paid", "wire", False, KanbanLane.RESOURCING),
        # 3) received outranks everything live — paid+received is NOT a risk
        (VF, "paid", "wire", True, KanbanLane.RECEIVED),
        (VF, None, None, True, KanbanLane.RECEIVED),
        (PV, "paid", "ach", True, KanbanLane.RECEIVED),
        # 4) the risk lane: prepay PAID + any advance method, outranks verified
        (VF, "paid", "wire", False, KanbanLane.PAID_AWAITING_DELIVERY),
        (VF, "paid", "paypal", False, KanbanLane.PAID_AWAITING_DELIVERY),
        (VF, "paid", "cc", False, KanbanLane.PAID_AWAITING_DELIVERY),
        (VF, "paid", "ach", False, KanbanLane.PAID_AWAITING_DELIVERY),
        (PV, "paid", "wire", False, KanbanLane.PAID_AWAITING_DELIVERY),
        # a line missing a payment_method entirely still risks (money went out)
        (VF, "paid", None, False, KanbanLane.PAID_AWAITING_DELIVERY),
        # 4b) COD never enters the risk lane (defensive — nothing paid in advance)
        (VF, "paid", "cod", False, KanbanLane.APPROVED),
        (PV, "paid", "cod", False, KanbanLane.PENDING_APPROVAL),
        # non-paid prepay states are badges, never lane movers
        (VF, "approved", "wire", False, KanbanLane.APPROVED),
        (PV, "requested", "wire", False, KanbanLane.PENDING_APPROVAL),
        # 5-7) the plain lifecycle fall-through
        (VF, None, None, False, KanbanLane.APPROVED),
        (PV, None, None, False, KanbanLane.PENDING_APPROVAL),
        (AW, None, None, False, KanbanLane.AWAITING_PO),
        # issue stays in Awaiting PO (badge on the card, not a column)
        ("issue", None, None, False, KanbanLane.AWAITING_PO),
        ("issue", "requested", None, False, KanbanLane.AWAITING_PO),
    ],
)
def test_kanban_lane_truth_table(line_status, prepay, method, received, expected):
    assert (
        kanban_lane(
            line_status=line_status,
            prepay_status=prepay,
            payment_method=method,
            received=received,
        )
        is expected
    )


# ── build_kanban helpers ─────────────────────────────────────────────────


def _prepayment(db: Session, bp, line, user: User, *, status: str, **overrides) -> Prepayment:
    defaults = dict(
        buy_plan_id=bp.id,
        buy_plan_line_id=line.id,
        vendor_name="Acme Dist",
        total_incl_fees=1234.56,
        currency="USD",
        status=status,
        created_by_id=user.id,
    )
    if status == PrepaymentStatus.PAID.value:
        defaults["paid_at"] = datetime.now(UTC)
    defaults.update(overrides)
    pp = Prepayment(**defaults)
    db.add(pp)
    db.flush()
    return pp


def _lane_view(board, lane: KanbanLane):
    return next((v for v in board if v.lane is lane), None)


# ── build_kanban ─────────────────────────────────────────────────────────


def test_build_kanban_five_core_lanes_in_board_order(db_session: Session, test_user: User):
    req, q, rq = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    db_session.commit()

    board = build_kanban(db_session, bp)
    assert [v.lane for v in board] == list(LANE_ORDER[:-1])  # no empty Re-sourcing lane
    assert [v.label for v in board] == [
        "Awaiting PO",
        "Pending approval",
        "Paid · awaiting delivery",
        "Approved",
        "Received",
    ]
    assert all(v.cards == [] for v in board)  # empty lanes still render


def test_build_kanban_places_cards_and_hides_cancelled(db_session: Session, test_user: User):
    req, q, rq = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    l_await = _line(db_session, bp, rq, test_user, status=AW)
    l_pending = _line(db_session, bp, rq, test_user, status=PV, po_number="PO-1", po_confirmed_at=datetime.now(UTC))
    l_verified = _line(db_session, bp, rq, test_user, status=VF, po_number="PO-2", po_verified_at=datetime.now(UTC))
    l_cancelled = _line(db_session, bp, rq, test_user, status=BuyPlanLineStatus.CANCELLED.value)
    db_session.commit()

    board = build_kanban(db_session, bp)
    by_lane = {v.lane: [c.line_id for c in v.cards] for v in board}
    assert by_lane[KanbanLane.AWAITING_PO] == [l_await.id]
    assert by_lane[KanbanLane.PENDING_APPROVAL] == [l_pending.id]
    assert by_lane[KanbanLane.APPROVED] == [l_verified.id]
    all_ids = [cid for ids in by_lane.values() for cid in ids]
    assert l_cancelled.id not in all_ids  # cancelled is hidden entirely

    # Card face basics + line N of M (cancelled still counts toward M — it exists).
    card = board[0].cards[0]
    assert card.part == "LM317"
    assert card.vendor == "Acme Dist"
    assert card.quantity == 100
    assert card.unit_cost == 1.0
    assert (card.line_index, card.line_total) == (1, 4)


def test_build_kanban_paid_verified_lands_in_risk_lane_with_amount_payee(db_session: Session, test_user: User):
    req, q, rq = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    line = _line(
        db_session,
        bp,
        rq,
        test_user,
        status=VF,
        po_number="PO-RISK",
        payment_method=PaymentMethod.WIRE.value,
        po_verified_at=datetime.now(UTC),
    )
    paid_at = datetime.now(UTC) - timedelta(days=8)
    _prepayment(db_session, bp, line, test_user, status=PrepaymentStatus.PAID.value, paid_at=paid_at)
    db_session.commit()

    board = build_kanban(db_session, bp)
    risk = _lane_view(board, KanbanLane.PAID_AWAITING_DELIVERY)
    assert risk.is_risk
    assert [c.line_id for c in risk.cards] == [line.id]
    card = risk.cards[0]
    assert card.prepay_state == "paid"
    assert card.prepay_amount == 1234.56
    assert card.prepay_payee == "Acme Dist"
    assert card.paid_at == paid_at
    assert card.age_at == paid_at  # risk aging keys on paid_at, not line timestamps
    assert card.can_receive  # paid-risk is mark-received eligible
    assert _lane_view(board, KanbanLane.APPROVED).cards == []  # risk outranks verified


def test_build_kanban_paid_received_lands_in_received(db_session: Session, test_user: User):
    req, q, rq = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    received_at = datetime.now(UTC)
    line = _line(
        db_session,
        bp,
        rq,
        test_user,
        status=VF,
        po_number="PO-RCV",
        payment_method=PaymentMethod.WIRE.value,
        received_at=received_at,
        received_by_id=test_user.id,
    )
    _prepayment(db_session, bp, line, test_user, status=PrepaymentStatus.PAID.value)
    db_session.commit()

    board = build_kanban(db_session, bp)
    received = _lane_view(board, KanbanLane.RECEIVED)
    assert [c.line_id for c in received.cards] == [line.id]
    assert received.cards[0].age_at == received_at
    assert not received.cards[0].can_receive  # already received — idempotent UI
    assert _lane_view(board, KanbanLane.PAID_AWAITING_DELIVERY).cards == []


def test_build_kanban_cod_paid_defensive_stays_approved(db_session: Session, test_user: User):
    req, q, rq = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    line = _line(
        db_session,
        bp,
        rq,
        test_user,
        status=VF,
        po_number="PO-COD",
        payment_method=PaymentMethod.COD.value,
        po_verified_at=datetime.now(UTC),
    )
    _prepayment(db_session, bp, line, test_user, status=PrepaymentStatus.PAID.value)
    db_session.commit()

    board = build_kanban(db_session, bp)
    assert _lane_view(board, KanbanLane.PAID_AWAITING_DELIVERY).cards == []
    approved = _lane_view(board, KanbanLane.APPROVED)
    assert [c.line_id for c in approved.cards] == [line.id]
    assert approved.cards[0].prepay_state == "paid"  # the badge still tells the truth


def test_build_kanban_resourcing_lane_only_when_populated(db_session: Session, test_user: User):
    req, q, rq = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    line = _line(db_session, bp, rq, test_user, status=BuyPlanLineStatus.RESOURCING.value, buyer_id=None)
    # resourcing outranks a paid prepayment (precedence 2 over 4)
    _prepayment(db_session, bp, line, test_user, status=PrepaymentStatus.PAID.value)
    db_session.commit()

    board = build_kanban(db_session, bp)
    resourcing = _lane_view(board, KanbanLane.RESOURCING)
    assert resourcing is not None and [c.line_id for c in resourcing.cards] == [line.id]
    assert _lane_view(board, KanbanLane.PAID_AWAITING_DELIVERY).cards == []


def test_build_kanban_issue_line_carries_issue_badge_in_awaiting(db_session: Session, test_user: User):
    req, q, rq = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    line = _line(
        db_session,
        bp,
        rq,
        test_user,
        status=BuyPlanLineStatus.ISSUE.value,
        issue_type="sold_out",
        issue_note="vendor sold out",
    )
    db_session.commit()

    board = build_kanban(db_session, bp)
    awaiting = _lane_view(board, KanbanLane.AWAITING_PO)
    assert [c.line_id for c in awaiting.cards] == [line.id]
    assert awaiting.cards[0].issue_type == "sold_out"


def test_build_kanban_counts_marker_and_partial_ship(db_session: Session, test_user: User):
    from app.services.field_audit import FieldEdit, log_field_edits
    from app.services.workspace_notes import add_note

    req, q, rq = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    line = _line(db_session, bp, rq, test_user, status=PV, po_number="PO-9", po_confirmed_at=datetime.now(UTC))
    db_session.add(QualityPlan(buy_plan_id=bp.id, sales_authorized_ship_partial=True))
    add_note(db_session, user=test_user, body="watch this one", buy_plan_id=bp.id, buy_plan_line_id=line.id)
    db_session.add(BuyPlanAttachment(buy_plan_line_id=line.id, file_name="coc.pdf", uploaded_by_id=test_user.id))
    manager = User(email="mgr-kanban@t.co", name="Mgr", role=UserRole.MANAGER.value, is_active=True)
    db_session.add(manager)
    db_session.flush()
    log_field_edits(
        db_session,
        user=manager,
        buy_plan_id=bp.id,
        buy_plan_line_id=line.id,
        edits=[FieldEdit(field="quantity", old="100", new="90")],
        stage="verify",  # what the line-edit writers stamp for a PENDING_VERIFY save
    )
    db_session.commit()

    board = build_kanban(db_session, bp)
    card = _lane_view(board, KanbanLane.PENDING_APPROVAL).cards[0]
    assert card.note_count == 1
    assert card.file_count == 1
    assert card.edited_by_manager is True
    assert card.partial_ship is True
    assert card.age_at == line.po_confirmed_at  # pending approval ages on confirm time
