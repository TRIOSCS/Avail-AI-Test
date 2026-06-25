"""test_approval_service.py — TDD tests for ApprovalService (Task 4).

Tests: first-responder-wins (one approve closes the request; a second decide on a
       terminal request raises ValueError → idempotent), reject requires a non-blank
       comment, a non-recipient is forbidden (PermissionError). Also covers
       create_request wiring (subject FK set, request routed to recipients) and the
       decided-outbox + audit-event side effects.

Called by: pytest
Depends on: conftest (db_session), app.services.approvals.service,
            app.models.approvals, app.models.quality_plan, app.constants
"""

from decimal import Decimal

import pytest
from sqlalchemy import select

from app.constants import (
    ApprovalGateType,
    ApprovalRecipientStatus,
    ApprovalRequestStatus,
)
from app.models.approvals import (
    ApprovalEvent,
    ApprovalGateConfig,
    ApprovalOutbox,
    ApprovalRequest,
    ApprovalStep,
    ApprovalStepRecipient,
)
from app.services.approvals.service import create_request, decide

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_user(db, email: str):
    from app.models import User

    u = User(email=email, name=email.split("@")[0])
    db.add(u)
    db.flush()
    return u


def _make_prepayment(db):
    """Build the minimal Requisition → Quote → BuyPlan → Prepayment chain.

    Prepayment.buy_plan_id is NOT NULL and FKs are enforced under the SQLite test
    engine, so a real flushed subject row is required to exercise create_request's
    subject-FK wiring.
    """
    from app.models.buy_plan import BuyPlan
    from app.models.quality_plan import Prepayment
    from app.models.quotes import Quote
    from app.models.sourcing import Requisition

    req = Requisition(name="RQ-APPROVAL-TEST")
    db.add(req)
    db.flush()

    quote = Quote(requisition_id=req.id, quote_number="QQ-APPROVAL-TEST", line_items=[])
    db.add(quote)
    db.flush()

    bp = BuyPlan(quote_id=quote.id, requisition_id=req.id)
    db.add(bp)
    db.flush()

    pp = Prepayment(buy_plan_id=bp.id, total_incl_fees=Decimal("5000.00"))
    db.add(pp)
    db.flush()
    return pp


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def mike(db_session):
    return _make_user(db_session, "mike@trioscs.com")


@pytest.fixture()
def marcus(db_session):
    return _make_user(db_session, "marcus@trioscs.com")


@pytest.fixture()
def other_user(db_session):
    return _make_user(db_session, "outsider@trioscs.com")


@pytest.fixture()
def prepayment_request_with_two_recipients(db_session, mike, marcus):
    """A PREPAYMENT ApprovalRequest routed to Mike + Marcus (both no-cap, PENDING).

    Built through the production create_request path against a real Prepayment subject.
    """
    requester = _make_user(db_session, "buyer@trioscs.com")
    for approver in (mike, marcus):
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
        amount=Decimal("5000.00"),
        subject=subject,
        requested_by=requester,
        owner=requester,
    )
    return req


# ── create_request ─────────────────────────────────────────────────────────────


def test_create_request_persists_and_routes(db_session, prepayment_request_with_two_recipients, mike, marcus):
    """create_request persists the request, sets the subject FK, and routes to both."""
    req = prepayment_request_with_two_recipients
    assert req.id is not None
    assert req.status == ApprovalRequestStatus.REQUESTED
    assert req.subject_prepayment_id is not None
    assert req.subject_quality_plan_id is None

    recipient_user_ids = {
        r.user_id
        for r in db_session.execute(
            select(ApprovalStepRecipient).join(ApprovalStepRecipient.step).where(ApprovalStep.request_id == req.id)
        ).scalars()
    }
    assert recipient_user_ids == {mike.id, marcus.id}


# ── decide: first-responder-wins / idempotency ──────────────────────────────────


def test_first_responder_wins(db_session, prepayment_request_with_two_recipients, mike, marcus):
    req = prepayment_request_with_two_recipients
    decide(db_session, req.id, mike, "approve")
    assert db_session.get(ApprovalRequest, req.id).status == ApprovalRequestStatus.APPROVED

    with pytest.raises(ValueError):  # already decided → terminal
        decide(db_session, req.id, marcus, "approve")


def test_decide_records_recipient_decision(db_session, prepayment_request_with_two_recipients, mike):
    """The acting recipient's own row flips to APPROVED with a decided_at timestamp."""
    req = prepayment_request_with_two_recipients
    decide(db_session, req.id, mike, "approve")

    recipient = db_session.execute(
        select(ApprovalStepRecipient)
        .join(ApprovalStepRecipient.step)
        .where(ApprovalStep.request_id == req.id, ApprovalStepRecipient.user_id == mike.id)
    ).scalar_one()
    assert recipient.status == ApprovalRecipientStatus.APPROVED
    assert recipient.decided_at is not None


def test_decide_enqueues_outbox_and_event(db_session, prepayment_request_with_two_recipients, mike):
    """One 'decided' outbox row and an audit event are written on resolution."""
    req = prepayment_request_with_two_recipients
    decide(db_session, req.id, mike, "approve")

    outbox = db_session.execute(select(ApprovalOutbox).where(ApprovalOutbox.request_id == req.id)).scalars().all()
    assert len(outbox) == 1
    assert (outbox[0].payload or {}).get("event_type") == "decided"

    events = db_session.execute(select(ApprovalEvent).where(ApprovalEvent.request_id == req.id)).scalars().all()
    assert any(e.event_type == "approved" for e in events)


# ── decide: reject path ─────────────────────────────────────────────────────────


def test_reject_requires_reason(db_session, prepayment_request_with_two_recipients, mike):
    with pytest.raises(ValueError):
        decide(db_session, prepayment_request_with_two_recipients.id, mike, "reject", comment="")


def test_reject_with_reason_closes_request(db_session, prepayment_request_with_two_recipients, mike):
    req = prepayment_request_with_two_recipients
    decide(db_session, req.id, mike, "reject", comment="Vendor unverified")
    assert db_session.get(ApprovalRequest, req.id).status == ApprovalRequestStatus.REJECTED


# ── decide: authorization ───────────────────────────────────────────────────────


def test_non_recipient_forbidden(db_session, prepayment_request_with_two_recipients, other_user):
    with pytest.raises(PermissionError):
        decide(db_session, prepayment_request_with_two_recipients.id, other_user, "approve")


def test_unknown_action_rejected(db_session, prepayment_request_with_two_recipients, mike):
    with pytest.raises(ValueError):
        decide(db_session, prepayment_request_with_two_recipients.id, mike, "maybe")


# ── decide: missing request ──────────────────────────────────────────────────────


def test_unknown_request_id_raises(db_session, mike):
    """Decide raises ValueError with a clear message for a non-existent request id."""
    with pytest.raises(ValueError, match="ApprovalRequest 999999 not found"):
        decide(db_session, 999999, mike, "approve")
