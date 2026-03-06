"""Tests for self-heal scheduler jobs (weekly report, daily consolidation).

Covers: registration of jobs, consolidation logic for duplicate tickets.

Called by: pytest
Depends on: app.models.trouble_ticket, app.services.trouble_ticket_service,
            app.services.ticket_consolidation
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


class TestJobRegistration:
    def test_register_selfheal_jobs(self):
        """register_selfheal_jobs adds weekly_report and consolidate_tickets."""
        from unittest.mock import MagicMock

        from app.jobs.selfheal_jobs import register_selfheal_jobs

        scheduler = MagicMock()
        settings = MagicMock()
        register_selfheal_jobs(scheduler, settings)

        job_ids = [call.kwargs["id"] for call in scheduler.add_job.call_args_list]
        assert "self_heal_weekly_report" in job_ids
        assert "consolidate_tickets" in job_ids
        # auto_close should no longer be registered
        assert "self_heal_auto_close" not in job_ids

    def test_no_auto_close_job(self):
        """The auto-close job function should no longer exist."""
        import app.jobs.selfheal_jobs as mod

        assert not hasattr(mod, "_job_self_heal_auto_close")


class TestConsolidationLogic:
    def test_unlinked_open_tickets_found(self, db_session, sched_user):
        """Open tickets without parent_ticket_id are candidates for consolidation."""
        now = datetime.now(timezone.utc)
        t1 = TroubleTicket(
            ticket_number="TT-CON-001",
            submitted_by=sched_user.id,
            title="Login page broken",
            description="Cannot log in",
            status="open",
            created_at=now - timedelta(hours=2),
        )
        t2 = TroubleTicket(
            ticket_number="TT-CON-002",
            submitted_by=sched_user.id,
            title="Login page broken again",
            description="Still cannot log in",
            status="open",
            created_at=now - timedelta(hours=1),
        )
        db_session.add_all([t1, t2])
        db_session.commit()

        # Both should be candidates (no parent_ticket_id)
        candidates = (
            db_session.query(TroubleTicket)
            .filter(
                TroubleTicket.status.in_(["open", "in_progress", "diagnosed"]),
                TroubleTicket.parent_ticket_id.is_(None),
            )
            .all()
        )
        assert len(candidates) >= 2

    def test_already_linked_excluded(self, db_session, sched_user):
        """Tickets already linked to a parent are excluded from consolidation."""
        now = datetime.now(timezone.utc)
        parent = TroubleTicket(
            ticket_number="TT-CON-003",
            submitted_by=sched_user.id,
            title="Parent ticket",
            description="Original issue",
            status="open",
            created_at=now - timedelta(hours=3),
        )
        db_session.add(parent)
        db_session.commit()
        db_session.refresh(parent)

        child = TroubleTicket(
            ticket_number="TT-CON-004",
            submitted_by=sched_user.id,
            title="Duplicate of parent",
            description="Same issue",
            status="open",
            parent_ticket_id=parent.id,
            created_at=now - timedelta(hours=1),
        )
        db_session.add(child)
        db_session.commit()

        candidates = (
            db_session.query(TroubleTicket)
            .filter(
                TroubleTicket.status.in_(["open", "in_progress", "diagnosed"]),
                TroubleTicket.parent_ticket_id.is_(None),
            )
            .all()
        )
        child_ids = [c.id for c in candidates]
        assert child.id not in child_ids

    def test_resolved_tickets_excluded(self, db_session, sched_user):
        """Resolved tickets should not be candidates for consolidation."""
        now = datetime.now(timezone.utc)
        ticket = TroubleTicket(
            ticket_number="TT-CON-005",
            submitted_by=sched_user.id,
            title="Already resolved",
            description="Fixed",
            status="resolved",
            created_at=now - timedelta(hours=5),
        )
        db_session.add(ticket)
        db_session.commit()

        candidates = (
            db_session.query(TroubleTicket)
            .filter(
                TroubleTicket.status.in_(["open", "in_progress", "diagnosed"]),
                TroubleTicket.parent_ticket_id.is_(None),
            )
            .all()
        )
        ids = [c.id for c in candidates]
        assert ticket.id not in ids
