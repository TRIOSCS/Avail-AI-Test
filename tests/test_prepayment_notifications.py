"""test_prepayment_notifications.py — accounting/AP prepayment notifications.

Single-path delivery (W3.8/§5.5): every prepayment notice is exactly ONE delivery —
an email enqueued on the approval outbox. Covers:
  - notify_prepayment_requested / _approved / _voided enqueue ONE outbox email
    addressed to the configured group DLs (accounting + AP) with an admin user as the
    delegated Graph sender; no Teams card, no in-app ActivityLog rows;
  - an unset config key / missing admin sender / missing anchor request skips the
    enqueue with no raise;
  - the email renders the amount to 2 decimals honoring currency + the beneficiary
    legal name (findings #9/#14), with distinct DO-NOT-PAY vs OK-TO-WIRE headings
    (finding #13) and the DO-NOT-WIRE stand-down reason;
  - notify_prepayment_paid enqueues one outbox email per Avail recipient (buyer,
    salesperson, every active manager) — no ActivityLog fan-out;
  - schedule_prepayment_notify's cross-thread dispatch.

Called by: pytest
Depends on: app.services.prepayment_notifications, app.services.prepayment_service,
            app.services.approvals.service, conftest (db_session), unittest.mock.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.orm import Session

from app.constants import (
    ApprovalGateType,
    ApprovalRequestStatus,
    ApprovalSubjectType,
    PaymentMethod,
    PrepaymentStatus,
)
from app.models import ActivityLog, Offer, Requirement, SystemConfig, User
from app.models.approvals import ApprovalOutbox, ApprovalRequest
from app.models.buy_plan import BuyPlan, BuyPlanLine
from app.models.quality_plan import Prepayment
from app.models.quotes import Quote
from app.models.sourcing import Requisition
from app.models.vendors import VendorCard
from app.services import prepayment_notifications as pn
from app.services.prepayment_service import create_prepayment

ACC = "accounting@trio.test"
AP = "ap@trio.test"
ADMIN_EMAIL = "admin@trioscs.com"


# ── Builders ─────────────────────────────────────────────────────────────


def _seed_approver(db: Session) -> User:
    """A manager who owns their requisitions AND can approve prepayments (routing +
    ownership both satisfied by the same user in these unit tests)."""
    u = User(
        email=f"appr-{uuid.uuid4().hex[:6]}@trioscs.com",
        name="PP Approver",
        role="manager",
        azure_id=f"az-{uuid.uuid4().hex[:8]}",
        is_active=True,
        can_approve_prepayments=True,
        prepayment_approval_limit=None,
        created_at=datetime.now(UTC),
    )
    db.add(u)
    db.flush()
    return u


def _seed_admin(db: Session, *, access_token: str | None = "tok") -> User:
    """The admin user the outbox row borrows as its delegated Graph sender."""
    u = User(
        email=ADMIN_EMAIL,
        name="Admin",
        role="admin",
        azure_id=f"az-{uuid.uuid4().hex[:8]}",
        is_active=True,
        access_token=access_token,
        created_at=datetime.now(UTC),
    )
    db.add(u)
    db.commit()
    return u


def _make_prepayment(
    db: Session,
    *,
    requester: User,
    vendor_legal: str | None = "Acme Components LLC",
    offer_vendor: str = "AcmeVendor",
    currency: str = "USD",
    amount: Decimal = Decimal("20002.38"),
    test_report_sent: bool = False,
    remarks: str | None = "rush",
) -> tuple[object, ApprovalRequest]:
    """Build a full plan→line→offer→vendor-card graph and a routed Prepayment."""
    req = Requisition(
        name=f"REQ-{uuid.uuid4().hex[:6]}",
        customer_name="AcmeCo",
        status="active",
        created_by=requester.id,
        created_at=datetime.now(UTC),
    )
    db.add(req)
    db.flush()
    rq = Requirement(requisition_id=req.id, primary_mpn="LM317", created_at=datetime.now(UTC))
    db.add(rq)
    db.flush()
    q = Quote(
        requisition_id=req.id,
        quote_number=f"Q-{uuid.uuid4().hex[:8]}",
        line_items=[],
        status="sent",
        created_by_id=requester.id,
        created_at=datetime.now(UTC),
    )
    db.add(q)
    db.flush()
    bp = BuyPlan(
        requisition_id=req.id,
        quote_id=q.id,
        status="active",
        so_status="approved",
        sales_order_number="SO-999",
        submitted_by_id=requester.id,
        created_at=datetime.now(UTC),
    )
    db.add(bp)
    db.flush()
    vc = VendorCard(
        normalized_name=f"vc-{uuid.uuid4().hex[:8]}",
        display_name="AcmeVendor Display",
        legal_name=vendor_legal,
    )
    db.add(vc)
    db.flush()
    off = Offer(
        requirement_id=rq.id,
        vendor_card_id=vc.id,
        vendor_name=offer_vendor,
        vendor_name_normalized=offer_vendor.lower(),
        mpn="LM317",
        normalized_mpn="LM317",
        unit_price=10.0,
    )
    db.add(off)
    db.flush()
    line = BuyPlanLine(
        buy_plan_id=bp.id,
        requirement_id=rq.id,
        offer_id=off.id,
        quantity=2,
        unit_cost=10.0,
        buyer_id=requester.id,
        status="pending_verify",
        po_number="PO-2024",
        po_confirmed_at=datetime.now(UTC),
    )
    db.add(line)
    db.flush()
    pp, ar = create_prepayment(
        db,
        buy_plan_id=bp.id,
        buy_plan_line_id=line.id,
        vendor_card_id=vc.id,
        payment_method=PaymentMethod.WIRE,
        total_incl_fees=amount,
        test_report_sent=test_report_sent,
        buyer_remarks=remarks,
        created_by=requester,
        currency=currency,
    )
    db.commit()
    return pp, ar


def _set_config(db: Session, **kv: str) -> None:
    """Seed system_config rows directly (set_config_value 404s on unknown keys)."""
    for k, v in kv.items():
        db.add(SystemConfig(key=k, value=v, updated_at=datetime.now(UTC)))
    db.commit()


def _patched_settings():
    """Patch the module-level settings with a live admin_emails list."""
    mock = patch.object(pn, "settings")
    return mock


def _dl_rows(db: Session) -> list[ApprovalOutbox]:
    """The outbox rows addressed to external DLs (payload carries 'to')."""
    return [r for r in db.query(ApprovalOutbox).all() if (r.payload or {}).get("to")]


# ── requested: ONE outbox email to the group DLs ──────────────────────────


@pytest.mark.asyncio
async def test_requested_enqueues_one_outbox_email(db_session: Session):
    appr = _seed_approver(db_session)
    admin = _seed_admin(db_session)
    pp, ar = _make_prepayment(db_session, requester=appr)
    _set_config(db_session, accounting_group_email=ACC, ap_group_email=AP)

    with _patched_settings() as mock_settings:
        mock_settings.admin_emails = [ADMIN_EMAIL]
        mock_settings.app_url = "https://avail.test"
        result = await pn.notify_prepayment_requested(pp.id, db=db_session)

    assert result["enqueued"] is True
    assert ACC in result["recipients"] and AP in result["recipients"]
    rows = _dl_rows(db_session)
    assert len(rows) == 1
    assert rows[0].channel == "email"
    assert rows[0].request_id == ar.id
    assert rows[0].recipient_user_id == admin.id  # delegated Graph sender
    assert rows[0].payload["to"] == [ACC, AP]
    assert "DO NOT PAY YET" in rows[0].payload["subject"]


@pytest.mark.asyncio
async def test_requested_no_teams_no_inapp(db_session: Session):
    """Single path: no Teams card post and no in-app ActivityLog row for the event."""
    appr = _seed_approver(db_session)
    _seed_admin(db_session)
    pp, _ar = _make_prepayment(db_session, requester=appr)
    _set_config(db_session, accounting_group_email=ACC)

    # The engine's audit ActivityLog rows (create_request 'submitted') already exist —
    # the pin is that the NOTIFY adds none.
    before = db_session.query(ActivityLog).count()
    with (
        _patched_settings() as mock_settings,
        patch("app.services.teams_notifications.post_teams_channel_card", new_callable=AsyncMock) as card,
    ):
        mock_settings.admin_emails = [ADMIN_EMAIL]
        mock_settings.app_url = "https://avail.test"
        await pn.notify_prepayment_requested(pp.id, db=db_session)

    card.assert_not_awaited()
    assert db_session.query(ActivityLog).count() == before
    assert len(_dl_rows(db_session)) == 1


# ── approved: distinct OK-TO-WIRE subject ─────────────────────────────────


@pytest.mark.asyncio
async def test_approved_enqueues_ok_to_wire(db_session: Session):
    from app.services.approvals.service import decide

    appr = _seed_approver(db_session)
    admin = _seed_admin(db_session)
    pp, ar = _make_prepayment(db_session, requester=appr)
    decide(db_session, ar.id, appr, "approve", comment="approved")
    db_session.commit()

    _set_config(db_session, accounting_group_email=ACC, ap_group_email=AP)
    with _patched_settings() as mock_settings:
        mock_settings.admin_emails = [ADMIN_EMAIL]
        mock_settings.app_url = "https://avail.test"
        result = await pn.notify_prepayment_approved(pp.id, db=db_session)

    assert result["enqueued"] is True
    rows = _dl_rows(db_session)
    assert len(rows) == 1
    assert rows[0].recipient_user_id == admin.id
    assert "OK TO WIRE" in rows[0].payload["subject"]
    assert "PP Approver" in rows[0].payload["html"]  # approver name resolved


# ── skip paths: unset config / no admin sender / no anchor request ────────


@pytest.mark.asyncio
async def test_unset_config_skips_no_raise(db_session: Session):
    appr = _seed_approver(db_session)
    _seed_admin(db_session)
    pp, _ar = _make_prepayment(db_session, requester=appr)

    with _patched_settings() as mock_settings:
        mock_settings.admin_emails = [ADMIN_EMAIL]
        mock_settings.app_url = "https://avail.test"
        result = await pn.notify_prepayment_requested(pp.id, db=db_session)

    assert result["enqueued"] is False and result["recipients"] == []
    assert _dl_rows(db_session) == []
    # No honesty fallback anymore — the outbox row itself is the durable record.
    assert db_session.query(ActivityLog).filter(ActivityLog.subject.like("Prepayment%FAILED%")).count() == 0


@pytest.mark.asyncio
async def test_no_admin_sender_skips_no_raise(db_session: Session):
    appr = _seed_approver(db_session)  # no admin user seeded
    pp, _ar = _make_prepayment(db_session, requester=appr)
    _set_config(db_session, accounting_group_email=ACC)

    with _patched_settings() as mock_settings:
        mock_settings.admin_emails = [ADMIN_EMAIL]
        mock_settings.app_url = "https://avail.test"
        result = await pn.notify_prepayment_requested(pp.id, db=db_session)

    assert result["enqueued"] is False
    assert _dl_rows(db_session) == []


@pytest.mark.asyncio
async def test_missing_prepayment_skips_no_raise(db_session: Session):
    result = await pn.notify_prepayment_requested(999999, db=db_session)
    assert result["enqueued"] is False
    assert _dl_rows(db_session) == []


# ── Email content: currency to 2 decimals + beneficiary legal name ────────


def test_email_shows_currency_2dp_and_beneficiary(db_session: Session):
    appr = _seed_approver(db_session)
    pp, _ar = _make_prepayment(
        db_session, requester=appr, vendor_legal="Acme Components LLC", amount=Decimal("20002.38")
    )
    text = pn._email_html(pp, "requested")
    assert "USD 20,002.38" in text  # 2 decimals + currency (finding #9)
    assert "Acme Components LLC" in text  # beneficiary legal name (finding #14)


def test_beneficiary_falls_back_to_vendor_name_without_legal(db_session: Session):
    appr = _seed_approver(db_session)
    pp, _ar = _make_prepayment(db_session, requester=appr, vendor_legal=None, offer_vendor="AcmeVendor")
    text = pn._email_html(pp, "requested")
    assert "AcmeVendor" in text  # snapshot vendor_name when no legal_name


# ── Distinct headings: DO-NOT-PAY vs OK-TO-WIRE (finding #13) ─────────────


def test_distinct_headings(db_session: Session):
    appr = _seed_approver(db_session)
    pp, _ar = _make_prepayment(db_session, requester=appr)
    req_email = pn._email_html(pp, "requested")
    app_email = pn._email_html(pp, "approved")
    assert "PENDING APPROVAL" in req_email and "DO NOT PAY YET" in req_email
    assert "OK TO WIRE" in app_email and "APPROVED" in app_email
    assert "DO NOT PAY" not in app_email


# ── Paid fan-out + Voided stand-down (prepay closure) ─────────────────────


def _seed_person(db: Session, *, role: str, name: str) -> User:
    """A plain active user in *role* (buyer / salesperson / manager fan-out targets)."""
    u = User(
        email=f"{name.lower()}-{uuid.uuid4().hex[:6]}@trioscs.com",
        name=name,
        role=role,
        azure_id=f"az-{uuid.uuid4().hex[:8]}",
        is_active=True,
        created_at=datetime.now(UTC),
    )
    db.add(u)
    db.flush()
    return u


def _make_paid_prepay(db: Session, buyer: User, salesperson: User) -> Prepayment:
    """A PAID Prepayment whose buyer (created_by) and salesperson (plan submitter)
    differ.

    Built directly (no approval routing) so the fan-out recipients are unambiguous; an
    anchor PREPAYMENT ApprovalRequest is added for the outbox FK.
    """
    req = Requisition(
        name=f"REQ-{uuid.uuid4().hex[:6]}",
        customer_name="AcmeCo",
        status="active",
        created_by=salesperson.id,
        created_at=datetime.now(UTC),
    )
    db.add(req)
    db.flush()
    q = Quote(
        requisition_id=req.id,
        quote_number=f"Q-{uuid.uuid4().hex[:8]}",
        line_items=[],
        status="sent",
        created_by_id=salesperson.id,
        created_at=datetime.now(UTC),
    )
    db.add(q)
    db.flush()
    bp = BuyPlan(
        requisition_id=req.id,
        quote_id=q.id,
        status="active",
        so_status="approved",
        sales_order_number="SO-PAID",
        submitted_by_id=salesperson.id,
        created_at=datetime.now(UTC),
    )
    db.add(bp)
    db.flush()
    vc = VendorCard(
        normalized_name=f"vc-{uuid.uuid4().hex[:8]}",
        display_name="AcmeVendor Display",
        legal_name="Acme Components LLC",
    )
    db.add(vc)
    db.flush()
    line = BuyPlanLine(
        buy_plan_id=bp.id,
        quantity=2,
        unit_cost=10.0,
        status="pending_verify",
        po_number="PO-2024",
        po_confirmed_at=datetime.now(UTC),
    )
    db.add(line)
    db.flush()
    pp = Prepayment(
        buy_plan_id=bp.id,
        buy_plan_line_id=line.id,
        vendor_card_id=vc.id,
        vendor_name="Acme Components LLC",
        total_incl_fees=Decimal("20002.38"),
        currency="USD",
        created_by_id=buyer.id,
        status=PrepaymentStatus.PAID.value,
        paid_amount=Decimal("20002.38"),
        paid_via="in_app",
        paid_by_label="MK",
        wire_reference="WIRE-1",
        paid_at=datetime.now(UTC),
    )
    db.add(pp)
    db.flush()
    db.add(
        ApprovalRequest(
            gate_type=ApprovalGateType.PREPAYMENT,
            subject_type=ApprovalSubjectType.PREPAYMENT,
            subject_id=pp.id,
            requested_by_id=buyer.id,
            owner_id=buyer.id,
            status=ApprovalRequestStatus.APPROVED,
        )
    )
    db.commit()
    return pp


@pytest.fixture()
def users(db_session: Session) -> dict:
    """A buyer, a distinct salesperson, and two managers — the paid-notice fan-out
    set."""
    m1 = _seed_person(db_session, role="manager", name="Mgr1")
    m2 = _seed_person(db_session, role="manager", name="Mgr2")
    buyer = _seed_person(db_session, role="buyer", name="Buyer")
    salesperson = _seed_person(db_session, role="sales", name="Sales")
    db_session.commit()
    return {"managers": [m1, m2], "buyer": buyer, "salesperson": salesperson}


@pytest.fixture()
def paid_prepay(db_session: Session, users: dict) -> Prepayment:
    return _make_paid_prepay(db_session, users["buyer"], users["salesperson"])


@pytest.fixture()
def approved_prepay(db_session: Session) -> Prepayment:
    """An APPROVED prepayment with a live pay_token (the voided stand-down subject)."""
    appr = _seed_approver(db_session)
    pp, _ar = _make_prepayment(db_session, requester=appr)
    pp.status = PrepaymentStatus.APPROVED.value
    pp.pay_token = f"tok-{uuid.uuid4().hex}"
    db_session.commit()
    return pp


@pytest.fixture()
def set_group_config(db_session: Session) -> None:
    _set_config(db_session, accounting_group_email=ACC, ap_group_email=AP)


@pytest.mark.asyncio
async def test_paid_enqueues_email_per_recipient(db_session: Session, paid_prepay: Prepayment, users: dict):
    await pn.notify_prepayment_paid(paid_prepay.id, db=db_session)

    rows = db_session.query(ApprovalOutbox).all()
    recips = {r.recipient_user_id for r in rows}
    assert paid_prepay.created_by_id in recips  # buyer
    assert paid_prepay.buy_plan.submitted_by_id in recips  # salesperson
    assert all(m.id in recips for m in users["managers"])  # every manager
    # Deduped: one outbox email per recipient.
    assert len(rows) == len(recips)
    html = rows[0].payload["html"]
    assert "Acme Components LLC" in html and "USD 20,002.38" in html and "PO-2024" in html
    assert "WIRE CONFIRMED" in rows[0].payload["subject"]
    # No in-app fan-out anymore — the outbox emails are the single delivery.
    assert db_session.query(ActivityLog).filter(ActivityLog.channel == "system").count() == 0


@pytest.mark.asyncio
async def test_paid_salesperson_falls_back_to_requisition_creator(db_session: Session):
    """When the plan has no submitter, the requisition creator gets the salesperson
    email."""
    buyer = _seed_person(db_session, role="buyer", name="Buyer2")
    creator = _seed_person(db_session, role="sales", name="ReqOwner")
    pp = _make_paid_prepay(db_session, buyer, creator)
    pp.buy_plan.submitted_by_id = None  # force the fallback
    db_session.commit()
    await pn.notify_prepayment_paid(pp.id, db=db_session)
    recips = {r.recipient_user_id for r in db_session.query(ApprovalOutbox).all()}
    assert creator.id in recips  # requisition.created_by fallback


@pytest.mark.asyncio
async def test_voided_enqueues_stand_down(db_session: Session, approved_prepay: Prepayment, set_group_config):
    _seed_admin(db_session)
    with _patched_settings() as mock_settings:
        mock_settings.admin_emails = [ADMIN_EMAIL]
        mock_settings.app_url = "https://avail.test"
        result = await pn.notify_prepayment_voided(approved_prepay.id, db=db_session, reason="plan cancelled")

    assert result["enqueued"] is True
    rows = _dl_rows(db_session)
    assert len(rows) == 1
    body = rows[0].payload["html"]
    assert "DO NOT WIRE" in body
    assert "plan cancelled" in body


# ═══════════════════════════════════════════════════════════════════════════════
# schedule_prepayment_notify — cross-thread fallback
#
# Sync callers (threadpool-run routes / sync services driving check_completion ->
# _complete_plan -> _cancel_open_prepayment_requests_for_plan) run on a worker
# thread with no running loop of its own. Before the fix, schedule_prepayment_notify's
# get_running_loop() always missed there and coro.close()'d the DO-NOT-WIRE stand-down.
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_cross_thread_dispatch_runs_notify_on_main_loop():
    """Simulates the to_thread context: schedule_prepayment_notify called from a worker
    thread with no running loop, but the main loop WAS registered — the notify
    coroutine must actually execute, not be silently closed."""
    main_loop = asyncio.get_running_loop()
    pn.set_main_event_loop(main_loop)
    executed = asyncio.Event()

    async def _coro():
        executed.set()

    def _call_from_worker_thread():
        pn.schedule_prepayment_notify(_coro())

    try:
        await asyncio.to_thread(_call_from_worker_thread)
        await asyncio.wait_for(executed.wait(), timeout=2)
        assert executed.is_set()
    finally:
        pn._main_event_loop = None


@pytest.mark.asyncio
async def test_cross_thread_dispatch_retains_task_via_hold_bg_task():
    """The wrapped coroutine calls hold_bg_task(asyncio.current_task()) once it starts
    running on the main loop — the strong ref must actually land in the shared _bg_tasks
    set (not just get created and immediately GC-eligible)."""
    from app.utils import async_helpers

    main_loop = asyncio.get_running_loop()
    pn.set_main_event_loop(main_loop)
    started = asyncio.Event()
    release = asyncio.Event()

    async def _coro():
        started.set()
        await release.wait()

    def _call_from_worker_thread():
        pn.schedule_prepayment_notify(_coro())

    try:
        await asyncio.to_thread(_call_from_worker_thread)
        await asyncio.wait_for(started.wait(), timeout=2)
        assert any(not t.done() for t in async_helpers._bg_tasks)
        release.set()
    finally:
        pn._main_event_loop = None


def test_no_registered_main_loop_still_closes_coro_from_thread():
    """Without a registered main loop (e.g. a boot that never reached the lifespan's
    registration point), the no-running-loop caller still safely closes the coroutine —
    preserves the pre-fix behavior instead of leaking it."""
    pn._main_event_loop = None

    async def _coro():
        pass

    coro = _coro()
    with patch("asyncio.get_running_loop", side_effect=RuntimeError("no loop")):
        pn.schedule_prepayment_notify(coro)

    with pytest.raises(RuntimeError, match="cannot reuse already awaited coroutine"):
        coro.send(None)


def test_registered_but_stopped_main_loop_still_closes_coro():
    """A registered loop that is no longer running (e.g. shutdown mid-flight) must not
    be dispatched to — falls back to closing the coroutine safely."""
    import asyncio as _asyncio

    stopped_loop = _asyncio.new_event_loop()
    pn.set_main_event_loop(stopped_loop)

    async def _coro():
        pass

    coro = _coro()
    try:
        with patch("asyncio.get_running_loop", side_effect=RuntimeError("no loop")):
            pn.schedule_prepayment_notify(coro)
        with pytest.raises(RuntimeError, match="cannot reuse already awaited coroutine"):
            coro.send(None)
    finally:
        pn._main_event_loop = None
        stopped_loop.close()
