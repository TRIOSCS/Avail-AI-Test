"""Tests for app/services/requisition_state.py — state machine transitions.

Covers:
- Allowed transitions succeed and update status
- Illegal transitions raise ValueError
- ActivityLog created on transition
- No-op when status unchanged
"""

import os

os.environ["TESTING"] = "1"

import pytest
from unittest.mock import MagicMock

from app.enums import RequisitionStatus
from app.services.requisition_state import ALLOWED_TRANSITIONS, transition
from app.models import ActivityLog


class TestTransition:
    def test_allowed_transition(self, db_session, test_requisition, test_user):
        test_requisition.status = "active"
        db_session.commit()

        transition(test_requisition, "sourcing", test_user, db_session)
        assert test_requisition.status == "sourcing"

    def test_allowed_transition_with_enum(self, db_session, test_requisition, test_user):
        test_requisition.status = "active"
        db_session.commit()

        transition(test_requisition, RequisitionStatus.offers, test_user, db_session)
        assert test_requisition.status == "offers"

    def test_illegal_transition_raises(self, db_session, test_requisition, test_user):
        test_requisition.status = "draft"
        db_session.commit()

        with pytest.raises(ValueError, match="Invalid transition"):
            transition(test_requisition, "won", test_user, db_session)

    def test_noop_when_same_status(self, db_session, test_requisition, test_user):
        test_requisition.status = "active"
        db_session.commit()

        transition(test_requisition, "active", test_user, db_session)
        assert test_requisition.status == "active"

    def test_creates_activity_log(self, db_session, test_requisition, test_user):
        test_requisition.status = "active"
        db_session.commit()

        transition(test_requisition, "sourcing", test_user, db_session)
        db_session.flush()

        logs = db_session.query(ActivityLog).filter_by(
            requisition_id=test_requisition.id,
            activity_type="status_change",
        ).all()
        assert len(logs) == 1
        assert "active → sourcing" in logs[0].subject

    def test_all_transitions_defined(self):
        """Every enum value has an entry in ALLOWED_TRANSITIONS."""
        for status in RequisitionStatus:
            assert status.value in ALLOWED_TRANSITIONS

    def test_archived_to_active(self, db_session, test_requisition, test_user):
        test_requisition.status = "archived"
        db_session.commit()

        transition(test_requisition, "active", test_user, db_session)
        assert test_requisition.status == "active"

    def test_won_cannot_go_to_active(self, db_session, test_requisition, test_user):
        test_requisition.status = "won"
        db_session.commit()

        with pytest.raises(ValueError):
            transition(test_requisition, "active", test_user, db_session)
