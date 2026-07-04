"""test_prepayment_notifications_gaps.py — Coverage gap tests for
prepayment_notifications.

Covers lines not reached by the main test file:
  - run_prepayment_notify_bg._run inner function (vanished prepayment, success, exception)
  - schedule_prepayment_notify (no-loop path and loop-running path)
  - _send_group_email internals (empty recipients, no admin token, token unavailable,
    successful send, partial failure, all failures)
  - _write_failure_alert edge cases (no user_ids → early return, commit exception → rollback)
  - _notify_paid_inner edge cases (not found, no user_ids, commit exception → rollback)

Called by: pytest
Depends on: app.services.prepayment_notifications, conftest (db_session), unittest.mock.
"""

import os

os.environ["TESTING"] = "1"

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models import ActivityLog, User
from app.models.buy_plan import BuyPlan, BuyPlanLine
from app.models.quality_plan import Prepayment
from app.models.quotes import Quote
from app.models.sourcing import Requisition
from app.models.vendors import VendorCard
from app.services.prepayment_notifications import (
    _notify_paid_inner,
    _send_group_email,
    _write_failure_alert,
    run_prepayment_notify_bg,
    schedule_prepayment_notify,
)

# ── Helpers ───────────────────────────────────────────────────────────────────


def _unique_email() -> str:
    return f"user-{uuid.uuid4().hex[:8]}@test.com"


def _make_user(
    db: Session,
    *,
    role: str = "manager",
    access_token: str | None = None,
    is_active: bool = True,
) -> User:
    u = User(
        email=_unique_email(),
        name="Test User",
        role=role,
        azure_id=f"az-{uuid.uuid4().hex[:8]}",
        is_active=is_active,
        access_token=access_token,
        created_at=datetime.now(timezone.utc),
    )
    db.add(u)
    db.flush()
    return u


def _make_stub_prepayment(db: Session, *, created_by_id: int | None = None) -> Prepayment:
    """Minimal Prepayment graph for _write_failure_alert and _notify_paid_inner
    tests."""
    creator_id = created_by_id or 0
    req = Requisition(
        name=f"REQ-{uuid.uuid4().hex[:6]}",
        customer_name="AcmeCo",
        status="active",
        created_by=creator_id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(req)
    db.flush()
    q = Quote(
        requisition_id=req.id,
        quote_number=f"Q-{uuid.uuid4().hex[:8]}",
        line_items=[],
        status="sent",
        created_by_id=creator_id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(q)
    db.flush()
    bp = BuyPlan(
        requisition_id=req.id,
        quote_id=q.id,
        status="active",
        so_status="approved",
        submitted_by_id=created_by_id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(bp)
    db.flush()
    vc = VendorCard(
        normalized_name=f"vc-{uuid.uuid4().hex[:8]}",
        display_name="Test Vendor",
    )
    db.add(vc)
    db.flush()
    line = BuyPlanLine(
        buy_plan_id=bp.id,
        quantity=1,
        unit_cost=100.0,
        status="pending_verify",
        po_confirmed_at=datetime.now(timezone.utc),
    )
    db.add(line)
    db.flush()
    pp = Prepayment(
        buy_plan_id=bp.id,
        buy_plan_line_id=line.id,
        vendor_card_id=vc.id,
        vendor_name="Test Vendor",
        total_incl_fees=Decimal("1000.00"),
        currency="USD",
        created_by_id=created_by_id,
        status="pending",
    )
    db.add(pp)
    db.commit()
    return pp


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


# ── _send_group_email ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_group_email_empty_recipients():
    """_send_group_email returns False immediately when the recipient list is empty."""
    mock_db = MagicMock()
    result = await _send_group_email(mock_db, [], "subject", "<html/>")
    assert result is False
    mock_db.query.assert_not_called()


@pytest.mark.asyncio
async def test_send_group_email_no_admin_with_access_token():
    """_send_group_email returns False when no admin in settings.admin_emails has a
    token."""
    admin = MagicMock()
    admin.access_token = None  # no live token

    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.all.return_value = [admin]

    with patch("app.services.prepayment_notifications.settings") as mock_settings:
        mock_settings.admin_emails = ["admin@test.com"]
        result = await _send_group_email(mock_db, ["ap@test.com"], "subj", "<html/>")

    assert result is False


@pytest.mark.asyncio
async def test_send_group_email_token_refresh_fails():
    """_send_group_email returns False when get_valid_token returns falsy."""
    admin = MagicMock()
    admin.access_token = "stale-token"

    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.all.return_value = [admin]

    with patch("app.services.prepayment_notifications.settings") as mock_settings:
        mock_settings.admin_emails = ["admin@test.com"]
        with patch(
            "app.utils.token_manager.get_valid_token",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await _send_group_email(mock_db, ["ap@test.com"], "subj", "<html/>")

    assert result is False


@pytest.mark.asyncio
async def test_send_group_email_sends_to_all_recipients():
    """_send_group_email posts to each address and returns True on success."""
    admin = MagicMock()
    admin.access_token = "tok-live"

    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.all.return_value = [admin]

    mock_gc = MagicMock()
    mock_gc.post_json = AsyncMock()

    with patch("app.services.prepayment_notifications.settings") as mock_settings:
        mock_settings.admin_emails = ["admin@test.com"]
        with patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="tok-live"):
            with patch("app.utils.graph_client.GraphClient", return_value=mock_gc):
                result = await _send_group_email(
                    mock_db,
                    ["acc@test.com", "ap@test.com"],
                    "subj",
                    "<html/>",
                )

    assert result is True
    assert mock_gc.post_json.call_count == 2


@pytest.mark.asyncio
async def test_send_group_email_partial_recipient_failure_returns_true():
    """_send_group_email returns True when at least one send succeeds despite one
    failure."""
    admin = MagicMock()
    admin.access_token = "tok-live"

    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.all.return_value = [admin]

    mock_gc = MagicMock()
    mock_gc.post_json = AsyncMock(side_effect=[None, RuntimeError("timeout")])

    with patch("app.services.prepayment_notifications.settings") as mock_settings:
        mock_settings.admin_emails = ["admin@test.com"]
        with patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="tok-live"):
            with patch("app.utils.graph_client.GraphClient", return_value=mock_gc):
                result = await _send_group_email(
                    mock_db,
                    ["acc@test.com", "ap@test.com"],
                    "subj",
                    "<html/>",
                )

    assert result is True


@pytest.mark.asyncio
async def test_send_group_email_all_recipients_fail_returns_false():
    """_send_group_email returns False when every post_json call raises."""
    admin = MagicMock()
    admin.access_token = "tok-live"

    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.all.return_value = [admin]

    mock_gc = MagicMock()
    mock_gc.post_json = AsyncMock(side_effect=RuntimeError("all failed"))

    with patch("app.services.prepayment_notifications.settings") as mock_settings:
        mock_settings.admin_emails = ["admin@test.com"]
        with patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="tok-live"):
            with patch("app.utils.graph_client.GraphClient", return_value=mock_gc):
                result = await _send_group_email(mock_db, ["ap@test.com"], "subj", "<html/>")

    assert result is False


# ── _write_failure_alert ──────────────────────────────────────────────────────


def test_write_failure_alert_no_user_ids_returns_early(db_session: Session):
    """_write_failure_alert logs and returns when no requester or admin exists."""
    pp = MagicMock()
    pp.id = 42
    pp.created_by_id = None  # no requester
    pp.buy_plan_id = None
    pp.buy_plan = None
    # No active admin users in the test DB.

    _write_failure_alert(db_session, pp)

    assert db_session.query(ActivityLog).filter(ActivityLog.subject.like("Prepayment #42%")).count() == 0


def test_write_failure_alert_exception_triggers_rollback():
    """_write_failure_alert calls db.rollback when db.commit raises."""
    pp = MagicMock()
    pp.id = 77
    pp.created_by_id = 1
    pp.buy_plan = None
    pp.buy_plan_id = None

    mock_db = MagicMock()
    # No admins so that user_ids = {1} from created_by_id alone.
    mock_db.query.return_value.filter.return_value.all.return_value = []
    mock_db.commit.side_effect = Exception("commit failed")

    _write_failure_alert(mock_db, pp)

    mock_db.rollback.assert_called_once()


# ── _notify_paid_inner ────────────────────────────────────────────────────────


def test_notify_paid_inner_prepayment_not_found(db_session: Session):
    """_notify_paid_inner returns empty alerted list when prepayment ID doesn't
    exist."""
    result = _notify_paid_inner(db_session, 99999)
    assert result == {"alerted": []}


def test_notify_paid_inner_no_recipients_returns_empty():
    """_notify_paid_inner skips commit and returns empty when no user_ids are
    resolved."""
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
    """_notify_paid_inner calls db.rollback when commit fails."""
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

    mock_db = MagicMock()
    mock_db.get.return_value = pp
    mock_db.query.return_value.filter.return_value.all.return_value = []  # no managers
    mock_db.commit.side_effect = Exception("db gone")

    result = _notify_paid_inner(mock_db, 66)

    mock_db.rollback.assert_called_once()
    assert result == {"alerted": []}
