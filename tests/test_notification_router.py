"""Tests for notification router endpoints.

Covers: GET /api/notifications, GET /api/notifications/unread,
POST /api/notifications/{id}/read, POST /api/notifications/read-all.

Called by: pytest
Depends on: app.routers.notifications, app.services.notification_service
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session

from app.models import User
from app.services.notification_service import create_notification


@pytest.fixture()
def notif_user(db_session: Session) -> User:
    """A user for notification router tests."""
    user = User(
        email="notifrouter@trioscs.com",
        name="Notif Router User",
        role="buyer",
        azure_id="test-notif-router-001",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture()
def notif_client(db_session: Session, notif_user: User):
    """TestClient authenticated as notif_user."""
    from fastapi.testclient import TestClient

    from app.database import get_db
    from app.dependencies import require_user
    from app.main import app

    def _override_db():
        yield db_session

    def _override_user():
        return notif_user

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_user] = _override_user

    with TestClient(app) as c:
        yield c

    app.dependency_overrides.clear()


@pytest.fixture()
def seed_notifications(db_session: Session, notif_user: User):
    """Create 3 notifications: 2 unread, 1 read."""
    n1 = create_notification(db_session, notif_user.id, "diagnosed", "Ticket diagnosed")
    n2 = create_notification(db_session, notif_user.id, "fixed", "Fix applied")
    n3 = create_notification(db_session, notif_user.id, "escalated", "Escalated")
    n3.is_read = True
    db_session.commit()
    return [n1, n2, n3]


class TestListNotifications:
    def test_returns_all_with_counts(self, notif_client, seed_notifications):
        resp = notif_client.get("/api/notifications")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 3
        assert data["unread_count"] == 2
        assert len(data["items"]) == 3

    def test_pagination(self, notif_client, seed_notifications):
        resp = notif_client.get("/api/notifications?limit=2&offset=0")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["items"]) == 2
        assert data["total"] == 3

    def test_empty_when_no_notifications(self, notif_client):
        resp = notif_client.get("/api/notifications")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["items"] == []


class TestUnreadNotifications:
    def test_returns_unread_only(self, notif_client, seed_notifications):
        resp = notif_client.get("/api/notifications/unread")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 2
        assert all(not n["is_read"] for n in data["items"])

    def test_empty_when_all_read(self, notif_client, db_session, notif_user):
        n = create_notification(db_session, notif_user.id, "fixed", "Done")
        n.is_read = True
        db_session.commit()
        resp = notif_client.get("/api/notifications/unread")
        assert resp.status_code == 200
        assert resp.json()["count"] == 0


class TestMarkRead:
    def test_marks_single(self, notif_client, seed_notifications):
        notif_id = seed_notifications[0].id
        resp = notif_client.post(f"/api/notifications/{notif_id}/read")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        # Verify it's actually read now
        resp2 = notif_client.get("/api/notifications/unread")
        assert resp2.json()["count"] == 1

    def test_404_for_missing(self, notif_client):
        resp = notif_client.post("/api/notifications/99999/read")
        assert resp.status_code == 404


class TestMarkAllRead:
    def test_marks_all(self, notif_client, seed_notifications):
        resp = notif_client.post("/api/notifications/read-all")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["count"] == 2  # 2 were unread
        # Verify
        resp2 = notif_client.get("/api/notifications/unread")
        assert resp2.json()["count"] == 0

    def test_returns_zero_when_none_unread(self, notif_client):
        resp = notif_client.post("/api/notifications/read-all")
        assert resp.status_code == 200
        assert resp.json()["count"] == 0
