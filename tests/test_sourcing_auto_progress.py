"""Tests for sourcing auto-progress service.

Called by: pytest
Depends on: app.services.sourcing_auto_progress, conftest fixtures
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from app.constants import ActivityType, SourcingStatus
from app.models import ActivityLog, Requirement, Requisition, User
from app.services.sourcing_auto_progress import auto_progress_status


@pytest.fixture()
def user(db_session: Session) -> User:
    """Create a test user."""
    u = User(
        email="auto-progress@test.com",
        name="Auto Progress Tester",
        role="buyer",
        azure_id="auto-progress-azure",
        m365_connected=True,
        created_at=datetime.now(UTC),
    )
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


@pytest.fixture()
def requisition(db_session: Session, user: User) -> Requisition:
    """Create a test requisition."""
    req = Requisition(
        name="TEST-REQ-001",
        customer_name="Test Customer",
        status="open",
        created_by=user.id,
        created_at=datetime.now(UTC),
    )
    db_session.add(req)
    db_session.commit()
    db_session.refresh(req)
    return req


@pytest.fixture()
def requirement(db_session: Session, requisition: Requisition) -> Requirement:
    """Create a test requirement with OPEN status."""
    r = Requirement(
        requisition_id=requisition.id,
        primary_mpn="TEST-MPN-001",
        target_qty=100,
        sourcing_status=SourcingStatus.OPEN,
        created_at=datetime.now(UTC),
    )
    db_session.add(r)
    db_session.commit()
    db_session.refresh(r)
    return r


class TestAutoProgressStatus:
    """Tests for auto_progress_status function."""

    def test_forward_progression_open_to_sourcing(self, db_session: Session, requirement: Requirement, user: User):
        """OPEN -> SOURCING should succeed and return True."""
        result = auto_progress_status(requirement, SourcingStatus.SOURCING, db_session, user.id)

        assert result is True
        assert requirement.sourcing_status == SourcingStatus.SOURCING

    def test_forward_progression_sourcing_to_offered(self, db_session: Session, requirement: Requirement, user: User):
        """SOURCING -> OFFERED should succeed."""
        requirement.sourcing_status = SourcingStatus.SOURCING
        db_session.commit()

        result = auto_progress_status(requirement, SourcingStatus.OFFERED, db_session, user.id)

        assert result is True
        assert requirement.sourcing_status == SourcingStatus.OFFERED

    def test_already_at_target_returns_false(self, db_session: Session, requirement: Requirement, user: User):
        """Same status should return False and not create activity."""
        result = auto_progress_status(requirement, SourcingStatus.OPEN, db_session, user.id)

        assert result is False
        assert requirement.sourcing_status == SourcingStatus.OPEN

        # No ActivityLog created
        count = db_session.query(ActivityLog).filter(ActivityLog.requirement_id == requirement.id).count()
        assert count == 0

    @pytest.mark.parametrize(
        ("current_status", "target_status"),
        [
            (SourcingStatus.SOURCING, SourcingStatus.OPEN),
            (SourcingStatus.OFFERED, SourcingStatus.SOURCING),
            (SourcingStatus.LOST, SourcingStatus.SOURCING),
            (SourcingStatus.ARCHIVED, SourcingStatus.SOURCING),
        ],
        ids=[
            "ahead_sourcing_not_downgraded_to_open",
            "ahead_offered_not_downgraded_to_sourcing",
            "non_progression_lost",
            "non_progression_archived",
        ],
    )
    def test_no_progression_returns_false(
        self,
        db_session: Session,
        requirement: Requirement,
        user: User,
        current_status: SourcingStatus,
        target_status: SourcingStatus,
    ):
        """Statuses ahead of the target or outside the progression order are not
        changed."""
        requirement.sourcing_status = current_status
        db_session.commit()

        result = auto_progress_status(requirement, target_status, db_session, user.id)

        assert result is False
        assert requirement.sourcing_status == current_status

    def test_activity_log_created_on_change(self, db_session: Session, requirement: Requirement, user: User):
        """ActivityLog should be created when status changes."""
        auto_progress_status(requirement, SourcingStatus.SOURCING, db_session, user.id)
        db_session.flush()

        logs = (
            db_session.query(ActivityLog)
            .filter(
                ActivityLog.requirement_id == requirement.id,
                ActivityLog.activity_type == ActivityType.STATUS_CHANGED,
            )
            .all()
        )

        assert len(logs) == 1
        log = logs[0]
        assert log.user_id == user.id
        assert log.requisition_id == requirement.requisition_id
        assert log.channel == "system"
        assert "Auto-progressed" in log.notes
        assert "open" in log.notes
        assert "sourcing" in log.notes

    def test_no_activity_log_when_no_change(self, db_session: Session, requirement: Requirement, user: User):
        """No ActivityLog when status is already at or ahead of target."""
        requirement.sourcing_status = SourcingStatus.OFFERED
        db_session.commit()

        auto_progress_status(requirement, SourcingStatus.SOURCING, db_session, user.id)
        db_session.flush()

        count = db_session.query(ActivityLog).filter(ActivityLog.requirement_id == requirement.id).count()
        assert count == 0

    def test_user_id_none_allowed(self, db_session: Session, requirement: Requirement):
        """user_id=None should still work (system-triggered progression)."""
        result = auto_progress_status(requirement, SourcingStatus.SOURCING, db_session, user_id=None)

        assert result is True
        db_session.flush()

        log = db_session.query(ActivityLog).filter(ActivityLog.requirement_id == requirement.id).first()
        assert log is not None
        assert log.user_id is None
