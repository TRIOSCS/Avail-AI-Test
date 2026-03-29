"""Tests for CRM Phase 2b — AI interaction quality scoring.

Called by: pytest
Depends on: app.models.intelligence, app.services.activity_quality_service
"""

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.auth import User
from app.models.intelligence import ActivityLog
from tests.conftest import engine  # noqa: F401


class TestActivityQualityColumns:
    """Test that ActivityLog has quality scoring columns."""

    def test_quality_score_column_exists(self, db_session: Session, test_user: User):
        """ActivityLog accepts quality_score."""
        log = ActivityLog(
            user_id=test_user.id,
            activity_type="phone_call",
            channel="phone",
            quality_score=75.0,
            quality_classification="conversation",
            is_meaningful=True,
        )
        db_session.add(log)
        db_session.flush()
        assert log.quality_score == 75.0
        assert log.quality_classification == "conversation"
        assert log.is_meaningful is True

    def test_quality_assessed_at_column(self, db_session: Session, test_user: User):
        """ActivityLog accepts quality_assessed_at timestamp."""
        now = datetime.now(timezone.utc)
        log = ActivityLog(
            user_id=test_user.id,
            activity_type="note",
            channel="manual",
            quality_assessed_at=now,
        )
        db_session.add(log)
        db_session.flush()
        assert log.quality_assessed_at == now


class TestQualityJobRegistration:
    """Test quality jobs are registered."""

    def test_register_quality_jobs_exists(self):
        """register_quality_jobs function exists."""
        from app.jobs.quality_jobs import register_quality_jobs

        assert callable(register_quality_jobs)
