"""Tests for get_last_activity_at note-exclusion (dormancy sweep).

Verifies that note-type activities (NOTE, SALES_NOTE, CONTACT_NOTE) are excluded
from the dormancy calculation so they don't falsely reset a company's dormancy clock.

Called by: pytest
Depends on: app.services.activity_service.get_last_activity_at,
            app.constants.ActivityType
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from app.constants import ActivityType
from app.models import ActivityLog, Company
from app.services.activity_service import get_last_activity_at


@pytest.fixture()
def company(db_session: Session) -> Company:
    co = Company(
        name="Dormancy Test Co",
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(co)
    db_session.commit()
    db_session.refresh(co)
    return co


def _add_activity(db: Session, company: Company, activity_type: str, days_ago: int = 0) -> ActivityLog:
    entry = ActivityLog(
        activity_type=activity_type,
        channel="email",  # NOT NULL in schema
        company_id=company.id,
        created_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


class TestGetLastActivityAtNoteExclusion:
    def test_only_note_returns_none(self, db_session: Session, company: Company):
        """Account with only a NOTE activity → treated as dormant (None returned)."""
        _add_activity(db_session, company, ActivityType.NOTE, days_ago=5)
        result = get_last_activity_at(company.id, db_session)
        assert result is None

    def test_only_sales_note_returns_none(self, db_session: Session, company: Company):
        """SALES_NOTE alone does not count as real activity."""
        _add_activity(db_session, company, ActivityType.SALES_NOTE, days_ago=3)
        result = get_last_activity_at(company.id, db_session)
        assert result is None

    def test_only_contact_note_returns_none(self, db_session: Session, company: Company):
        """CONTACT_NOTE alone does not count as real activity."""
        _add_activity(db_session, company, ActivityType.CONTACT_NOTE, days_ago=1)
        result = get_last_activity_at(company.id, db_session)
        assert result is None

    def test_real_activity_returned(self, db_session: Session, company: Company):
        """A call (email_sent) IS included and its timestamp is returned."""
        entry = _add_activity(db_session, company, ActivityType.EMAIL_SENT, days_ago=10)
        result = get_last_activity_at(company.id, db_session)
        assert result is not None
        # Allow 1-second slop for test timing
        assert abs((result - entry.created_at.replace(tzinfo=timezone.utc)).total_seconds()) < 2

    def test_note_does_not_shadow_earlier_real_activity(self, db_session: Session, company: Company):
        """A recent note should not mask an older real-activity timestamp."""
        real = _add_activity(db_session, company, ActivityType.CALL_LOGGED, days_ago=20)
        _add_activity(db_session, company, ActivityType.NOTE, days_ago=2)  # newer but excluded
        result = get_last_activity_at(company.id, db_session)
        assert result is not None
        assert abs((result - real.created_at.replace(tzinfo=timezone.utc)).total_seconds()) < 2

    def test_all_three_note_types_excluded(self, db_session: Session, company: Company):
        """All three note types are excluded simultaneously."""
        _add_activity(db_session, company, ActivityType.NOTE, days_ago=1)
        _add_activity(db_session, company, ActivityType.SALES_NOTE, days_ago=2)
        _add_activity(db_session, company, ActivityType.CONTACT_NOTE, days_ago=3)
        result = get_last_activity_at(company.id, db_session)
        assert result is None

    def test_no_activity_returns_none(self, db_session: Session, company: Company):
        """Company with no activities → None."""
        result = get_last_activity_at(company.id, db_session)
        assert result is None
