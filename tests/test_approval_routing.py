"""test_approval_routing.py — TDD tests for RoutingService (Task 3).

Tests: threshold eligibility (all three / exact boundary / capped excluded),
       no-config gate raises NoEligibleApproverError.

Called by: pytest
Depends on: conftest (db_session, test_user), app.services.approvals.routing,
            app.models.approvals, app.constants
"""

from decimal import Decimal

import pytest

from app.constants import ApprovalGateType, ApprovalRecipientStatus, ApprovalStepRule
from app.models.approvals import ApprovalGateConfig, ApprovalRequest, ApprovalStep
from app.services.approvals.routing import NoEligibleApproverError, route_request

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_user(db, email: str):
    from app.models import User

    u = User(email=email, name=email.split("@")[0])
    db.add(u)
    db.flush()
    return u


def _make_request(db, gate: ApprovalGateType, amount: Decimal) -> ApprovalRequest:
    req = ApprovalRequest(gate_type=gate, amount=amount)
    db.add(req)
    db.flush()
    return req


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def three_approvers(db_session):
    """Myrna (cap $1000), Mike (no cap), Marcus (no cap) for PREPAYMENT gate."""
    myrna = _make_user(db_session, "myrna@trioscs.com")
    mike = _make_user(db_session, "mike@trioscs.com")
    marcus = _make_user(db_session, "marcus@trioscs.com")

    configs = [
        ApprovalGateConfig(
            gate_type=ApprovalGateType.PREPAYMENT,
            approver_user_id=myrna.id,
            max_amount=Decimal("1000.00"),
            active=True,
        ),
        ApprovalGateConfig(
            gate_type=ApprovalGateType.PREPAYMENT,
            approver_user_id=mike.id,
            max_amount=None,
            active=True,
        ),
        ApprovalGateConfig(
            gate_type=ApprovalGateType.PREPAYMENT,
            approver_user_id=marcus.id,
            max_amount=None,
            active=True,
        ),
    ]
    for c in configs:
        db_session.add(c)
    db_session.flush()

    return myrna, mike, marcus


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_all_three_eligible_for_small_amount(db_session, three_approvers):
    """$400 request routes to all three (Myrna cap 1000 >= 400)."""
    myrna, mike, marcus = three_approvers
    req = _make_request(db_session, ApprovalGateType.PREPAYMENT, Decimal("400.00"))

    step = route_request(db_session, req)

    user_ids = {r.user_id for r in step.recipients}
    assert user_ids == {myrna.id, mike.id, marcus.id}


def test_threshold_excludes_capped_approver(db_session, three_approvers):
    """$2500 request excludes Myrna (cap 1000) — only Mike + Marcus eligible."""
    myrna, mike, marcus = three_approvers
    req = _make_request(db_session, ApprovalGateType.PREPAYMENT, Decimal("2500.00"))

    step = route_request(db_session, req)

    user_ids = {r.user_id for r in step.recipients}
    assert user_ids == {mike.id, marcus.id}
    assert myrna.id not in user_ids


def test_exact_boundary_is_eligible(db_session, three_approvers):
    """$1000.00 request should include Myrna — boundary is inclusive (amount <=
    max_amount)."""
    myrna, mike, marcus = three_approvers
    req = _make_request(db_session, ApprovalGateType.PREPAYMENT, Decimal("1000.00"))

    step = route_request(db_session, req)

    user_ids = {r.user_id for r in step.recipients}
    assert myrna.id in user_ids
    assert user_ids == {myrna.id, mike.id, marcus.id}


def test_step_has_any_rule(db_session, three_approvers):
    """Created step uses rule=ANY."""
    _, _, _ = three_approvers
    req = _make_request(db_session, ApprovalGateType.PREPAYMENT, Decimal("400.00"))

    step = route_request(db_session, req)

    assert step.rule == ApprovalStepRule.ANY


def test_recipients_have_pending_status(db_session, three_approvers):
    """All created recipients start as PENDING."""
    _, _, _ = three_approvers
    req = _make_request(db_session, ApprovalGateType.PREPAYMENT, Decimal("400.00"))

    step = route_request(db_session, req)

    for recipient in step.recipients:
        assert recipient.status == ApprovalRecipientStatus.PENDING


def test_step_linked_to_request(db_session, three_approvers):
    """Created step is linked to the correct request."""
    _, _, _ = three_approvers
    req = _make_request(db_session, ApprovalGateType.PREPAYMENT, Decimal("400.00"))

    step = route_request(db_session, req)

    assert step.request_id == req.id
    assert isinstance(step, ApprovalStep)


def test_no_eligible_approver_error_when_no_config(db_session):
    """Gate with zero configs raises NoEligibleApproverError."""
    req = _make_request(db_session, ApprovalGateType.PREPAYMENT, Decimal("500.00"))

    with pytest.raises(NoEligibleApproverError):
        route_request(db_session, req)


def test_inactive_config_excluded(db_session):
    """Inactive config rows are not considered eligible approvers."""
    user = _make_user(db_session, "inactive@trioscs.com")
    db_session.add(
        ApprovalGateConfig(
            gate_type=ApprovalGateType.PREPAYMENT,
            approver_user_id=user.id,
            max_amount=None,
            active=False,  # inactive — should not route to this user
        )
    )
    db_session.flush()

    req = _make_request(db_session, ApprovalGateType.PREPAYMENT, Decimal("500.00"))

    with pytest.raises(NoEligibleApproverError):
        route_request(db_session, req)
