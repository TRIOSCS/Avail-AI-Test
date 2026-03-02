"""Tests for pattern tracker service.

Covers: weekly stats, recurring patterns, health status indicator.

Called by: pytest
Depends on: app.services.pattern_tracker
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from app.models import User
from app.models.self_heal_log import SelfHealLog
from app.models.trouble_ticket import TroubleTicket
from app.services.pattern_tracker import (
    detect_recurring_patterns,
    get_health_status,
    get_weekly_stats,
)


@pytest.fixture()
def pt_user(db_session: Session) -> User:
    user = User(
        email="pattern@trioscs.com", name="Pattern User", role="admin",
        azure_id="test-pt-001", created_at=datetime.now(timezone.utc),
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _ticket(db, user_id, **kwargs):
    defaults = {
        "ticket_number": f"TT-PT-{db.query(TroubleTicket).count() + 1:03d}",
        "submitted_by": user_id,
        "title": "Test ticket",
        "description": "Test",
    }
    defaults.update(kwargs)
    t = TroubleTicket(**defaults)
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


class TestWeeklyStats:
    def test_empty_database(self, db_session):
        stats = get_weekly_stats(db_session)
        assert stats["tickets_created"] == 0
        assert stats["tickets_resolved"] == 0
        assert stats["by_category"] == {}
        assert stats["success_rate"] == 0.0
        assert stats["total_cost"] == 0.0

    def test_counts_created_and_resolved(self, db_session, pt_user):
        now = datetime.now(timezone.utc)
        _ticket(db_session, pt_user.id, category="api")
        _ticket(db_session, pt_user.id, category="ui",
                status="resolved", resolved_at=now)
        stats = get_weekly_stats(db_session)
        assert stats["tickets_created"] == 2
        assert stats["tickets_resolved"] == 1

    def test_by_category(self, db_session, pt_user):
        _ticket(db_session, pt_user.id, category="api")
        _ticket(db_session, pt_user.id, category="api")
        _ticket(db_session, pt_user.id, category="ui")
        stats = get_weekly_stats(db_session)
        assert stats["by_category"]["api"] == 2
        assert stats["by_category"]["ui"] == 1

    def test_by_risk(self, db_session, pt_user):
        _ticket(db_session, pt_user.id, risk_tier="low")
        _ticket(db_session, pt_user.id, risk_tier="high")
        stats = get_weekly_stats(db_session)
        assert stats["by_risk"]["low"] == 1
        assert stats["by_risk"]["high"] == 1

    def test_success_rate(self, db_session, pt_user):
        t = _ticket(db_session, pt_user.id)
        db_session.add(SelfHealLog(ticket_id=t.id, fix_succeeded=True))
        db_session.add(SelfHealLog(ticket_id=t.id, fix_succeeded=False))
        db_session.commit()
        stats = get_weekly_stats(db_session)
        assert stats["success_rate"] == 50.0

    def test_avg_resolution_time(self, db_session, pt_user):
        now = datetime.now(timezone.utc)
        _ticket(db_session, pt_user.id,
                created_at=now - timedelta(hours=6),
                status="resolved", resolved_at=now)
        _ticket(db_session, pt_user.id,
                created_at=now - timedelta(hours=2),
                status="resolved", resolved_at=now)
        stats = get_weekly_stats(db_session)
        assert stats["avg_resolution_hours"] == 4.0  # (6+2)/2

    def test_total_cost(self, db_session, pt_user):
        t = _ticket(db_session, pt_user.id)
        db_session.add(SelfHealLog(ticket_id=t.id, cost_usd=1.50))
        db_session.add(SelfHealLog(ticket_id=t.id, cost_usd=0.75))
        db_session.commit()
        stats = get_weekly_stats(db_session)
        assert stats["total_cost"] == 2.25

    def test_excludes_old_data(self, db_session, pt_user):
        old = datetime.now(timezone.utc) - timedelta(days=14)
        _ticket(db_session, pt_user.id, category="api", created_at=old)
        _ticket(db_session, pt_user.id, category="ui")  # this week
        stats = get_weekly_stats(db_session, weeks_back=1)
        assert stats["tickets_created"] == 1


class TestRecurringPatterns:
    def test_no_patterns_when_few(self, db_session, pt_user):
        _ticket(db_session, pt_user.id, category="api", current_page="/vendors")
        _ticket(db_session, pt_user.id, category="api", current_page="/vendors")
        patterns = detect_recurring_patterns(db_session, min_occurrences=3)
        assert patterns == []

    def test_detects_pattern(self, db_session, pt_user):
        for _ in range(4):
            _ticket(db_session, pt_user.id, category="api", current_page="/vendors")
        patterns = detect_recurring_patterns(db_session, min_occurrences=3)
        assert len(patterns) == 1
        assert patterns[0]["category"] == "api"
        assert patterns[0]["page"] == "/vendors"
        assert patterns[0]["count"] == 4

    def test_multiple_patterns(self, db_session, pt_user):
        for _ in range(3):
            _ticket(db_session, pt_user.id, category="api", current_page="/vendors")
        for _ in range(3):
            _ticket(db_session, pt_user.id, category="ui", current_page="/search")
        patterns = detect_recurring_patterns(db_session, min_occurrences=3)
        assert len(patterns) == 2

    def test_excludes_old_tickets(self, db_session, pt_user):
        old = datetime.now(timezone.utc) - timedelta(days=45)
        for _ in range(5):
            _ticket(db_session, pt_user.id, category="api",
                    current_page="/old", created_at=old)
        patterns = detect_recurring_patterns(db_session, min_occurrences=3)
        assert patterns == []


class TestHealthStatus:
    def test_green_when_empty(self, db_session):
        health = get_health_status(db_session)
        assert health["status"] == "green"
        assert health["open_count"] == 0

    def test_green_few_tickets(self, db_session, pt_user):
        _ticket(db_session, pt_user.id, status="submitted")
        _ticket(db_session, pt_user.id, status="submitted")
        health = get_health_status(db_session)
        assert health["status"] == "green"

    def test_yellow_several_tickets(self, db_session, pt_user):
        for _ in range(4):
            _ticket(db_session, pt_user.id, status="submitted")
        health = get_health_status(db_session)
        assert health["status"] == "yellow"

    def test_yellow_high_risk(self, db_session, pt_user):
        _ticket(db_session, pt_user.id, status="submitted", risk_tier="high")
        health = get_health_status(db_session)
        assert health["status"] == "yellow"
        assert health["high_risk_count"] == 1

    def test_red_many_tickets(self, db_session, pt_user):
        for _ in range(12):
            _ticket(db_session, pt_user.id, status="submitted")
        health = get_health_status(db_session)
        assert health["status"] == "red"

    def test_red_many_high_risk(self, db_session, pt_user):
        for _ in range(4):
            _ticket(db_session, pt_user.id, status="diagnosed", risk_tier="high")
        health = get_health_status(db_session)
        assert health["status"] == "red"

    def test_ignores_resolved_tickets(self, db_session, pt_user):
        for _ in range(15):
            _ticket(db_session, pt_user.id, status="resolved")
        health = get_health_status(db_session)
        assert health["status"] == "green"
