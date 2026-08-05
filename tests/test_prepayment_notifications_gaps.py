"""test_prepayment_notifications_gaps.py — Coverage gap tests for
prepayment_notifications.

Covers lines not reached by the main test file:
  - run_prepayment_notify_bg._run inner function (vanished prepayment, success, exception)
  - schedule_prepayment_notify (no-loop path and loop-running path)
  - _notify_paid_inner edge cases (not found, no user_ids, commit exception → rollback)

The _send_group_email / _write_failure_alert internals this file used to cover were
deleted with the single-path outbox reroute (W3.8/§5.5) — the outbox drain job now owns
sending and failure durability.

Called by: pytest
Depends on: app.services.prepayment_notifications, conftest (db_session), unittest.mock.
"""

import os

os.environ["TESTING"] = "1"

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.services.prepayment_notifications import (
    _notify_paid_inner,
    run_prepayment_notify_bg,
    schedule_prepayment_notify,
)

# ── run_prepayment_notify_bg._run ─────────────────────────────────────────────


async def _run_immediately(coro, *, task_name, suppress_in_testing=False):
    """Helper: actually execute the coroutine passed to
    safe_background_task."""
    await coro


@pytest.mark.asyncio
async def test_run_bg_skips_when_prepayment_vanished():
    """_run logs a warning and returns without calling coro_fn when prepayment is
    gone."""
    coro_fn = AsyncMock()
    mock_db = MagicMock()
    mock_db.get.return_value = None  # prepayment no longer exists

    with patch("app.services.prepayment_notifications.safe_background_task", side_effect=_run_immediately):
        with patch("app.database.SessionLocal", return_value=mock_db):
            await run_prepayment_notify_bg(coro_fn, 99999)

    coro_fn.assert_not_called()
    mock_db.close.assert_called_once()


@pytest.mark.asyncio
async def test_run_bg_calls_coro_fn_when_prepayment_exists():
    """_run calls coro_fn(prepayment_id, db=...) when the prepayment is found."""
    coro_fn = AsyncMock()
    mock_db = MagicMock()
    mock_db.get.return_value = MagicMock()  # prepayment found

    with patch("app.services.prepayment_notifications.safe_background_task", side_effect=_run_immediately):
        with patch("app.database.SessionLocal", return_value=mock_db):
            await run_prepayment_notify_bg(coro_fn, 1)

    coro_fn.assert_called_once_with(1, db=mock_db)
    mock_db.close.assert_called_once()


@pytest.mark.asyncio
async def test_run_bg_logs_exception_and_closes_session():
    """_run catches exceptions from coro_fn, logs them, and still closes the session."""
    coro_fn = AsyncMock(side_effect=RuntimeError("graph down"))
    mock_db = MagicMock()
    mock_db.get.return_value = MagicMock()

    with patch("app.services.prepayment_notifications.safe_background_task", side_effect=_run_immediately):
        with patch("app.database.SessionLocal", return_value=mock_db):
            await run_prepayment_notify_bg(coro_fn, 1)  # must not raise

    mock_db.close.assert_called_once()


# ── schedule_prepayment_notify ────────────────────────────────────────────────


def test_schedule_no_loop_closes_coroutine():
    """With no running loop, schedule_prepayment_notify closes the coroutine cleanly."""
    mock_coro = MagicMock()

    with patch("asyncio.get_running_loop", side_effect=RuntimeError("no running loop")):
        schedule_prepayment_notify(mock_coro)

    mock_coro.close.assert_called_once()


@pytest.mark.asyncio
async def test_schedule_with_loop_creates_task():
    """With a running loop, schedule_prepayment_notify schedules the coro as a task."""
    import asyncio

    async def _dummy():
        pass

    coro = _dummy()
    loop = asyncio.get_running_loop()

    with patch.object(loop, "create_task") as mock_create:
        schedule_prepayment_notify(coro)

    mock_create.assert_called_once_with(coro)
    coro.close()  # prevent coroutine-never-awaited warning


# ── _notify_paid_inner ────────────────────────────────────────────────────────


def test_notify_paid_inner_prepayment_not_found(db_session: Session):
    """_notify_paid_inner returns empty alerted list when prepayment ID doesn't
    exist."""
    result = _notify_paid_inner(db_session, 99999)
    assert result == {"alerted": []}


def test_notify_paid_inner_no_recipients_returns_empty():
    """_notify_paid_inner skips the enqueue and returns empty when no user_ids
    resolve."""
    pp = MagicMock()
    pp.id = 55
    pp.created_by_id = None  # no buyer
    pp.buy_plan = None  # no plan → no salesperson
    pp.buy_plan_id = None

    mock_db = MagicMock()
    mock_db.get.return_value = pp
    mock_db.query.return_value.filter.return_value.all.return_value = []  # no managers

    result = _notify_paid_inner(mock_db, 55)

    assert result == {"alerted": []}
    mock_db.commit.assert_not_called()


def test_notify_paid_inner_exception_triggers_rollback():
    """_notify_paid_inner calls db.rollback when the enqueue commit fails."""
    pp = MagicMock()
    pp.id = 66
    pp.created_by_id = 7  # one buyer in user_ids
    pp.buy_plan = None
    pp.buy_plan_id = None
    pp.paid_amount = None
    pp.total_incl_fees = Decimal("500.00")
    pp.currency = "USD"
    pp.vendor_card = None
    pp.vendor_name = "AcmeCo"
    pp.buy_plan_line = None
    pp.void_reason = None
    pp.wire_reference = None
    pp.paid_by_label = None
    pp.buyer_remarks = None
    pp.payment_method = "wire"
    pp.test_report_sent = False
    pp.created_by = None

    mock_db = MagicMock()
    mock_db.get.return_value = pp
    mock_db.query.return_value.filter.return_value.all.return_value = []  # no managers
    mock_db.commit.side_effect = Exception("db gone")

    result = _notify_paid_inner(mock_db, 66)

    mock_db.rollback.assert_called_once()
    assert result == {"alerted": []}
