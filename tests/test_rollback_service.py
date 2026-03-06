"""Tests for rollback service (post-fix verification via SiteTester).

Covers: verify_and_retest pass/fail paths, ticket status updates, notifications.

Called by: pytest
Depends on: app.services.rollback_service
"""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models import User
from app.models.notification import Notification
from app.models.trouble_ticket import TroubleTicket
from app.services.rollback_service import verify_and_retest


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture()
def rb_user(db_session: Session) -> User:
    user = User(
        email="rollback@trioscs.com",
        name="Rollback User",
        role="admin",
        azure_id="test-rb-001",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture()
def fixed_ticket(db_session: Session, rb_user: User) -> TroubleTicket:
    ticket = TroubleTicket(
        ticket_number="TT-RB-001",
        submitted_by=rb_user.id,
        title="Fixed bug",
        description="Was broken, now fixed",
        status="awaiting_verification",
        risk_tier="low",
        diagnosis={"root_cause": "Query error"},
        file_mapping=["app/routers/vendors.py"],
    )
    db_session.add(ticket)
    db_session.commit()
    db_session.refresh(ticket)
    return ticket


class TestVerifyAndRetest:
    @patch("app.services.site_tester.SiteTester")
    def test_pass_resolves_ticket(self, MockTester, db_session, fixed_ticket):
        """No issues in sweep -> ticket resolved."""
        instance = MagicMock()
        instance.run_full_sweep = AsyncMock(return_value=[])
        MockTester.return_value = instance

        result = _run(verify_and_retest(fixed_ticket.id, db_session))
        assert result["ok"] is True
        assert result["status"] == "resolved"
        db_session.refresh(fixed_ticket)
        assert fixed_ticket.status == "resolved"

    @patch("app.services.site_tester.SiteTester")
    def test_pass_creates_resolved_notification(self, MockTester, db_session, fixed_ticket, rb_user):
        """Resolved ticket sends notification to submitter."""
        instance = MagicMock()
        instance.run_full_sweep = AsyncMock(return_value=[])
        MockTester.return_value = instance

        _run(verify_and_retest(fixed_ticket.id, db_session))
        notifs = (
            db_session.query(Notification)
            .filter_by(ticket_id=fixed_ticket.id, event_type="resolved")
            .all()
        )
        assert len(notifs) == 1

    def test_missing_ticket_returns_error(self, db_session):
        """Non-existent ticket -> error dict."""
        result = _run(verify_and_retest(99999, db_session))
        assert "error" in result
        assert "not found" in result["error"].lower()

    @patch("app.services.rollback_service.create_ticket")
    @patch("app.services.site_tester.SiteTester")
    def test_fail_escalates_ticket(self, MockTester, mock_create, db_session, fixed_ticket):
        """Issues found in the ticket's area -> ticket escalated."""
        area = fixed_ticket.tested_area or fixed_ticket.category or "general"
        instance = MagicMock()
        instance.run_full_sweep = AsyncMock(return_value=[
            {"area": area, "title": "500 error on vendors", "description": "Server error"},
        ])
        MockTester.return_value = instance
        mock_create.return_value = MagicMock(id=9999)

        result = _run(verify_and_retest(fixed_ticket.id, db_session))
        assert result["ok"] is False
        assert result["status"] == "escalated"
        assert len(result["issues"]) == 1
        db_session.refresh(fixed_ticket)
        assert fixed_ticket.status == "escalated"

    @patch("app.services.rollback_service.create_ticket")
    @patch("app.services.site_tester.SiteTester")
    def test_fail_creates_escalated_notification(self, MockTester, mock_create, db_session, fixed_ticket, rb_user):
        """Failed retest sends escalation notification."""
        area = fixed_ticket.tested_area or fixed_ticket.category or "general"
        instance = MagicMock()
        instance.run_full_sweep = AsyncMock(return_value=[
            {"area": area, "title": "Regression", "description": "Broke again"},
        ])
        MockTester.return_value = instance
        mock_create.return_value = MagicMock(id=9999)

        _run(verify_and_retest(fixed_ticket.id, db_session))
        notifs = (
            db_session.query(Notification)
            .filter_by(ticket_id=fixed_ticket.id, event_type="escalated")
            .all()
        )
        assert len(notifs) == 1

    @patch("app.services.site_tester.SiteTester")
    def test_sweep_exception_returns_error(self, MockTester, db_session, fixed_ticket):
        """SiteTester crash -> error dict, ticket unchanged."""
        instance = MagicMock()
        instance.run_full_sweep = AsyncMock(side_effect=RuntimeError("Playwright crashed"))
        MockTester.return_value = instance

        result = _run(verify_and_retest(fixed_ticket.id, db_session))
        assert "error" in result
        assert "failed" in result["error"].lower()
        db_session.refresh(fixed_ticket)
        assert fixed_ticket.status == "awaiting_verification"

    @patch("app.services.site_tester.SiteTester")
    def test_unrelated_issues_still_pass(self, MockTester, db_session, fixed_ticket):
        """Issues in OTHER areas don't block resolution of this ticket."""
        instance = MagicMock()
        instance.run_full_sweep = AsyncMock(return_value=[
            {"area": "unrelated_area", "title": "Other problem", "description": "Not ours"},
        ])
        MockTester.return_value = instance

        result = _run(verify_and_retest(fixed_ticket.id, db_session))
        assert result["ok"] is True
        assert result["status"] == "resolved"
