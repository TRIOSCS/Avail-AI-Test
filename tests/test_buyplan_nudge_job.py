"""test_buyplan_nudge_job.py — Tests for the unconfirmed-instruction nudge job.

Covers _job_buyplan_nudge: buyer nudge (awaiting_po past buyer SLA), ops nudge
(pending_verify past ops SLA), idempotency via last_nudge_at, and skip conditions
(recent nudge, no buyer, non-active plan, empty ops group).

All jobs use SessionLocal() internally, so we patch app.database.SessionLocal to the
test session with close() disabled (mirrors test_jobs_inventory.py).
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models import ActivityLog
from app.models.buy_plan import (
    BuyPlan,
    BuyPlanLine,
    BuyPlanLineStatus,
    BuyPlanStatus,
    VerificationGroupMember,
)


def _ago(hours: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=hours)


@pytest.fixture()
def scheduler_db(db_session: Session):
    """Patch SessionLocal so the nudge job uses the test DB; keep the session open."""
    original_close = db_session.close
    db_session.close = lambda: None
    with patch("app.database.SessionLocal", return_value=db_session):
        yield db_session
    db_session.close = original_close


def _active_plan(db, test_user, test_quote, test_requisition, approved_hours_ago=5.0):
    plan = BuyPlan(
        requisition_id=test_requisition.id,
        quote_id=test_quote.id,
        status=BuyPlanStatus.ACTIVE.value,
        so_status="approved",
        sales_order_number="SO-NUDGE",
        submitted_by_id=test_user.id,
        approved_at=_ago(approved_hours_ago),
        created_at=_ago(approved_hours_ago + 1),
    )
    db.add(plan)
    db.flush()
    return plan


def _line(db, plan, status, *, buyer_id=None, po_confirmed_hours_ago=None, last_nudge_hours_ago=None):
    line = BuyPlanLine(
        buy_plan_id=plan.id,
        quantity=10,
        status=status,
        buyer_id=buyer_id,
        po_confirmed_at=_ago(po_confirmed_hours_ago) if po_confirmed_hours_ago is not None else None,
        last_nudge_at=_ago(last_nudge_hours_ago) if last_nudge_hours_ago is not None else None,
    )
    db.add(line)
    db.flush()
    return line


def _run_job():
    from app.jobs.inventory_jobs import _job_buyplan_nudge

    asyncio.run(_job_buyplan_nudge())


def _nudge_logs(db, line_id):
    return [
        a
        for a in db.query(ActivityLog).filter_by(activity_type="buyplan_pending").all()
        if a.notes and f"line_id={line_id}" in a.notes
    ]


# ── Buyer nudge ──────────────────────────────────────────────────────────


class TestBuyerNudge:
    def test_fires_when_overdue(self, scheduler_db, test_user, test_quote, test_requisition):
        plan = _active_plan(scheduler_db, test_user, test_quote, test_requisition, approved_hours_ago=5)
        line = _line(scheduler_db, plan, BuyPlanLineStatus.AWAITING_PO.value, buyer_id=test_user.id)
        scheduler_db.commit()

        with patch("app.services.buyplan_notifications._teams_dm", new_callable=AsyncMock):
            _run_job()

        scheduler_db.refresh(line)
        assert line.last_nudge_at is not None
        logs = _nudge_logs(scheduler_db, line.id)
        assert logs and logs[0].user_id == test_user.id
        assert logs[0].buy_plan_id == plan.id  # DEAD-5: FK populated

    def test_skips_recent_nudge(self, scheduler_db, test_user, test_quote, test_requisition):
        plan = _active_plan(scheduler_db, test_user, test_quote, test_requisition, approved_hours_ago=5)
        line = _line(
            scheduler_db, plan, BuyPlanLineStatus.AWAITING_PO.value, buyer_id=test_user.id, last_nudge_hours_ago=1
        )
        scheduler_db.commit()
        before = line.last_nudge_at

        with patch("app.services.buyplan_notifications._teams_dm", new_callable=AsyncMock):
            _run_job()

        scheduler_db.refresh(line)
        assert line.last_nudge_at == before  # unchanged
        assert not _nudge_logs(scheduler_db, line.id)

    def test_skips_no_buyer(self, scheduler_db, test_user, test_quote, test_requisition):
        plan = _active_plan(scheduler_db, test_user, test_quote, test_requisition, approved_hours_ago=5)
        line = _line(scheduler_db, plan, BuyPlanLineStatus.AWAITING_PO.value, buyer_id=None)
        scheduler_db.commit()

        _run_job()

        scheduler_db.refresh(line)
        assert line.last_nudge_at is None

    def test_skips_non_active_plan(self, scheduler_db, test_user, test_quote, test_requisition):
        plan = _active_plan(scheduler_db, test_user, test_quote, test_requisition, approved_hours_ago=5)
        plan.status = BuyPlanStatus.PENDING.value
        scheduler_db.flush()
        line = _line(scheduler_db, plan, BuyPlanLineStatus.AWAITING_PO.value, buyer_id=test_user.id)
        scheduler_db.commit()

        with patch("app.services.buyplan_notifications._teams_dm", new_callable=AsyncMock):
            _run_job()

        scheduler_db.refresh(line)
        assert line.last_nudge_at is None

    def test_refires_after_window(self, scheduler_db, test_user, test_quote, test_requisition):
        # last_nudge 5h ago (> 4h buyer window) -> re-nudged
        plan = _active_plan(scheduler_db, test_user, test_quote, test_requisition, approved_hours_ago=6)
        line = _line(
            scheduler_db, plan, BuyPlanLineStatus.AWAITING_PO.value, buyer_id=test_user.id, last_nudge_hours_ago=5
        )
        scheduler_db.commit()
        before = line.last_nudge_at

        with patch("app.services.buyplan_notifications._teams_dm", new_callable=AsyncMock):
            _run_job()

        scheduler_db.refresh(line)
        assert line.last_nudge_at > before  # advanced to ~now
        assert _nudge_logs(scheduler_db, line.id)


# ── Ops nudge ────────────────────────────────────────────────────────────


class TestOpsNudge:
    def test_fires_when_overdue(self, scheduler_db, test_user, test_quote, test_requisition):
        plan = _active_plan(scheduler_db, test_user, test_quote, test_requisition)
        line = _line(
            scheduler_db, plan, BuyPlanLineStatus.PENDING_VERIFY.value, buyer_id=test_user.id, po_confirmed_hours_ago=3
        )
        scheduler_db.add(VerificationGroupMember(user_id=test_user.id, is_active=True))
        scheduler_db.commit()

        _run_job()

        scheduler_db.refresh(line)
        assert line.last_nudge_at is not None
        logs = _nudge_logs(scheduler_db, line.id)
        assert logs and logs[0].user_id == test_user.id

    def test_skips_when_no_ops_members(self, scheduler_db, test_user, test_quote, test_requisition):
        plan = _active_plan(scheduler_db, test_user, test_quote, test_requisition)
        line = _line(
            scheduler_db, plan, BuyPlanLineStatus.PENDING_VERIFY.value, buyer_id=test_user.id, po_confirmed_hours_ago=3
        )
        scheduler_db.commit()

        _run_job()

        scheduler_db.refresh(line)
        assert line.last_nudge_at is None
        assert not _nudge_logs(scheduler_db, line.id)

    def test_refires_after_window(self, scheduler_db, test_user, test_quote, test_requisition):
        plan = _active_plan(scheduler_db, test_user, test_quote, test_requisition)
        line = _line(
            scheduler_db,
            plan,
            BuyPlanLineStatus.PENDING_VERIFY.value,
            buyer_id=test_user.id,
            po_confirmed_hours_ago=4,
            last_nudge_hours_ago=3,
        )
        scheduler_db.add(VerificationGroupMember(user_id=test_user.id, is_active=True))
        scheduler_db.commit()
        before = line.last_nudge_at

        _run_job()

        scheduler_db.refresh(line)
        assert line.last_nudge_at > before  # > 2h ops window -> re-nudged

    def test_skips_recent_nudge(self, scheduler_db, test_user, test_quote, test_requisition):
        plan = _active_plan(scheduler_db, test_user, test_quote, test_requisition)
        line = _line(
            scheduler_db,
            plan,
            BuyPlanLineStatus.PENDING_VERIFY.value,
            buyer_id=test_user.id,
            po_confirmed_hours_ago=3,
            last_nudge_hours_ago=1,
        )
        scheduler_db.add(VerificationGroupMember(user_id=test_user.id, is_active=True))
        scheduler_db.commit()
        before = line.last_nudge_at

        _run_job()

        scheduler_db.refresh(line)
        assert line.last_nudge_at == before  # within 2h ops window -> not re-nudged
