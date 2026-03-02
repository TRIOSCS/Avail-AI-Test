"""Tests for rollback service (post-fix health monitoring).

Covers: health check stub, alert emission, regression handling.

Called by: pytest
Depends on: app.services.rollback_service
"""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models import User
from app.models.notification import Notification
from app.models.trouble_ticket import TroubleTicket
from app.services.rollback_service import check_post_fix_health


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture()
def rb_user(db_session: Session) -> User:
    user = User(
        email="rollback@trioscs.com", name="Rollback User", role="admin",
        azure_id="test-rb-001", created_at=datetime.now(timezone.utc),
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture()
def fixed_ticket(db_session: Session, rb_user: User) -> TroubleTicket:
    ticket = TroubleTicket(
        ticket_number="TT-RB-001", submitted_by=rb_user.id,
        title="Fixed bug", description="Was broken, now fixed",
        status="awaiting_verification", risk_tier="low",
        diagnosis={"root_cause": "Query error"},
        file_mapping=["app/routers/vendors.py"],
    )
    db_session.add(ticket)
    db_session.commit()
    db_session.refresh(ticket)
    return ticket


class TestCheckPostFixHealth:
    def test_healthy_by_default(self, db_session, fixed_ticket):
        result = _run(check_post_fix_health(fixed_ticket.id, db_session))
        assert result["healthy"] is True
        assert result["issues"] == []

    def test_missing_ticket_returns_healthy(self, db_session):
        result = _run(check_post_fix_health(99999, db_session))
        assert result["healthy"] is True

    @patch("app.services.rollback_service._check_health", new_callable=AsyncMock)
    def test_issues_detected_alerts(self, mock_health, db_session, fixed_ticket):
        mock_health.return_value = ["New 500 error on /api/vendors", "TypeError in search"]
        result = _run(check_post_fix_health(fixed_ticket.id, db_session))
        assert result["healthy"] is False
        assert len(result["issues"]) == 2

    @patch("app.services.rollback_service._check_health", new_callable=AsyncMock)
    def test_issues_create_notification(self, mock_health, db_session, fixed_ticket, rb_user):
        mock_health.return_value = ["Database timeout"]
        _run(check_post_fix_health(fixed_ticket.id, db_session))
        notifs = db_session.query(Notification).filter_by(
            ticket_id=fixed_ticket.id, event_type="failed",
        ).all()
        assert len(notifs) == 1
        assert "regression" in notifs[0].title.lower()
        assert "Database timeout" in notifs[0].body

    @patch("app.services.rollback_service._check_health", new_callable=AsyncMock)
    def test_issues_update_resolution_notes(self, mock_health, db_session, fixed_ticket):
        mock_health.return_value = ["Memory leak"]
        _run(check_post_fix_health(fixed_ticket.id, db_session))
        db_session.refresh(fixed_ticket)
        assert "Memory leak" in (fixed_ticket.resolution_notes or "")

    @patch("app.services.rollback_service._check_health", new_callable=AsyncMock)
    def test_does_not_auto_revert(self, mock_health, db_session, fixed_ticket):
        """Rollback is alert-only — status should NOT change."""
        mock_health.return_value = ["Error detected"]
        _run(check_post_fix_health(fixed_ticket.id, db_session))
        db_session.refresh(fixed_ticket)
        # Status should remain awaiting_verification (not reverted)
        assert fixed_ticket.status == "awaiting_verification"
