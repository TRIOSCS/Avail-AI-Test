"""test_mark_received.py — the manual mark-received action (Workspace 3.2).

mark_line_received: actor matrix (line buyer / manager / admin only), state gate
(VERIFIED or the paid-risk prepay state), idempotency (double-tap is a no-op),
the LINE_RECEIVED activity row with the line FK, kanban lane transition
(risk → Received), and the plan-status hands-off guarantee. Plus the
POST /lines/{id}/receive route (403 on a stranger, workspace pane re-render).

Called by: pytest
Depends on: conftest (db_session, test_user), tests.test_approvals_hub_tabs
            builders, tests.test_kanban_lanes._prepayment,
            app.services.buyplan_workflow.mark_line_received.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.constants import (
    ActivityType,
    BuyPlanLineStatus,
    BuyPlanStatus,
    KanbanLane,
    PaymentMethod,
    PrepaymentStatus,
    UserRole,
)
from app.database import get_db
from app.dependencies import require_user
from app.models import ActivityLog, User
from app.services.buyplan_workflow import mark_line_received
from app.services.kanban_lanes import build_kanban
from tests.test_approvals_hub_tabs import _line, _plan, _req_quote
from tests.test_kanban_lanes import _lane_view, _prepayment


def _user(db: Session, role: str, tag: str) -> User:
    u = User(email=f"{tag}@trioscs.com", name=tag.title(), role=role, is_active=True, created_at=datetime.now(UTC))
    db.add(u)
    db.flush()
    return u


def _verified_line(db: Session, bp, rq, buyer: User, **overrides):
    defaults = dict(
        status=BuyPlanLineStatus.VERIFIED.value,
        po_number="PO-RCV",
        po_confirmed_at=datetime.now(UTC),
        po_verified_at=datetime.now(UTC),
        po_verified_by_id=buyer.id,
    )
    defaults.update(overrides)
    return _line(db, bp, rq, buyer, **defaults)


@pytest.fixture()
def ws_client(db_session: Session, test_user: User):
    """TestClient authed as test_user (plain require_user — the receive route's gate is
    service-side)."""
    from app.main import app

    app.dependency_overrides[get_db] = lambda: (yield db_session)  # type: ignore[misc]
    app.dependency_overrides[require_user] = lambda: test_user
    try:
        with TestClient(app) as c:
            yield c
    finally:
        for dep in (get_db, require_user):
            app.dependency_overrides.pop(dep, None)


# ── Permission matrix ────────────────────────────────────────────────────


def test_line_buyer_can_mark_received(db_session: Session, test_user: User):
    req, q, rq = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    line = _verified_line(db_session, bp, rq, test_user)
    db_session.commit()

    updated = mark_line_received(bp.id, line.id, test_user, db_session)
    db_session.commit()

    assert updated.received_at is not None
    assert updated.received_by_id == test_user.id
    assert updated.is_received
    assert updated.status == BuyPlanLineStatus.VERIFIED.value  # status untouched


@pytest.mark.parametrize("role", [UserRole.MANAGER.value, UserRole.ADMIN.value])
def test_manager_and_admin_can_mark_received(db_session: Session, test_user: User, role: str):
    actor = _user(db_session, role, f"{role}-recv")
    req, q, rq = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    line = _verified_line(db_session, bp, rq, test_user)  # buyer is test_user, NOT actor
    db_session.commit()

    updated = mark_line_received(bp.id, line.id, actor, db_session)
    assert updated.received_by_id == actor.id


def test_stranger_cannot_mark_received(db_session: Session, test_user: User):
    stranger = _user(db_session, "buyer", "other-buyer-recv")
    req, q, rq = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    line = _verified_line(db_session, bp, rq, test_user)
    db_session.commit()

    with pytest.raises(PermissionError, match="buyer or a manager"):
        mark_line_received(bp.id, line.id, stranger, db_session)
    db_session.rollback()
    assert line.received_at is None


# ── State requirements ───────────────────────────────────────────────────


def test_paid_risk_line_can_be_received_before_verify(db_session: Session, test_user: User):
    """Prepay PAID → goods can arrive before the approver signs off (spec §6)."""
    req, q, rq = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    line = _line(
        db_session,
        bp,
        rq,
        test_user,
        status=BuyPlanLineStatus.PENDING_VERIFY.value,
        po_number="PO-PAID",
        po_confirmed_at=datetime.now(UTC),
        payment_method=PaymentMethod.WIRE.value,
    )
    _prepayment(db_session, bp, line, test_user, status=PrepaymentStatus.PAID.value)
    db_session.commit()

    # Before: the risk lane. After: Received (the lane transition).
    board = build_kanban(db_session, bp)
    assert [c.line_id for c in _lane_view(board, KanbanLane.PAID_AWAITING_DELIVERY).cards] == [line.id]

    mark_line_received(bp.id, line.id, test_user, db_session)
    db_session.commit()

    board = build_kanban(db_session, bp)
    assert _lane_view(board, KanbanLane.PAID_AWAITING_DELIVERY).cards == []
    assert [c.line_id for c in _lane_view(board, KanbanLane.RECEIVED).cards] == [line.id]
    assert line.status == BuyPlanLineStatus.PENDING_VERIFY.value  # verify still pending


@pytest.mark.parametrize(
    "status",
    [
        BuyPlanLineStatus.AWAITING_PO.value,
        BuyPlanLineStatus.PENDING_VERIFY.value,
        BuyPlanLineStatus.ISSUE.value,
        BuyPlanLineStatus.RESOURCING.value,
    ],
)
def test_unverified_unpaid_line_cannot_be_received(db_session: Session, test_user: User, status: str):
    req, q, rq = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    line = _line(db_session, bp, rq, test_user, status=status)
    db_session.commit()

    with pytest.raises(ValueError, match="verified.*or prepaid"):
        mark_line_received(bp.id, line.id, test_user, db_session)
    db_session.rollback()
    assert line.received_at is None


def test_cancelled_line_cannot_be_received_even_if_paid(db_session: Session, test_user: User):
    req, q, rq = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    line = _line(db_session, bp, rq, test_user, status=BuyPlanLineStatus.CANCELLED.value)
    _prepayment(db_session, bp, line, test_user, status=PrepaymentStatus.PAID.value)
    db_session.commit()

    with pytest.raises(ValueError, match="cancelled"):
        mark_line_received(bp.id, line.id, test_user, db_session)
    db_session.rollback()


def test_unknown_plan_or_line_raises(db_session: Session, test_user: User):
    req, q, rq = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    other_bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    line = _verified_line(db_session, bp, rq, test_user)
    db_session.commit()

    with pytest.raises(ValueError, match="not found"):
        mark_line_received(999999, line.id, test_user, db_session)
    with pytest.raises(ValueError, match="not found"):
        mark_line_received(bp.id, 999999, test_user, db_session)
    with pytest.raises(ValueError, match="not found"):
        mark_line_received(other_bp.id, line.id, test_user, db_session)  # cross-plan


# ── Idempotency ──────────────────────────────────────────────────────────


def test_mark_received_is_idempotent(db_session: Session, test_user: User):
    req, q, rq = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    line = _verified_line(db_session, bp, rq, test_user)
    db_session.commit()

    first = mark_line_received(bp.id, line.id, test_user, db_session)
    db_session.commit()
    stamp = first.received_at

    again = mark_line_received(bp.id, line.id, test_user, db_session)
    db_session.commit()
    assert again.received_at == stamp  # no re-stamp
    rows = (
        db_session.query(ActivityLog)
        .filter(
            ActivityLog.activity_type == ActivityType.LINE_RECEIVED.value,
            ActivityLog.buy_plan_line_id == line.id,
        )
        .count()
    )
    assert rows == 1  # no duplicate activity row


# ── Activity + plan-status hands-off ─────────────────────────────────────


def test_mark_received_writes_line_received_activity(db_session: Session, test_user: User):
    req, q, rq = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    line = _verified_line(db_session, bp, rq, test_user)
    db_session.commit()

    mark_line_received(bp.id, line.id, test_user, db_session)
    db_session.commit()

    row = db_session.query(ActivityLog).filter(ActivityLog.activity_type == ActivityType.LINE_RECEIVED.value).one()
    assert row.buy_plan_id == bp.id
    assert row.buy_plan_line_id == line.id  # the line FK
    assert row.user_id == test_user.id
    assert "PO-RCV" in (row.notes or "")  # log_activity maps description → notes


def test_mark_received_never_touches_plan_status(db_session: Session, test_user: User):
    """Even when every line is verified+received, the plan stays ACTIVE — completion
    runs exclusively through verify_po's machinery."""
    req, q, rq = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    line = _verified_line(db_session, bp, rq, test_user)
    db_session.commit()

    mark_line_received(bp.id, line.id, test_user, db_session)
    db_session.commit()
    db_session.expire_all()
    assert bp.status == BuyPlanStatus.ACTIVE.value


# ── The receive route ────────────────────────────────────────────────────


def test_receive_route_rerenders_po_pane(ws_client: TestClient, db_session: Session, test_user: User):
    req, q, rq = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    line = _verified_line(db_session, bp, rq, test_user)
    db_session.commit()

    r = ws_client.post(
        f"/v2/partials/buy-plans/{bp.id}/lines/{line.id}/receive",
        data={"origin": "approvals_workspace"},
    )
    assert r.status_code == 200
    assert r.headers.get("HX-Trigger") == "awListRefresh"
    assert "Approved by" in r.text  # the PO pane's verified stamp — pane re-rendered
    db_session.expire_all()
    assert line.received_at is not None


def test_receive_route_with_lens_rerenders_plan_pane(ws_client: TestClient, db_session: Session, test_user: User):
    req, q, rq = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    line = _verified_line(db_session, bp, rq, test_user)
    db_session.commit()

    r = ws_client.post(
        f"/v2/partials/buy-plans/{bp.id}/lines/{line.id}/receive",
        data={"origin": "approvals_workspace", "lens": "sales-orders"},
    )
    assert r.status_code == 200
    assert "aw-pane-body" in r.text  # the SO/BP pane (kanban home), not the PO pane
    db_session.expire_all()
    assert line.received_at is not None


def test_receive_route_403s_for_stranger(ws_client: TestClient, db_session: Session, test_user: User):
    stranger = _user(db_session, "buyer", "route-stranger-recv")
    req, q, rq = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    line = _verified_line(db_session, bp, rq, stranger, buyer_id=stranger.id)  # not the caller's line
    db_session.commit()

    r = ws_client.post(
        f"/v2/partials/buy-plans/{bp.id}/lines/{line.id}/receive",
        data={"origin": "approvals_workspace"},
    )
    assert r.status_code == 403
    db_session.expire_all()
    assert line.received_at is None


def test_receive_route_400s_on_bad_state(ws_client: TestClient, db_session: Session, test_user: User):
    req, q, rq = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    line = _line(db_session, bp, rq, test_user, status=BuyPlanLineStatus.AWAITING_PO.value)
    db_session.commit()

    r = ws_client.post(
        f"/v2/partials/buy-plans/{bp.id}/lines/{line.id}/receive",
        data={"origin": "approvals_workspace"},
    )
    assert r.status_code == 400
