"""Tests for notification service.

Covers: create, get_unread, get_all, mark_read, mark_all_read, edge cases.

Called by: pytest
Depends on: app.services.notification_service, app.models.notification
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session

from app.models.notification import Notification
from app.models.trouble_ticket import TroubleTicket
from app.models import User
from app.services.notification_service import (
    create_notification,
    get_all,
    get_unread,
    mark_all_read,
    mark_read,
)


@pytest.fixture()
def notif_user(db_session: Session) -> User:
    """A user for notification tests."""
    user = User(
        email="notif@trioscs.com",
        name="Notif User",
        role="admin",
        azure_id="test-notif-001",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture()
def other_user(db_session: Session) -> User:
    """A second user to test isolation."""
    user = User(
        email="other@trioscs.com",
        name="Other User",
        role="buyer",
        azure_id="test-notif-002",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


class TestCreateNotification:
    def test_creates_notification(self, db_session, notif_user):
        notif = create_notification(
            db_session,
            user_id=notif_user.id,
            event_type="diagnosed",
            title="Ticket #1 diagnosed",
            body="Category: api, Risk: low",
            ticket_id=None,
        )
        assert notif.id is not None
        assert notif.user_id == notif_user.id
        assert notif.event_type == "diagnosed"
        assert notif.title == "Ticket #1 diagnosed"
        assert notif.body == "Category: api, Risk: low"
        assert notif.is_read is False
        assert notif.created_at is not None

    def test_creates_with_ticket_id(self, db_session, notif_user):
        ticket = TroubleTicket(
            ticket_number="TT-20260302-001",
            submitted_by=notif_user.id,
            title="Test ticket",
            description="For notification FK test",
        )
        db_session.add(ticket)
        db_session.commit()
        db_session.refresh(ticket)

        notif = create_notification(
            db_session,
            user_id=notif_user.id,
            event_type="fixed",
            title="Fix applied",
            ticket_id=ticket.id,
        )
        assert notif.ticket_id == ticket.id

    def test_creates_without_body(self, db_session, notif_user):
        notif = create_notification(
            db_session,
            user_id=notif_user.id,
            event_type="escalated",
            title="Escalated to human",
        )
        assert notif.body is None

    def test_all_event_types(self, db_session, notif_user):
        for event in ("diagnosed", "prompt_ready", "escalated", "fixed", "failed"):
            notif = create_notification(
                db_session,
                user_id=notif_user.id,
                event_type=event,
                title=f"Event: {event}",
            )
            assert notif.event_type == event


class TestGetUnread:
    def test_returns_unread_only(self, db_session, notif_user):
        create_notification(db_session, notif_user.id, "diagnosed", "Unread 1")
        create_notification(db_session, notif_user.id, "fixed", "Unread 2")
        # Mark one as read directly
        read_notif = create_notification(db_session, notif_user.id, "escalated", "Read")
        read_notif.is_read = True
        db_session.commit()

        unread = get_unread(db_session, notif_user.id)
        assert len(unread) == 2
        assert all(not n["is_read"] for n in unread)

    def test_newest_first(self, db_session, notif_user):
        create_notification(db_session, notif_user.id, "diagnosed", "First")
        create_notification(db_session, notif_user.id, "fixed", "Second")
        unread = get_unread(db_session, notif_user.id)
        assert unread[0]["title"] == "Second"
        assert unread[1]["title"] == "First"

    def test_respects_limit(self, db_session, notif_user):
        for i in range(5):
            create_notification(db_session, notif_user.id, "diagnosed", f"N{i}")
        unread = get_unread(db_session, notif_user.id, limit=3)
        assert len(unread) == 3

    def test_empty_when_none(self, db_session, notif_user):
        unread = get_unread(db_session, notif_user.id)
        assert unread == []

    def test_user_isolation(self, db_session, notif_user, other_user):
        create_notification(db_session, notif_user.id, "diagnosed", "User A")
        create_notification(db_session, other_user.id, "diagnosed", "User B")
        unread_a = get_unread(db_session, notif_user.id)
        unread_b = get_unread(db_session, other_user.id)
        assert len(unread_a) == 1
        assert unread_a[0]["title"] == "User A"
        assert len(unread_b) == 1
        assert unread_b[0]["title"] == "User B"


class TestGetAll:
    def test_returns_all_with_counts(self, db_session, notif_user):
        create_notification(db_session, notif_user.id, "diagnosed", "N1")
        n2 = create_notification(db_session, notif_user.id, "fixed", "N2")
        n2.is_read = True
        db_session.commit()

        result = get_all(db_session, notif_user.id)
        assert result["total"] == 2
        assert result["unread_count"] == 1
        assert len(result["items"]) == 2

    def test_pagination(self, db_session, notif_user):
        for i in range(5):
            create_notification(db_session, notif_user.id, "diagnosed", f"N{i}")
        result = get_all(db_session, notif_user.id, limit=2, offset=0)
        assert len(result["items"]) == 2
        assert result["total"] == 5

        result2 = get_all(db_session, notif_user.id, limit=2, offset=2)
        assert len(result2["items"]) == 2

    def test_empty_user(self, db_session, notif_user):
        result = get_all(db_session, notif_user.id)
        assert result["total"] == 0
        assert result["unread_count"] == 0
        assert result["items"] == []


class TestMarkRead:
    def test_marks_single_read(self, db_session, notif_user):
        notif = create_notification(db_session, notif_user.id, "diagnosed", "Test")
        assert mark_read(db_session, notif.id, notif_user.id) is True
        db_session.refresh(notif)
        assert notif.is_read is True

    def test_returns_false_for_missing(self, db_session, notif_user):
        assert mark_read(db_session, 99999, notif_user.id) is False

    def test_cannot_mark_other_users_notification(self, db_session, notif_user, other_user):
        notif = create_notification(db_session, notif_user.id, "diagnosed", "Private")
        assert mark_read(db_session, notif.id, other_user.id) is False
        db_session.refresh(notif)
        assert notif.is_read is False  # unchanged


class TestMarkAllRead:
    def test_marks_all_unread(self, db_session, notif_user):
        create_notification(db_session, notif_user.id, "diagnosed", "N1")
        create_notification(db_session, notif_user.id, "fixed", "N2")
        count = mark_all_read(db_session, notif_user.id)
        assert count == 2
        unread = get_unread(db_session, notif_user.id)
        assert len(unread) == 0

    def test_returns_zero_when_none(self, db_session, notif_user):
        assert mark_all_read(db_session, notif_user.id) == 0

    def test_only_affects_own_notifications(self, db_session, notif_user, other_user):
        create_notification(db_session, notif_user.id, "diagnosed", "User A")
        create_notification(db_session, other_user.id, "diagnosed", "User B")
        mark_all_read(db_session, notif_user.id)
        # Other user's notification still unread
        unread_b = get_unread(db_session, other_user.id)
        assert len(unread_b) == 1

    def test_skips_already_read(self, db_session, notif_user):
        n1 = create_notification(db_session, notif_user.id, "diagnosed", "N1")
        n1.is_read = True
        db_session.commit()
        create_notification(db_session, notif_user.id, "fixed", "N2")
        count = mark_all_read(db_session, notif_user.id)
        assert count == 1  # only N2 was unread


class TestToDict:
    def test_dict_format(self, db_session, notif_user):
        ticket = TroubleTicket(
            ticket_number="TT-20260302-002",
            submitted_by=notif_user.id,
            title="Dict test ticket",
            description="For dict format test",
        )
        db_session.add(ticket)
        db_session.commit()
        db_session.refresh(ticket)

        notif = create_notification(
            db_session,
            user_id=notif_user.id,
            event_type="prompt_ready",
            title="Prompt generated",
            body="Fix prompt is ready for review",
            ticket_id=ticket.id,
        )
        unread = get_unread(db_session, notif_user.id)
        d = unread[0]
        assert d["id"] == notif.id
        assert d["user_id"] == notif_user.id
        assert d["ticket_id"] == ticket.id
        assert d["event_type"] == "prompt_ready"
        assert d["title"] == "Prompt generated"
        assert d["body"] == "Fix prompt is ready for review"
        assert d["is_read"] is False
        assert "created_at" in d
