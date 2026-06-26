"""Tests for app/services/requisition_state.py — state machine transitions.

Covers:
- Allowed transitions succeed and update status
- Illegal transitions raise ValueError
- ActivityLog created on transition
- No-op when status unchanged
- set_hotlist helper
- Won/Lost transitions require an outcome reason
"""

import os

os.environ["TESTING"] = "1"


import pytest

from app.constants import ActivityType, RequisitionStatus
from app.models import ActivityLog
from app.services.requisition_state import (
    ALLOWED_TRANSITIONS,
    OutcomeReasonRequired,
    set_hotlist,
    transition,
)


class TestTransition:
    @pytest.mark.parametrize(
        ("from_status", "to_status"),
        [
            ("open", "rfqs_sent"),
            ("open", "offers"),
            ("won", "open"),
        ],
        ids=["open_to_rfqs_sent", "open_to_offers", "won_to_open"],
    )
    def test_allowed_transition(self, db_session, test_requisition, test_user, from_status, to_status):
        test_requisition.status = from_status
        db_session.commit()

        transition(test_requisition, to_status, test_user, db_session)
        assert test_requisition.status == to_status

    def test_allowed_transition_with_enum(self, db_session, test_requisition, test_user):
        test_requisition.status = "open"
        db_session.commit()

        transition(test_requisition, RequisitionStatus.OFFERS, test_user, db_session)
        assert test_requisition.status == "offers"

    def test_illegal_transition_raises(self, db_session, test_requisition, test_user):
        test_requisition.status = "draft"
        db_session.commit()

        with pytest.raises(ValueError, match="Invalid transition"):
            transition(test_requisition, "won", test_user, db_session)

    def test_noop_when_same_status(self, db_session, test_requisition, test_user):
        test_requisition.status = "open"
        db_session.commit()

        transition(test_requisition, "open", test_user, db_session)
        assert test_requisition.status == "open"

    def test_creates_activity_log(self, db_session, test_requisition, test_user):
        test_requisition.status = "open"
        db_session.commit()

        transition(test_requisition, "rfqs_sent", test_user, db_session)
        db_session.flush()

        logs = (
            db_session.query(ActivityLog)
            .filter_by(
                requisition_id=test_requisition.id,
                activity_type=ActivityType.STATUS_CHANGED,
            )
            .all()
        )
        assert len(logs) == 1
        assert "open → rfqs_sent" in logs[0].subject

    def test_all_transitions_defined(self):
        """Every enum value has an entry in ALLOWED_TRANSITIONS."""
        for status in RequisitionStatus:
            assert status.value in ALLOWED_TRANSITIONS

    def test_none_actor(self, db_session, test_requisition):
        """Transition with actor=None: ActivityLog creation may fail (NOT NULL FK), but
        the status transition still succeeds because the exception is caught."""
        test_requisition.status = "open"
        db_session.commit()

        transition(test_requisition, "rfqs_sent", None, db_session)
        # Transition still succeeds even if the log creation fails
        assert test_requisition.status == "rfqs_sent"

    def test_none_initial_status_defaults_to_open(self, db_session, test_requisition, test_user):
        """When req.status is None, it defaults to 'open' for transition logic."""
        test_requisition.status = None

        transition(test_requisition, "rfqs_sent", test_user, db_session)
        assert test_requisition.status == "rfqs_sent"

    def test_activity_log_exception_suppressed(self, db_session, test_requisition, test_user):
        """Exception during activity log creation is suppressed (logged)."""
        from unittest.mock import patch

        test_requisition.status = "open"
        db_session.commit()

        # Mock db.add to raise on the ActivityLog
        original_add = db_session.add
        call_count = 0

        def flaky_add(obj):
            nonlocal call_count
            call_count += 1
            if isinstance(obj, ActivityLog):
                raise RuntimeError("DB add failed")
            return original_add(obj)

        with patch.object(db_session, "add", side_effect=flaky_add):
            transition(test_requisition, "rfqs_sent", test_user, db_session)

        # Transition succeeded despite log failure
        assert test_requisition.status == "rfqs_sent"


class TestTransitionEdgeCases:
    """Boundary and illegal transition edge cases."""

    @pytest.mark.parametrize(
        ("from_status", "to_status"),
        [
            ("won", "rfqs_sent"),
            ("lost", "offers"),
        ],
        ids=["won_to_rfqs_sent_fails", "lost_to_offers_fails"],
    )
    def test_illegal_transition_fails(self, db_session, test_requisition, test_user, from_status, to_status):
        test_requisition.status = from_status
        db_session.commit()
        with pytest.raises(ValueError, match="Invalid transition"):
            transition(test_requisition, to_status, test_user, db_session)

    def test_won_to_open_roundtrip(self, db_session, test_requisition, test_user):
        test_requisition.status = "won"
        db_session.commit()
        transition(test_requisition, "open", test_user, db_session)
        assert test_requisition.status == "open"
        transition(test_requisition, "rfqs_sent", test_user, db_session)
        assert test_requisition.status == "rfqs_sent"

    def test_rapid_double_transition(self, db_session, test_requisition, test_user):
        test_requisition.status = "open"
        db_session.commit()
        transition(test_requisition, "rfqs_sent", test_user, db_session)
        transition(test_requisition, "open", test_user, db_session)
        assert test_requisition.status == "open"

    def test_every_illegal_transition_from_won(self, db_session, test_user):
        """Won can ONLY go to open.

        All others must fail.
        """
        from app.models import Requisition

        illegal = {"rfqs_sent", "offers", "quoted", "lost", "hotlist", "draft", "cancelled"}
        for target in illegal:
            req = Requisition(name=f"test-{target}", status="won", created_by=test_user.id)
            db_session.add(req)
            db_session.flush()
            with pytest.raises(ValueError):
                transition(req, target, test_user, db_session)


class TestPipelineAndHelpers:
    """New pipeline transitions, legacy normalisation, and the hotlist helper."""

    def test_transition_open_to_rfqs_sent(self, db_session, test_requisition, test_user):
        test_requisition.status = "open"
        db_session.commit()
        transition(test_requisition, "rfqs_sent", test_user, db_session)
        assert test_requisition.status == "rfqs_sent"

    def test_legacy_sourcing_origin_allows_open(self, db_session, test_requisition, test_user):
        # rows still on a legacy value can always move to open
        test_requisition.status = "sourcing"
        db_session.commit()
        transition(test_requisition, "open", test_user, db_session)
        assert test_requisition.status == "open"

    def test_set_hotlist_and_back(self, db_session, test_requisition, test_user):
        test_requisition.status = "open"
        db_session.commit()
        set_hotlist(test_requisition, test_user, db_session)
        assert test_requisition.status == "hotlist"
        transition(test_requisition, "open", test_user, db_session)
        assert test_requisition.status == "open"

    def test_won_requires_reason(self, db_session, test_requisition, test_user):
        test_requisition.status = "offers"
        db_session.commit()
        with pytest.raises(OutcomeReasonRequired, match="reason is required"):
            transition(test_requisition, "won", test_user, db_session)

    def test_lost_requires_reason(self, db_session, test_requisition, test_user):
        test_requisition.status = "offers"
        db_session.commit()
        with pytest.raises(OutcomeReasonRequired, match="reason is required"):
            transition(test_requisition, "lost", test_user, db_session)

    def test_won_persists_stripped_reason(self, db_session, test_requisition, test_user):
        test_requisition.status = "offers"
        db_session.commit()
        transition(test_requisition, "won", test_user, db_session, reason="  Best price  ")
        assert test_requisition.status == "won"
        assert test_requisition.outcome_reason == "Best price"

    def test_non_terminal_transition_clears_outcome_reason(self, db_session, test_requisition, test_user):
        test_requisition.status = "won"
        test_requisition.outcome_reason = "stale"
        db_session.commit()
        transition(test_requisition, "open", test_user, db_session)
        assert test_requisition.outcome_reason is None
