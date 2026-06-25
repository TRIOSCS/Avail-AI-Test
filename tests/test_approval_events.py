"""test_approval_events.py — TDD tests for ApprovalEventService (Task 5).

Tests:
  - decide() writes exactly one ApprovalEvent + one ActivityLog (no duplicates)
  - reassign() moves the from-user's recipient to REASSIGNED (sets reassigned_to_id),
    adds a new PENDING recipient for to_user, and records one ApprovalEvent
  - cancel() on a non-REQUESTED request raises ValueError
  - cancel() on a REQUESTED request sets status CANCELLED and records one ApprovalEvent
  - events.record() creates an ApprovalEvent and a matching ActivityLog row

Called by: pytest
Depends on: conftest (db_session), app.services.approvals.events,
            app.services.approvals.service, app.models.approvals, app.constants
"""

from decimal import Decimal

import pytest
from sqlalchemy import select

from app.constants import (
    ActivityType,
    ApprovalGateType,
    ApprovalRecipientStatus,
    ApprovalRequestStatus,
)
from app.models import ActivityLog
from app.models.approvals import (
    ApprovalEvent,
    ApprovalGateConfig,
    ApprovalRequest,
    ApprovalStep,
    ApprovalStepRecipient,
)
from app.services.approvals import create_request, decide
from app.services.approvals.events import cancel, reassign, record

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_user(db, email: str):
    from app.models import User

    u = User(email=email, name=email.split("@")[0])
    db.add(u)
    db.flush()
    return u


def _make_prepayment(db):
    from app.models.buy_plan import BuyPlan
    from app.models.quality_plan import Prepayment
    from app.models.quotes import Quote
    from app.models.sourcing import Requisition

    req = Requisition(name="RQ-EVENTS-TEST")
    db.add(req)
    db.flush()

    quote = Quote(requisition_id=req.id, quote_number="QQ-EVENTS-TEST", line_items=[])
    db.add(quote)
    db.flush()

    bp = BuyPlan(quote_id=quote.id, requisition_id=req.id)
    db.add(bp)
    db.flush()

    pp = Prepayment(buy_plan_id=bp.id, total_incl_fees=Decimal("2000.00"))
    db.add(pp)
    db.flush()
    return pp


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def actor(db_session):
    return _make_user(db_session, "actor@trioscs.com")


@pytest.fixture()
def approver(db_session):
    return _make_user(db_session, "approver@trioscs.com")


@pytest.fixture()
def delegate(db_session):
    return _make_user(db_session, "delegate@trioscs.com")


@pytest.fixture()
def open_request(db_session, approver):
    """An open PREPAYMENT ApprovalRequest routed to approver."""
    requester = _make_user(db_session, "buyer.ev@trioscs.com")
    db_session.add(
        ApprovalGateConfig(
            gate_type=ApprovalGateType.PREPAYMENT,
            approver_user_id=approver.id,
            max_amount=None,
            active=True,
        )
    )
    db_session.flush()

    subject = _make_prepayment(db_session)
    req = create_request(
        db_session,
        gate_type=ApprovalGateType.PREPAYMENT,
        amount=Decimal("2000.00"),
        subject=subject,
        requested_by=requester,
        owner=requester,
    )
    return req


# ── events.record() ───────────────────────────────────────────────────────────


def test_record_creates_approval_event(db_session, open_request, actor):
    """Record() appends an ApprovalEvent row."""
    record(db_session, open_request, actor, "submitted")
    events = (
        db_session.execute(select(ApprovalEvent).where(ApprovalEvent.request_id == open_request.id)).scalars().all()
    )
    assert any(e.event_type == "submitted" and e.actor_id == actor.id for e in events)


def test_record_creates_activity_log(db_session, open_request, actor):
    """Record() also writes one ActivityLog row with an ActivityType approval member."""
    record(db_session, open_request, actor, "submitted")
    logs = db_session.execute(select(ActivityLog)).scalars().all()
    assert any(log.activity_type == ActivityType.APPROVAL_REQUESTED for log in logs)


def test_record_passes_metadata_as_payload(db_session, open_request, actor):
    """Metadata dict lands on the ApprovalEvent.payload field."""
    record(db_session, open_request, actor, "submitted", metadata={"amount": 2000})
    events = (
        db_session.execute(select(ApprovalEvent).where(ApprovalEvent.request_id == open_request.id)).scalars().all()
    )
    assert any(e.payload and e.payload.get("amount") == 2000 for e in events)


# ── decide() → exactly one event + one ActivityLog ────────────────────────────


def test_decide_writes_exactly_one_event(db_session, open_request, approver):
    """Decide() must write exactly one ApprovalEvent (not an extra inline one)."""
    decide(db_session, open_request.id, approver, "approve")
    events = (
        db_session.execute(select(ApprovalEvent).where(ApprovalEvent.request_id == open_request.id)).scalars().all()
    )
    assert len(events) == 1
    assert events[0].event_type == "approved"


def test_decide_writes_exactly_one_activity_log(db_session, open_request, approver):
    """Decide() must write exactly one ActivityLog row via events.record()."""
    decide(db_session, open_request.id, approver, "approve")
    logs = db_session.execute(select(ActivityLog)).scalars().all()
    assert len(logs) == 1
    assert logs[0].activity_type in (
        ActivityType.APPROVAL_APPROVED,
        ActivityType.APPROVAL_REJECTED,
    )


# ── reassign() ────────────────────────────────────────────────────────────────


def test_reassign_marks_from_recipient_reassigned(db_session, open_request, approver, delegate):
    """Reassign() sets the from-user's recipient status to REASSIGNED with
    reassigned_to_id."""
    reassign(db_session, open_request.id, from_user=approver, to_user=delegate, actor=approver)

    from_recipient = db_session.execute(
        select(ApprovalStepRecipient)
        .join(ApprovalStep, ApprovalStepRecipient.step_id == ApprovalStep.id)
        .where(
            ApprovalStep.request_id == open_request.id,
            ApprovalStepRecipient.user_id == approver.id,
        )
    ).scalar_one()
    assert from_recipient.status == ApprovalRecipientStatus.REASSIGNED
    assert from_recipient.reassigned_to_id == delegate.id


def test_reassign_adds_new_pending_recipient(db_session, open_request, approver, delegate):
    """Reassign() adds a new PENDING recipient row for to_user."""
    reassign(db_session, open_request.id, from_user=approver, to_user=delegate, actor=approver)

    new_recipient = db_session.execute(
        select(ApprovalStepRecipient)
        .join(ApprovalStep, ApprovalStepRecipient.step_id == ApprovalStep.id)
        .where(
            ApprovalStep.request_id == open_request.id,
            ApprovalStepRecipient.user_id == delegate.id,
        )
    ).scalar_one_or_none()
    assert new_recipient is not None
    assert new_recipient.status == ApprovalRecipientStatus.PENDING


def test_reassign_records_event(db_session, open_request, approver, delegate):
    """Reassign() records one ApprovalEvent + one ActivityLog."""
    reassign(db_session, open_request.id, from_user=approver, to_user=delegate, actor=approver)
    events = (
        db_session.execute(select(ApprovalEvent).where(ApprovalEvent.request_id == open_request.id)).scalars().all()
    )
    assert any(e.event_type == "reassigned" for e in events)


def test_reassign_missing_from_recipient_raises(db_session, open_request, actor, delegate):
    """Reassign() raises ValueError when from_user has no PENDING recipient row."""
    with pytest.raises(ValueError, match="no pending recipient"):
        reassign(db_session, open_request.id, from_user=actor, to_user=delegate, actor=actor)


# ── cancel() ──────────────────────────────────────────────────────────────────


def test_cancel_terminal_request_raises(db_session, open_request, approver, actor):
    """Cancel() on an already-decided request raises ValueError."""
    decide(db_session, open_request.id, approver, "approve")
    with pytest.raises(ValueError, match="not open"):
        cancel(db_session, open_request.id, actor=actor)


def test_cancel_open_request_sets_cancelled(db_session, open_request, actor):
    """Cancel() transitions a REQUESTED request to CANCELLED."""
    cancel(db_session, open_request.id, actor=actor)
    req = db_session.get(ApprovalRequest, open_request.id)
    assert req.status == ApprovalRequestStatus.CANCELLED


def test_cancel_records_event(db_session, open_request, actor):
    """Cancel() records one ApprovalEvent with event_type='cancelled'."""
    cancel(db_session, open_request.id, actor=actor)
    events = (
        db_session.execute(select(ApprovalEvent).where(ApprovalEvent.request_id == open_request.id)).scalars().all()
    )
    assert any(e.event_type == "cancelled" for e in events)
