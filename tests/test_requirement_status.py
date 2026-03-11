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

from datetime import datetime, timezone

import pytest

from app.enums import RequirementSourcingStatus
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


class TestTransitionRequirement:
    def test_open_to_sourcing(self, db_session, test_requisition, test_user):
        req_item = test_requisition.requirements[0]
        req_item.sourcing_status = "open"
        db_session.commit()

        changed = transition_requirement(req_item, "sourcing", db_session, actor=test_user)
        assert changed is True
        assert req_item.sourcing_status == "sourcing"

    def test_with_enum(self, db_session, test_requisition, test_user):
        req_item = test_requisition.requirements[0]
        req_item.sourcing_status = "open"
        db_session.commit()

        changed = transition_requirement(req_item, RequirementSourcingStatus.offered, db_session, actor=test_user)
        assert changed is True
        assert req_item.sourcing_status == "offered"

    def test_illegal_transition_raises(self, db_session, test_requisition, test_user):
        req_item = test_requisition.requirements[0]
        req_item.sourcing_status = "won"
        db_session.commit()

        with pytest.raises(ValueError, match="Invalid requirement transition"):
            transition_requirement(req_item, "open", db_session, actor=test_user)

    def test_noop_when_same_status(self, db_session, test_requisition, test_user):
        req_item = test_requisition.requirements[0]
        req_item.sourcing_status = "sourcing"
        db_session.commit()

        changed = transition_requirement(req_item, "sourcing", db_session, actor=test_user)
        assert changed is False

    def test_creates_activity_log(self, db_session, test_requisition, test_user):
        req_item = test_requisition.requirements[0]
        req_item.sourcing_status = "open"
        db_session.commit()

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
        req_item = test_requisition.requirements[0]
        req_item.sourcing_status = None
        db_session.commit()

        transition_requirement(req_item, "sourcing", db_session, actor=test_user)
        assert req_item.sourcing_status == "sourcing"

    def test_all_transitions_defined(self):
        for status in RequirementSourcingStatus:
            assert status.value in ALLOWED_TRANSITIONS


class TestOnRfqSent:
    def test_marks_parts_as_sourcing(self, db_session, test_requisition, test_user):
        req_item = test_requisition.requirements[0]
        req_item.sourcing_status = "open"
        db_session.commit()

        changed = on_rfq_sent([req_item.id], db_session, actor=test_user)
        assert changed == 1
        assert req_item.sourcing_status == "sourcing"

    def test_skips_already_sourcing(self, db_session, test_requisition, test_user):
        req_item = test_requisition.requirements[0]
        req_item.sourcing_status = "sourcing"
        db_session.commit()

        changed = on_rfq_sent([req_item.id], db_session, actor=test_user)
        assert changed == 0

    def test_skips_offered(self, db_session, test_requisition, test_user):
        req_item = test_requisition.requirements[0]
        req_item.sourcing_status = "offered"
        db_session.commit()

        changed = on_rfq_sent([req_item.id], db_session, actor=test_user)
        assert changed == 0


class TestOnOfferCreated:
    def test_open_to_offered(self, db_session, test_requisition, test_user):
        req_item = test_requisition.requirements[0]
        req_item.sourcing_status = "open"
        db_session.commit()

        changed = on_offer_created(req_item, db_session, actor=test_user)
        assert changed is True
        assert req_item.sourcing_status == "offered"

    def test_sourcing_to_offered(self, db_session, test_requisition, test_user):
        req_item = test_requisition.requirements[0]
        req_item.sourcing_status = "sourcing"
        db_session.commit()

        changed = on_offer_created(req_item, db_session, actor=test_user)
        assert changed is True
        assert req_item.sourcing_status == "offered"

    def test_does_not_demote_from_quoted(self, db_session, test_requisition, test_user):
        req_item = test_requisition.requirements[0]
        req_item.sourcing_status = "quoted"
        db_session.commit()

        changed = on_offer_created(req_item, db_session, actor=test_user)
        assert changed is False
        assert req_item.sourcing_status == "quoted"


class TestOnQuoteBuilt:
    def test_marks_offered_as_quoted(self, db_session, test_requisition, test_user):
        req_item = test_requisition.requirements[0]
        req_item.sourcing_status = "offered"
        db_session.commit()

        changed = on_quote_built([req_item.id], db_session, actor=test_user)
        assert changed == 1
        assert req_item.sourcing_status == "quoted"

    def test_skips_already_won(self, db_session, test_requisition, test_user):
        req_item = test_requisition.requirements[0]
        req_item.sourcing_status = "won"
        db_session.commit()

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
                activity_type="requisition_claimed",
            )
            .all()
        )
        assert len(logs) == 1


class TestUnclaimRequisition:
    def test_unclaim(self, db_session, test_requisition, test_user):
        test_requisition.claimed_by_id = test_user.id
        test_requisition.claimed_at = datetime.now(timezone.utc)
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
