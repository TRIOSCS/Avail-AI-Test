"""test_requirement_status_coverage.py — Extra coverage for requirement_status.py.

Targets uncovered branches at lines 73-74, 91-92, 105-107, 124-125, 138, 158-159, 183-184.
These cover:
- transition_requirement: actor=None (no user_id), ActivityLog exception handling
- on_rfq_sent: ValueError during transition_requirement is caught
- on_offer_created: ValueError during transition is caught
- on_quote_built: ValueError during transition is caught
- claim_requisition: not-found path
- unclaim_requisition: actor=None path, ActivityLog exception handling

Called by: pytest
Depends on: app/services/requirement_status.py
"""

import os

os.environ["TESTING"] = "1"

from unittest.mock import patch

import pytest

from app.models import ActivityLog, Requisition
from app.services.requirement_status import (
    claim_requisition,
    on_offer_created,
    on_quote_built,
    on_rfq_sent,
    transition_requirement,
    unclaim_requisition,
)


class TestTransitionRequirementNoActor:
    """Lines 73-74 — transition with no actor logs with user_id=None."""

    def test_transition_without_actor_creates_log_with_null_user(self, db_session, test_requisition):
        req_item = test_requisition.requirements[0]
        req_item.sourcing_status = "open"
        db_session.commit()

        changed = transition_requirement(req_item, "sourcing", db_session, actor=None)
        assert changed is True

        db_session.flush()
        log = (
            db_session.query(ActivityLog)
            .filter_by(
                requisition_id=test_requisition.id,
                activity_type="part_status_change",
            )
            .first()
        )
        assert log is not None
        assert log.user_id is None

    def test_transition_activity_log_exception_does_not_crash(self, db_session, test_requisition):
        """ActivityLog creation failure is swallowed (lines 73-74)."""
        req_item = test_requisition.requirements[0]
        req_item.sourcing_status = "open"
        db_session.commit()

        with patch("app.services.requirement_status.ActivityLog", side_effect=Exception("db error")):
            # Should not raise — exception is swallowed
            changed = transition_requirement(req_item, "sourcing", db_session)
        assert changed is True
        assert req_item.sourcing_status == "sourcing"


class TestOnRfqSentValueError:
    """Line 91-92 — ValueError during transition is caught."""

    def test_value_error_during_rfq_transition_is_caught(self, db_session, test_requisition):
        """If transition_requirement raises ValueError, it's caught and skipped."""
        req_item = test_requisition.requirements[0]
        req_item.sourcing_status = "open"
        db_session.commit()

        with patch(
            "app.services.requirement_status.transition_requirement",
            side_effect=ValueError("bad transition"),
        ):
            changed = on_rfq_sent([req_item.id], db_session)
        assert changed == 0


class TestOnOfferCreatedEdgeCases:
    """Lines 105-107 — ValueError during offer transition is caught."""

    def test_value_error_during_offer_transition_is_caught(self, db_session, test_requisition):
        req_item = test_requisition.requirements[0]
        req_item.sourcing_status = "open"
        db_session.commit()

        with patch(
            "app.services.requirement_status.transition_requirement",
            side_effect=ValueError("bad transition"),
        ):
            result = on_offer_created(req_item, db_session)
        assert result is False

    def test_on_offer_created_from_won_does_not_change(self, db_session, test_requisition):
        """Won status → on_offer_created returns False without transition."""
        req_item = test_requisition.requirements[0]
        req_item.sourcing_status = "won"
        db_session.commit()

        result = on_offer_created(req_item, db_session)
        assert result is False
        assert req_item.sourcing_status == "won"

    def test_on_offer_created_from_quoted_does_not_change(self, db_session, test_requisition):
        """Quoted status → on_offer_created returns False without transition."""
        req_item = test_requisition.requirements[0]
        req_item.sourcing_status = "quoted"
        db_session.commit()

        result = on_offer_created(req_item, db_session)
        assert result is False


class TestOnQuoteBuiltValueError:
    """Lines 124-125 — ValueError during quote transition is caught."""

    def test_value_error_during_quote_transition_is_caught(self, db_session, test_requisition):
        req_item = test_requisition.requirements[0]
        req_item.sourcing_status = "open"
        db_session.commit()

        with patch(
            "app.services.requirement_status.transition_requirement",
            side_effect=ValueError("bad transition"),
        ):
            changed = on_quote_built([req_item.id], db_session)
        assert changed == 0

    def test_on_quote_built_from_open_status(self, db_session, test_requisition, test_user):
        """Open status → on_quote_built transitions to quoted."""
        req_item = test_requisition.requirements[0]
        req_item.sourcing_status = "open"
        db_session.commit()

        changed = on_quote_built([req_item.id], db_session, actor=test_user)
        assert changed == 1
        assert req_item.sourcing_status == "quoted"


class TestClaimRequisitionNotFound:
    """Line 138 — claim when requisition not found raises ValueError."""

    def test_claim_requisition_not_found_raises(self, db_session, test_user):
        """Locks and fails if requisition not found in DB."""
        # Create a fake requisition object not saved to DB
        fake_req = Requisition(
            id=999999,
            name="ghost-req",
            status="active",
            created_by=test_user.id,
        )
        # The with_for_update() query will return None for id=999999
        with pytest.raises(ValueError, match="not found"):
            claim_requisition(fake_req, test_user, db_session)


class TestUnclaimRequisitionNoActor:
    """Lines 183-184 — unclaim with no actor uses actor_id=None."""

    def test_unclaim_without_actor(self, db_session, test_requisition, test_user):
        test_requisition.claimed_by_id = test_user.id
        db_session.commit()

        changed = unclaim_requisition(test_requisition, db_session, actor=None)
        assert changed is True
        assert test_requisition.claimed_by_id is None

        db_session.flush()
        log = (
            db_session.query(ActivityLog)
            .filter_by(
                requisition_id=test_requisition.id,
                activity_type="requisition_unclaimed",
            )
            .first()
        )
        assert log is not None
        assert log.user_id is None

    def test_unclaim_activity_log_exception_is_swallowed(self, db_session, test_requisition, test_user):
        """ActivityLog creation failure during unclaim is swallowed (lines 183-184)."""
        test_requisition.claimed_by_id = test_user.id
        db_session.commit()

        with patch("app.services.requirement_status.ActivityLog", side_effect=Exception("log err")):
            changed = unclaim_requisition(test_requisition, db_session, actor=test_user)
        assert changed is True
        assert test_requisition.claimed_by_id is None
