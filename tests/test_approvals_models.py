"""test_approvals_models.py — ORM smoke tests for the approval engine models.

Tests: ApprovalRequest / ApprovalStep / ApprovalStepRecipient chain integrity,
       per-user prepayment approval toggle + limit on User, QualityPlan, and
       Prepayment.

Called by: pytest
Depends on: conftest (db_session, test_user), app.models.approvals,
            app.models.auth, app.models.quality_plan, app.models.offers,
            app.constants
"""

from decimal import Decimal

import pytest

from app.constants import (
    ApprovalGateType,
    ApprovalRequestStatus,
    PaymentMethod,
    QPOrderType,
    QualityPlanStatus,
)
from app.models.approvals import (
    ApprovalEvent,
    ApprovalOutbox,
    ApprovalRequest,
    ApprovalStep,
    ApprovalStepRecipient,
)
from app.models.auth import User
from app.models.quality_plan import Prepayment, QualityPlan

# ── Core chain test (from brief) ───────────────────────────────────────────────


def test_request_step_recipient_chain(db_session, test_user):
    req = ApprovalRequest(
        gate_type=ApprovalGateType.PREPAYMENT,
        status=ApprovalRequestStatus.REQUESTED,
        amount=Decimal("400.00"),
        currency="USD",
        requested_by_id=test_user.id,
        owner_id=test_user.id,
    )
    db_session.add(req)
    db_session.flush()

    step = ApprovalStep(request_id=req.id, seq=1, rule="any", status="pending")
    db_session.add(step)
    db_session.flush()

    rec = ApprovalStepRecipient(step_id=step.id, user_id=test_user.id, status="pending")
    db_session.add(rec)
    db_session.flush()

    assert rec.id is not None
    assert step.request_id == req.id


# ── Per-user prepayment toggle + limit ────────────────────────────────────────


def test_user_prepayment_toggle_and_limit(db_session):
    """can_approve_prepayments toggle + prepayment_approval_limit persists correctly."""
    u = User(
        email="myrna@trioscs.com",
        name="Myrna",
        can_approve_prepayments=True,
        prepayment_approval_limit=Decimal("1000.00"),
    )
    db_session.add(u)
    db_session.flush()

    assert u.can_approve_prepayments is True
    assert u.prepayment_approval_limit == Decimal("1000.00")


def test_user_prepayment_unlimited(db_session):
    """prepayment_approval_limit=None means unlimited — column accepts NULL."""
    u = User(
        email="mike@trioscs.com",
        name="Mike",
        can_approve_prepayments=True,
        prepayment_approval_limit=None,
    )
    db_session.add(u)
    db_session.flush()

    assert u.can_approve_prepayments is True
    assert u.prepayment_approval_limit is None


def test_user_prepayment_defaults_off(db_session):
    """can_approve_prepayments defaults to False for a new user."""
    u = User(email="newuser@trioscs.com", name="New")
    db_session.add(u)
    db_session.flush()

    assert u.can_approve_prepayments is False
    assert u.prepayment_approval_limit is None


# ── ApprovalEvent append-only ──────────────────────────────────────────────────


def test_approval_event_created(db_session, test_user):
    req = ApprovalRequest(
        gate_type=ApprovalGateType.BUY_PLAN,
        status=ApprovalRequestStatus.REQUESTED,
        requested_by_id=test_user.id,
        owner_id=test_user.id,
    )
    db_session.add(req)
    db_session.flush()

    evt = ApprovalEvent(
        request_id=req.id,
        actor_id=test_user.id,
        event_type="submitted",
        payload={"gate": "buy_plan", "comment": "Submitted for review"},
    )
    db_session.add(evt)
    db_session.flush()

    assert evt.id is not None
    assert evt.event_type == "submitted"


# ── ApprovalOutbox dispatch record ────────────────────────────────────────────


def test_approval_outbox_record(db_session, test_user):
    req = ApprovalRequest(
        gate_type=ApprovalGateType.PREPAYMENT,
        status=ApprovalRequestStatus.REQUESTED,
        requested_by_id=test_user.id,
        owner_id=test_user.id,
    )
    db_session.add(req)
    db_session.flush()

    outbox = ApprovalOutbox(
        request_id=req.id,
        recipient_user_id=test_user.id,
        channel="email",
        payload={"subject": "Approval needed"},
    )
    db_session.add(outbox)
    db_session.flush()

    assert outbox.id is not None
    assert outbox.sent_at is None
    assert outbox.fail_count == 0


# ── UniqueConstraint on step + user ──────────────────────────────────────────


def test_step_recipient_unique_constraint(db_session, test_user):
    req = ApprovalRequest(
        gate_type=ApprovalGateType.PREPAYMENT,
        status=ApprovalRequestStatus.REQUESTED,
        requested_by_id=test_user.id,
        owner_id=test_user.id,
    )
    db_session.add(req)
    db_session.flush()

    step = ApprovalStep(request_id=req.id, seq=1, rule="any", status="pending")
    db_session.add(step)
    db_session.flush()

    rec1 = ApprovalStepRecipient(step_id=step.id, user_id=test_user.id, status="pending")
    db_session.add(rec1)
    db_session.flush()

    rec2 = ApprovalStepRecipient(step_id=step.id, user_id=test_user.id, status="pending")
    db_session.add(rec2)
    with pytest.raises(Exception):  # IntegrityError (unique constraint)
        db_session.flush()


# ── QualityPlan basic create ──────────────────────────────────────────────────


def test_quality_plan_create(db_session, test_user, test_buy_plan):
    qp = QualityPlan(
        buy_plan_id=test_buy_plan.id,
        status=QualityPlanStatus.DRAFT,
        order_type=QPOrderType.NEW,
        inspection_level="AQL 1.5",
        created_by_id=test_user.id,
    )
    db_session.add(qp)
    db_session.flush()

    assert qp.id is not None
    assert qp.status == QualityPlanStatus.DRAFT


# ── Prepayment basic create ───────────────────────────────────────────────────


def test_prepayment_create(db_session, test_user, test_buy_plan):
    pp = Prepayment(
        buy_plan_id=test_buy_plan.id,
        total_incl_fees=Decimal("5000.00"),
        currency="USD",
        payment_method=PaymentMethod.WIRE,
        test_report_sent=False,
        buyer_remarks="Prepay 50% upfront per vendor terms",
        created_by_id=test_user.id,
    )
    db_session.add(pp)
    db_session.flush()

    assert pp.id is not None
    assert pp.total_incl_fees == Decimal("5000.00")
    assert pp.test_report_sent is False
