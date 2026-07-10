"""test_startup_po_completion_sweep.py — Phase 3: the startup completion sweep.

Migration 176 reverts every plan parked in the retired INBOUND state back to
ACTIVE. A plan whose lines were ALL already terminal (verified/cancelled) with an
approved SO must then actually COMPLETE — via the canonical
check_completion/_complete_plan path, not raw migration SQL.
``app.startup._complete_reverted_active_plans`` is that one-time, idempotent boot
sweep. This pins:
  - an ACTIVE + all-terminal + SO-approved plan completes (and gets a case_report);
  - a plan with a still-open PENDING_VERIFY line is left ACTIVE (the guard holds);
  - a non-approved-SO plan is left ACTIVE;
  - the sweep is idempotent (a second run is a no-op, completes nothing new).

Called by: pytest
Depends on: conftest (db_session), app.startup, app.services.buyplan_workflow,
            app.models.{buy_plan,auth,quotes,sourcing}.
"""

import uuid
from datetime import UTC, datetime
from unittest.mock import patch

from sqlalchemy.orm import Session

from app.constants import (
    BuyPlanLineStatus,
    BuyPlanStatus,
    SOVerificationStatus,
)
from app.models.auth import User
from app.models.buy_plan import BuyPlan, BuyPlanLine
from app.models.quotes import Quote
from app.models.sourcing import Requisition


def _user(db: Session) -> User:
    u = User(
        email=f"sweep-{uuid.uuid4().hex[:8]}@test.com",
        name="Sweep User",
        role="admin",
        azure_id=f"azure-sweep-{uuid.uuid4().hex[:8]}",
        created_at=datetime.now(UTC),
    )
    db.add(u)
    db.flush()
    return u


def _plan(
    db: Session,
    user: User,
    *,
    so_status: str = SOVerificationStatus.APPROVED.value,
) -> BuyPlan:
    req = Requisition(
        name=f"REQ-SW-{uuid.uuid4().hex[:6]}",
        customer_name="SweepCo",
        status="active",
        created_by=user.id,
        created_at=datetime.now(UTC),
    )
    db.add(req)
    db.flush()
    quote = Quote(
        requisition_id=req.id,
        quote_number=f"QSW-{uuid.uuid4().hex[:8]}",
        line_items=[],
        status="sent",
        created_by_id=user.id,
        created_at=datetime.now(UTC),
    )
    db.add(quote)
    db.flush()
    bp = BuyPlan(
        requisition_id=req.id,
        quote_id=quote.id,
        status=BuyPlanStatus.ACTIVE.value,
        so_status=so_status,
        total_cost=100.0,
        submitted_by_id=user.id,
    )
    db.add(bp)
    db.flush()
    return bp


def _line(db: Session, plan: BuyPlan, status: str) -> BuyPlanLine:
    line = BuyPlanLine(
        buy_plan_id=plan.id,
        status=status,
        unit_cost=10.0,
        quantity=10,
        po_number="PO-SW",
        po_confirmed_at=datetime.now(UTC),
    )
    db.add(line)
    db.flush()
    return line


def _run_sweep(db: Session) -> None:
    """Run the sweep against the test session (SessionLocal patched to it)."""
    from app import startup

    with patch.object(startup, "SessionLocal", return_value=db), patch.object(db, "close"):
        startup._complete_reverted_active_plans()


def test_sweep_completes_all_terminal_active_plan(db_session: Session):
    user = _user(db_session)
    plan = _plan(db_session, user)
    _line(db_session, plan, BuyPlanLineStatus.VERIFIED.value)
    _line(db_session, plan, BuyPlanLineStatus.CANCELLED.value)
    db_session.commit()

    _run_sweep(db_session)

    db_session.refresh(plan)
    assert plan.status == BuyPlanStatus.COMPLETED.value
    assert plan.completed_at is not None
    assert plan.case_report is not None


def test_sweep_leaves_plan_with_open_line_active(db_session: Session):
    user = _user(db_session)
    plan = _plan(db_session, user)
    _line(db_session, plan, BuyPlanLineStatus.VERIFIED.value)
    _line(db_session, plan, BuyPlanLineStatus.PENDING_VERIFY.value)
    db_session.commit()

    _run_sweep(db_session)

    db_session.refresh(plan)
    assert plan.status == BuyPlanStatus.ACTIVE.value


def test_sweep_leaves_plan_with_unapproved_so_active(db_session: Session):
    user = _user(db_session)
    plan = _plan(db_session, user, so_status=SOVerificationStatus.PENDING.value)
    _line(db_session, plan, BuyPlanLineStatus.VERIFIED.value)
    db_session.commit()

    _run_sweep(db_session)

    db_session.refresh(plan)
    assert plan.status == BuyPlanStatus.ACTIVE.value


def test_sweep_is_idempotent(db_session: Session):
    user = _user(db_session)
    plan = _plan(db_session, user)
    _line(db_session, plan, BuyPlanLineStatus.VERIFIED.value)
    db_session.commit()

    _run_sweep(db_session)
    db_session.refresh(plan)
    assert plan.status == BuyPlanStatus.COMPLETED.value
    first_completed_at = plan.completed_at

    # Second boot: the plan is no longer ACTIVE, so it is not re-selected or re-completed.
    _run_sweep(db_session)
    db_session.refresh(plan)
    assert plan.status == BuyPlanStatus.COMPLETED.value
    assert plan.completed_at == first_completed_at
