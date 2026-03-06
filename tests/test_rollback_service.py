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
    @patch("app.services.site_tester.SiteTester")
    def test_pass_resolves_ticket(self, mock_st_cls, db_session, queued_ticket):
        instance = AsyncMock()
        instance.run_full_sweep = AsyncMock(return_value=[])
        mock_st_cls.return_value = instance
        result = _run(verify_and_retest(
            queued_ticket.id, db_session,
            base_url="http://localhost:8000",
            session_cookie="abc",
        ))
        assert result["ok"] is True
        db_session.refresh(queued_ticket)
        assert queued_ticket.status == "resolved"

    @patch("app.services.site_tester.SiteTester")
    def test_fail_creates_regression_ticket(self, mock_st_cls, db_session, queued_ticket):
        instance = AsyncMock()
        instance.run_full_sweep = AsyncMock(return_value=[
            {"area": "tickets", "title": "JS error", "description": "Uncaught TypeError"},
        ])
        mock_st_cls.return_value = instance
        result = _run(verify_and_retest(
            queued_ticket.id, db_session,
            base_url="http://localhost:8000",
            session_cookie="abc",
        ))
        assert result["ok"] is False
        assert result["status"] == "escalated"
        assert len(result["issues"]) > 0

        # Original should be escalated
        db_session.refresh(queued_ticket)
        assert queued_ticket.status == "escalated"

        # Check child ticket was created
        children = (
            db_session.query(TroubleTicket)
            .filter(TroubleTicket.parent_ticket_id == queued_ticket.id)
            .all()
        )
        assert len(children) >= 1
        assert children[0].tested_area == "tickets"

    def test_missing_ticket(self, db_session):
        result = _run(verify_and_retest(
            99999, db_session,
            base_url="http://localhost:8000",
            session_cookie="abc",
        ))
        assert "error" in result
        assert "not found" in result["error"].lower()

    @patch("app.services.site_tester.SiteTester")
    def test_pass_emits_notification(self, mock_st_cls, db_session, queued_ticket, rb_user):
        instance = AsyncMock()
        instance.run_full_sweep = AsyncMock(return_value=[])
        mock_st_cls.return_value = instance
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
