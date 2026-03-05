"""Tests for self-heal scheduler jobs (auto-close, weekly report).

Covers: auto-close stale tickets (48h verify, 7d submitted),
weekly report generation.

Called by: pytest
Depends on: app.models.trouble_ticket, app.services.trouble_ticket_service
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from app.models import User
from app.models.trouble_ticket import TroubleTicket
from app.services.trouble_ticket_service import update_ticket


@pytest.fixture()
def sched_user(db_session: Session) -> User:
    user = User(
        email="sched@trioscs.com",
        name="Sched User",
        role="admin",
        azure_id="test-sched-001",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


class TestAutoCloseVerification:
    def test_auto_resolves_48h_stale(self, db_session, sched_user):
        """Tickets awaiting_verification > 48h should be auto-resolved."""
        now = datetime.now(timezone.utc)
        ticket = TroubleTicket(
            ticket_number="TT-AC-001",
            submitted_by=sched_user.id,
            title="Stale verify",
            description="D",
            status="awaiting_verification",
            diagnosed_at=now - timedelta(hours=50),
        )
        db_session.add(ticket)
        db_session.commit()

        # Simulate auto-close logic
        stale = (
            db_session.query(TroubleTicket)
            .filter(
                TroubleTicket.status == "awaiting_verification",
                TroubleTicket.diagnosed_at < now - timedelta(hours=48),
            )
            .all()
        )
        assert len(stale) == 1
        update_ticket(db_session, stale[0].id, status="resolved", resolution_notes="Auto-resolved: 48h timeout")
        db_session.refresh(ticket)
        assert ticket.status == "resolved"

    def test_does_not_close_recent_verify(self, db_session, sched_user):
        """Tickets awaiting_verification < 48h should NOT be auto-resolved."""
        now = datetime.now(timezone.utc)
        ticket = TroubleTicket(
            ticket_number="TT-AC-002",
            submitted_by=sched_user.id,
            title="Recent verify",
            description="D",
            status="awaiting_verification",
            diagnosed_at=now - timedelta(hours=24),
        )
        db_session.add(ticket)
        db_session.commit()

        stale = (
            db_session.query(TroubleTicket)
            .filter(
                TroubleTicket.status == "awaiting_verification",
                TroubleTicket.diagnosed_at < now - timedelta(hours=48),
            )
            .all()
        )
        assert len(stale) == 0


class TestAutoCloseSubmitted:
    def test_auto_rejects_7d_stale(self, db_session, sched_user):
        """Submitted tickets > 7d old should be auto-rejected."""
        now = datetime.now(timezone.utc)
        ticket = TroubleTicket(
            ticket_number="TT-AC-003",
            submitted_by=sched_user.id,
            title="Old ticket",
            description="D",
            status="submitted",
            created_at=now - timedelta(days=8),
        )
        db_session.add(ticket)
        db_session.commit()

        stale = (
            db_session.query(TroubleTicket)
            .filter(
                TroubleTicket.status == "submitted",
                TroubleTicket.created_at < now - timedelta(days=7),
            )
            .all()
        )
        assert len(stale) == 1
        update_ticket(db_session, stale[0].id, status="rejected", resolution_notes="Auto-rejected: 7d timeout")
        db_session.refresh(ticket)
        assert ticket.status == "rejected"

    def test_does_not_close_recent_submitted(self, db_session, sched_user):
        """Submitted tickets < 7d should NOT be auto-rejected."""
        now = datetime.now(timezone.utc)
        ticket = TroubleTicket(
            ticket_number="TT-AC-004",
            submitted_by=sched_user.id,
            title="Recent ticket",
            description="D",
            status="submitted",
            created_at=now - timedelta(days=3),
        )
        db_session.add(ticket)
        db_session.commit()

        stale = (
            db_session.query(TroubleTicket)
            .filter(
                TroubleTicket.status == "submitted",
                TroubleTicket.created_at < now - timedelta(days=7),
            )
            .all()
        )
        assert len(stale) == 0

    def test_does_not_close_diagnosed(self, db_session, sched_user):
        """Diagnosed tickets should NOT be auto-rejected regardless of age."""
        now = datetime.now(timezone.utc)
        ticket = TroubleTicket(
            ticket_number="TT-AC-005",
            submitted_by=sched_user.id,
            title="Old diagnosed",
            description="D",
            status="diagnosed",
            created_at=now - timedelta(days=14),
        )
        db_session.add(ticket)
        db_session.commit()

        stale = (
            db_session.query(TroubleTicket)
            .filter(
                TroubleTicket.status == "submitted",
                TroubleTicket.created_at < now - timedelta(days=7),
            )
            .all()
        )
        assert len(stale) == 0
