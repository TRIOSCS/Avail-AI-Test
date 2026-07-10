"""Tests for app/services/requirement_status.py — per-part sourcing status.

Covers:
- Requirement sourcing status transitions (open → sourcing → offered → quoted → won/lost)
- Illegal transitions raise ValueError
- on_rfq_sent marks parts as sourcing
- on_offer_created advances to offered
- on_quote_built advances to quoted
- Buyer claim/unclaim on requisitions
- ActivityLog created on transitions
"""

import os

os.environ["TESTING"] = "1"

from datetime import UTC, datetime

import pytest

from app.constants import ActivityType
from app.constants import SourcingStatus as RequirementSourcingStatus
from app.models import ActivityLog, User
from app.services.requirement_status import (
    ALLOWED_TRANSITIONS,
    claim_requisition,
    on_offer_created,
    on_quote_built,
    on_rfq_sent,
    transition_requirement,
    unclaim_requisition,
)


def _first_requirement(test_requisition, db_session, sourcing_status):
    """Return the requisition's first requirement with its sourcing_status set +
    committed."""
    req_item = test_requisition.requirements[0]
    req_item.sourcing_status = sourcing_status
    db_session.commit()
    return req_item


class TestTransitionRequirement:
    def test_open_to_sourcing(self, db_session, test_requisition, test_user):
        req_item = _first_requirement(test_requisition, db_session, "open")

        changed = transition_requirement(req_item, "sourcing", db_session, actor=test_user)
        assert changed is True
        assert req_item.sourcing_status == "sourcing"

    def test_with_enum(self, db_session, test_requisition, test_user):
        req_item = _first_requirement(test_requisition, db_session, "open")

        changed = transition_requirement(req_item, RequirementSourcingStatus.OFFERED, db_session, actor=test_user)
        assert changed is True
        assert req_item.sourcing_status == "offered"

    def test_illegal_transition_raises(self, db_session, test_requisition, test_user):
        req_item = _first_requirement(test_requisition, db_session, "won")

        with pytest.raises(ValueError, match="Invalid requirement transition"):
            transition_requirement(req_item, "open", db_session, actor=test_user)

    def test_noop_when_same_status(self, db_session, test_requisition, test_user):
        req_item = _first_requirement(test_requisition, db_session, "sourcing")

        changed = transition_requirement(req_item, "sourcing", db_session, actor=test_user)
        assert changed is False

    def test_creates_activity_log(self, db_session, test_requisition, test_user):
        req_item = _first_requirement(test_requisition, db_session, "open")

        transition_requirement(req_item, "sourcing", db_session, actor=test_user)
        db_session.flush()

        logs = (
            db_session.query(ActivityLog)
            .filter_by(
                requisition_id=test_requisition.id,
                activity_type="part_status_change",
            )
            .all()
        )
        assert len(logs) == 1
        assert "LM317T" in logs[0].subject
        assert "open → sourcing" in logs[0].subject

    def test_none_status_defaults_to_open(self, db_session, test_requisition, test_user):
        req_item = _first_requirement(test_requisition, db_session, None)

        transition_requirement(req_item, "sourcing", db_session, actor=test_user)
        assert req_item.sourcing_status == "sourcing"

    def test_all_transitions_defined(self):
        for status in RequirementSourcingStatus:
            assert status.value in ALLOWED_TRANSITIONS


class TestOnRfqSent:
    def test_marks_parts_as_sourcing(self, db_session, test_requisition, test_user):
        req_item = _first_requirement(test_requisition, db_session, "open")

        changed = on_rfq_sent([req_item.id], db_session, actor=test_user)
        assert changed == 1
        assert req_item.sourcing_status == "sourcing"

    @pytest.mark.parametrize("initial_status", ["sourcing", "offered"])
    def test_skips_non_open(self, db_session, test_requisition, test_user, initial_status):
        req_item = _first_requirement(test_requisition, db_session, initial_status)

        changed = on_rfq_sent([req_item.id], db_session, actor=test_user)
        assert changed == 0


class TestOnOfferCreated:
    @pytest.mark.parametrize("initial_status", ["open", "sourcing"])
    def test_advances_to_offered(self, db_session, test_requisition, test_user, initial_status):
        req_item = _first_requirement(test_requisition, db_session, initial_status)

        changed = on_offer_created(req_item, db_session, actor=test_user)
        assert changed is True
        assert req_item.sourcing_status == "offered"

    def test_does_not_demote_from_quoted(self, db_session, test_requisition, test_user):
        req_item = _first_requirement(test_requisition, db_session, "quoted")

        changed = on_offer_created(req_item, db_session, actor=test_user)
        assert changed is False
        assert req_item.sourcing_status == "quoted"


class TestOnQuoteBuilt:
    def test_marks_offered_as_quoted(self, db_session, test_requisition, test_user):
        req_item = _first_requirement(test_requisition, db_session, "offered")

        changed = on_quote_built([req_item.id], db_session, actor=test_user)
        assert changed == 1
        assert req_item.sourcing_status == "quoted"

    def test_skips_already_won(self, db_session, test_requisition, test_user):
        req_item = _first_requirement(test_requisition, db_session, "won")

        changed = on_quote_built([req_item.id], db_session, actor=test_user)
        assert changed == 0


class TestClaimRequisition:
    def test_buyer_claims_requisition(self, db_session, test_requisition, test_user):
        test_requisition.claimed_by_id = None
        db_session.commit()

        changed = claim_requisition(test_requisition, test_user, db_session)
        assert changed is True
        assert test_requisition.claimed_by_id == test_user.id
        assert test_requisition.claimed_at is not None

    def test_already_claimed_by_same_user(self, db_session, test_requisition, test_user):
        test_requisition.claimed_by_id = test_user.id
        db_session.commit()

        changed = claim_requisition(test_requisition, test_user, db_session)
        assert changed is False

    def test_claimed_by_different_user_raises(self, db_session, test_requisition, test_user):
        other_user = User(
            email="otherbuyer@trioscs.com",
            name="Other Buyer",
            role="buyer",
            azure_id="test-azure-other",
        )
        db_session.add(other_user)
        db_session.commit()

        test_requisition.claimed_by_id = other_user.id
        db_session.commit()

        with pytest.raises(ValueError, match="already claimed"):
            claim_requisition(test_requisition, test_user, db_session)

    def test_claim_creates_activity_log(self, db_session, test_requisition, test_user):
        test_requisition.claimed_by_id = None
        db_session.commit()

        claim_requisition(test_requisition, test_user, db_session)
        db_session.flush()

        logs = (
            db_session.query(ActivityLog)
            .filter_by(
                requisition_id=test_requisition.id,
                activity_type=ActivityType.ASSIGNMENT_CHANGED,
            )
            .all()
        )
        assert len(logs) == 1


class TestUnclaimRequisition:
    def test_unclaim(self, db_session, test_requisition, test_user):
        test_requisition.claimed_by_id = test_user.id
        test_requisition.claimed_at = datetime.now(UTC)
        db_session.commit()

        changed = unclaim_requisition(test_requisition, db_session, actor=test_user)
        assert changed is True
        assert test_requisition.claimed_by_id is None
        assert test_requisition.claimed_at is None

    def test_unclaim_already_unclaimed(self, db_session, test_requisition, test_user):
        test_requisition.claimed_by_id = None
        db_session.commit()

        changed = unclaim_requisition(test_requisition, db_session, actor=test_user)
        assert changed is False


class TestValidatorParity:
    """Both per-part sourcing validators must agree for every transition.

    ``transition_requirement`` (requirement_status.py) and
    ``validate_transition("requirement", …)`` (status_machine.py) previously used
    two divergent tables, so a transition's legality depended on which validator a
    caller happened to hit. They now share ``status_machine.SOURCING_TRANSITIONS``
    as the single source of truth; this parametrized matrix guards re-divergence.
    """

    def test_tables_are_the_same_object(self):
        from app.services.status_machine import SOURCING_TRANSITIONS

        assert ALLOWED_TRANSITIONS is SOURCING_TRANSITIONS

    @pytest.mark.parametrize("to_status", list(RequirementSourcingStatus))
    @pytest.mark.parametrize("from_status", list(RequirementSourcingStatus))
    def test_both_validators_agree(self, db_session, test_requisition, test_user, from_status, to_status):
        from app.services.status_machine import validate_transition

        if from_status == to_status:
            # No-op: status_machine treats it as valid, transition_requirement as
            # a no-change (False). Neither rejects — they agree.
            assert validate_transition("requirement", from_status.value, to_status.value) is True
            req_item = _first_requirement(test_requisition, db_session, from_status.value)
            assert transition_requirement(req_item, to_status.value, db_session, actor=test_user) is False
            return

        expected_legal = to_status.value in ALLOWED_TRANSITIONS.get(from_status.value, set())

        # status_machine validator
        sm_legal = validate_transition("requirement", from_status.value, to_status.value, raise_on_invalid=False)
        assert sm_legal is expected_legal

        # requirement_status validator (raises ValueError on an illegal transition)
        req_item = _first_requirement(test_requisition, db_session, from_status.value)
        if expected_legal:
            assert transition_requirement(req_item, to_status.value, db_session, actor=test_user) is True
            assert req_item.sourcing_status == to_status.value
        else:
            with pytest.raises(ValueError):
                transition_requirement(req_item, to_status.value, db_session, actor=test_user)
