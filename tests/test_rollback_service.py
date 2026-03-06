"""Tests for verify-retest service (post-fix Playwright area retest).

Covers: pass resolves ticket, fail creates regression, missing ticket,
        notification emission.

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
def queued_ticket(db_session: Session, rb_user: User) -> TroubleTicket:
    ticket = TroubleTicket(
        ticket_number="TT-RB-001",
        submitted_by=rb_user.id,
        title="Tickets view broken",
        description="The tickets view throws a JS error",
        status="fix_queued",
        tested_area="tickets",
        risk_tier="low",
    )
    db_session.add(ticket)
    db_session.commit()
    db_session.refresh(ticket)
    return ticket


def _make_mock_tester(issues=None):
    """Build a mock SiteTester class whose instances have run_full_sweep + issues."""
    mock_cls = MagicMock()
    instance = MagicMock()
    instance.run_full_sweep = AsyncMock(return_value=[])
    instance.issues = issues or []
    mock_cls.return_value = instance
    return mock_cls


class TestVerifyAndRetest:
    @patch("app.services.rollback_service.SiteTester")
    def test_pass_resolves_ticket(self, mock_st_cls, db_session, queued_ticket):
        mock_st_cls.return_value = MagicMock(
            run_full_sweep=AsyncMock(return_value=[]),
            issues=[],
        )
        result = _run(verify_and_retest(
            queued_ticket.id, db_session,
            base_url="http://localhost:8000",
            session_cookie="abc",
        ))
        assert result["passed"] is True
        assert result["issues"] == []
        db_session.refresh(queued_ticket)
        assert queued_ticket.status == "resolved"

    @patch("app.services.rollback_service.SiteTester")
    def test_fail_creates_regression_ticket(self, mock_st_cls, db_session, queued_ticket):
        mock_st_cls.return_value = MagicMock(
            run_full_sweep=AsyncMock(return_value=[]),
            issues=[
                {"area": "tickets", "title": "JS error", "description": "Uncaught TypeError"},
            ],
        )
        result = _run(verify_and_retest(
            queued_ticket.id, db_session,
            base_url="http://localhost:8000",
            session_cookie="abc",
        ))
        assert result["passed"] is False
        assert "regression_ticket_id" in result

        # Check child ticket
        child = db_session.get(TroubleTicket, result["regression_ticket_id"])
        assert child is not None
        assert child.parent_ticket_id == queued_ticket.id
        assert child.tested_area == "tickets"
        assert "Retest failed" in child.title

        # Original should be escalated
        db_session.refresh(queued_ticket)
        assert queued_ticket.status == "escalated"

    def test_missing_ticket(self, db_session):
        result = _run(verify_and_retest(
            99999, db_session,
            base_url="http://localhost:8000",
            session_cookie="abc",
        ))
        assert result["passed"] is False
        assert "not found" in result["error"].lower()

    @patch("app.services.rollback_service.SiteTester")
    def test_pass_emits_notification(self, mock_st_cls, db_session, queued_ticket, rb_user):
        mock_st_cls.return_value = MagicMock(
            run_full_sweep=AsyncMock(return_value=[]),
            issues=[],
        )
        _run(verify_and_retest(
            queued_ticket.id, db_session,
            base_url="http://localhost:8000",
            session_cookie="abc",
        ))
        notifs = (
            db_session.query(Notification)
            .filter_by(
                ticket_id=queued_ticket.id,
                event_type="resolved",
            )
            .all()
        )
        assert len(notifs) == 1
