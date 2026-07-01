"""test_approval_routing.py — TDD tests for RoutingService (toggle model).

Tests: buy_plan gate routes to can_approve_buy_plans users; prepayment gate filters
       by can_approve_prepayments + prepayment_approval_limit; no eligible users raises
       NoEligibleApproverError; step rule=ANY + all recipients start PENDING.

Called by: pytest
Depends on: conftest (db_session), app.services.approvals.routing,
            app.models.approvals, app.models.auth, app.constants
"""

from decimal import Decimal

import pytest

from app.constants import ApprovalGateType, ApprovalRecipientStatus, ApprovalStepRule
from app.models.approvals import ApprovalRequest, ApprovalStep
from app.models.auth import User
from app.services.approvals.routing import NoEligibleApproverError, has_eligible_approver, route_request

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_user(
    db,
    email: str,
    *,
    can_approve_buy_plans: bool = False,
    can_approve_prepayments: bool = False,
    prepayment_approval_limit=None,
) -> User:
    u = User(
        email=email,
        name=email.split("@")[0],
        can_approve_buy_plans=can_approve_buy_plans,
        can_approve_prepayments=can_approve_prepayments,
        prepayment_approval_limit=prepayment_approval_limit,
    )
    db.add(u)
    db.flush()
    return u


def _make_request(db, gate: ApprovalGateType, amount: Decimal | None = None) -> ApprovalRequest:
    req = ApprovalRequest(gate_type=gate, amount=amount)
    db.add(req)
    db.flush()
    return req


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def prepayment_approvers(db_session):
    """Myrna (cap $1,000), Mike (unlimited), Marcus (unlimited) for PREPAYMENT gate."""
    myrna = _make_user(
        db_session,
        "myrna@trioscs.com",
        can_approve_prepayments=True,
        prepayment_approval_limit=Decimal("1000.00"),
    )
    mike = _make_user(
        db_session,
        "mike@trioscs.com",
        can_approve_prepayments=True,
        prepayment_approval_limit=None,
    )
    marcus = _make_user(
        db_session,
        "marcus@trioscs.com",
        can_approve_prepayments=True,
        prepayment_approval_limit=None,
    )
    return myrna, mike, marcus


# ── Buy-plan gate tests ───────────────────────────────────────────────────────


def test_buyplan_routes_to_can_approve_buy_plans_users(db_session):
    """BUY_PLAN gate routes to all users with can_approve_buy_plans=True."""
    alice = _make_user(db_session, "alice@trioscs.com", can_approve_buy_plans=True)
    bob = _make_user(db_session, "bob@trioscs.com", can_approve_buy_plans=True)
    # charlie has the toggle off — must not be routed
    _make_user(db_session, "charlie@trioscs.com", can_approve_buy_plans=False)

    req = _make_request(db_session, ApprovalGateType.BUY_PLAN)
    step = route_request(db_session, req)

    user_ids = {r.user_id for r in step.recipients}
    assert user_ids == {alice.id, bob.id}


def test_buyplan_no_eligible_raises(db_session):
    """BUY_PLAN gate with no can_approve_buy_plans users raises
    NoEligibleApproverError."""
    _make_user(db_session, "nobody@trioscs.com", can_approve_buy_plans=False)
    req = _make_request(db_session, ApprovalGateType.BUY_PLAN)

    with pytest.raises(NoEligibleApproverError):
        route_request(db_session, req)


# ── Prepayment gate tests ─────────────────────────────────────────────────────


def test_all_three_eligible_for_small_amount(db_session, prepayment_approvers):
    """$400 request routes to all three (Myrna cap 1000 >= 400)."""
    myrna, mike, marcus = prepayment_approvers
    req = _make_request(db_session, ApprovalGateType.PREPAYMENT, Decimal("400.00"))

    step = route_request(db_session, req)

    user_ids = {r.user_id for r in step.recipients}
    assert user_ids == {myrna.id, mike.id, marcus.id}


def test_threshold_excludes_capped_approver(db_session, prepayment_approvers):
    """$2,500 request excludes Myrna (cap 1,000) — only Mike + Marcus eligible."""
    myrna, mike, marcus = prepayment_approvers
    req = _make_request(db_session, ApprovalGateType.PREPAYMENT, Decimal("2500.00"))

    step = route_request(db_session, req)

    user_ids = {r.user_id for r in step.recipients}
    assert user_ids == {mike.id, marcus.id}
    assert myrna.id not in user_ids


def test_exact_boundary_is_eligible(db_session, prepayment_approvers):
    """$1,000.00 request includes Myrna — boundary is inclusive (amount <= limit)."""
    myrna, mike, marcus = prepayment_approvers
    req = _make_request(db_session, ApprovalGateType.PREPAYMENT, Decimal("1000.00"))

    step = route_request(db_session, req)

    user_ids = {r.user_id for r in step.recipients}
    assert myrna.id in user_ids
    assert user_ids == {myrna.id, mike.id, marcus.id}


def test_prepayment_toggle_off_not_routed(db_session):
    """Users with can_approve_prepayments=False are never routed even if active."""
    _make_user(db_session, "disabled@trioscs.com", can_approve_prepayments=False)
    req = _make_request(db_session, ApprovalGateType.PREPAYMENT, Decimal("500.00"))

    with pytest.raises(NoEligibleApproverError):
        route_request(db_session, req)


def test_prepayment_no_eligible_approver_raises(db_session):
    """Gate with zero eligible users raises NoEligibleApproverError."""
    req = _make_request(db_session, ApprovalGateType.PREPAYMENT, Decimal("500.00"))

    with pytest.raises(NoEligibleApproverError):
        route_request(db_session, req)


# ── Step + recipient invariants ───────────────────────────────────────────────


def test_step_has_any_rule(db_session, prepayment_approvers):
    """Created step uses rule=ANY."""
    req = _make_request(db_session, ApprovalGateType.PREPAYMENT, Decimal("400.00"))
    step = route_request(db_session, req)

    assert step.rule == ApprovalStepRule.ANY


def test_recipients_have_pending_status(db_session, prepayment_approvers):
    """All created recipients start as PENDING."""
    req = _make_request(db_session, ApprovalGateType.PREPAYMENT, Decimal("400.00"))
    step = route_request(db_session, req)

    for recipient in step.recipients:
        assert recipient.status == ApprovalRecipientStatus.PENDING


def test_step_linked_to_request(db_session, prepayment_approvers):
    """Created step is linked to the correct request."""
    req = _make_request(db_session, ApprovalGateType.PREPAYMENT, Decimal("400.00"))
    step = route_request(db_session, req)

    assert step.request_id == req.id
    assert isinstance(step, ApprovalStep)


class TestHasEligibleApprover:
    """has_eligible_approver mirrors route_request's eligibility without creating a step
    — used to detect (and surface) a plan that would silently stall for lack of an
    approver."""

    def test_true_when_active_buy_plan_approver_exists(self, db_session):
        db_session.add(User(email="a@trioscs.com", name="A", is_active=True, can_approve_buy_plans=True))
        db_session.flush()
        assert has_eligible_approver(db_session, ApprovalGateType.BUY_PLAN) is True

    def test_false_when_no_buy_plan_approver(self, db_session):
        db_session.add(User(email="b@trioscs.com", name="B", is_active=True, can_approve_buy_plans=False))
        db_session.flush()
        assert has_eligible_approver(db_session, ApprovalGateType.BUY_PLAN) is False

    def test_inactive_approver_excluded(self, db_session):
        db_session.add(User(email="c@trioscs.com", name="C", is_active=False, can_approve_buy_plans=True))
        db_session.flush()
        assert has_eligible_approver(db_session, ApprovalGateType.BUY_PLAN) is False

    def test_po_gate_respects_dollar_limit(self, db_session):
        db_session.add(
            User(
                email="d@trioscs.com",
                name="D",
                is_active=True,
                can_approve_purchase_orders=True,
                purchase_order_approval_limit=Decimal("1000"),
            )
        )
        db_session.flush()
        assert has_eligible_approver(db_session, ApprovalGateType.PURCHASE_ORDER, Decimal("500")) is True
        assert has_eligible_approver(db_session, ApprovalGateType.PURCHASE_ORDER, Decimal("5000")) is False
