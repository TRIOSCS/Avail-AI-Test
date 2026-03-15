"""Tests for TT-026 (bell badge endpoint) and TT-040 (needs-attention fallback).

TT-026: Bell badge should use /api/sales/notifications/count (ActivityLog-based).
TT-040: needs-attention should fall back to all active companies when user owns none.

Called by: pytest
Depends on: conftest (client, db_session, test_user, sales_user)
"""

from datetime import datetime, timedelta, timezone

from app.models import ActivityLog

# ── TT-026: Sales notification count endpoint ───────────────────────


class TestBellBadgeCount:
    """The badge should use /api/sales/notifications/count for ActivityLog-based
    counts."""

    def test_count_zero_when_no_notifications(self, client):
        """No activity logs -> count 0."""
        resp = client.get("/api/sales/notifications/count")
        assert resp.status_code == 200
        assert resp.json()["count"] == 0

    def test_count_reflects_undismissed_notifications(self, client, db_session, test_user):
        """Undismissed notification-type activities should be counted."""
        for i in range(3):
            al = ActivityLog(
                user_id=test_user.id,
                activity_type="offer_pending_review",
                channel="system",
                subject=f"Offer {i}",
                created_at=datetime.now(timezone.utc) - timedelta(hours=i),
            )
            db_session.add(al)
        db_session.commit()

        resp = client.get("/api/sales/notifications/count")
        assert resp.status_code == 200
        assert resp.json()["count"] == 3

    def test_dismissed_not_counted(self, client, db_session, test_user):
        """Dismissed (read) notifications should not appear in count."""
        al = ActivityLog(
            user_id=test_user.id,
            activity_type="offer_pending_review",
            channel="system",
            subject="Read offer",
            dismissed_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(al)
        db_session.commit()

        resp = client.get("/api/sales/notifications/count")
        assert resp.json()["count"] == 0

    def test_old_notifications_not_counted(self, client, db_session, test_user):
        """Notifications older than 14 days should not appear."""
        al = ActivityLog(
            user_id=test_user.id,
            activity_type="offer_pending_review",
            channel="system",
            subject="Old offer",
            created_at=datetime.now(timezone.utc) - timedelta(days=15),
        )
        db_session.add(al)
        db_session.commit()

        resp = client.get("/api/sales/notifications/count")
        assert resp.json()["count"] == 0

    def test_mark_read_decrements_count(self, client, db_session, test_user):
        """Marking a notification read should reduce the count."""
        al = ActivityLog(
            user_id=test_user.id,
            activity_type="offer_pending_review",
            channel="system",
            subject="To dismiss",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(al)
        db_session.commit()
        db_session.refresh(al)

        # Count before
        resp = client.get("/api/sales/notifications/count")
        assert resp.json()["count"] == 1

        # Mark read
        resp = client.post(f"/api/sales/notifications/{al.id}/read")
        assert resp.status_code == 200

        # Count after
        resp = client.get("/api/sales/notifications/count")
        assert resp.json()["count"] == 0

    def test_mark_all_read_zeros_count(self, client, db_session, test_user):
        """Mark-all-read should zero out the count."""
        for i in range(3):
            db_session.add(
                ActivityLog(
                    user_id=test_user.id,
                    activity_type="offer_pending_review",
                    channel="system",
                    subject=f"Notif {i}",
                    created_at=datetime.now(timezone.utc),
                )
            )
        db_session.commit()

        resp = client.get("/api/sales/notifications/count")
        assert resp.json()["count"] == 3

        resp = client.post("/api/sales/notifications/read-all")
        assert resp.status_code == 200

        resp = client.get("/api/sales/notifications/count")
        assert resp.json()["count"] == 0


# ── TT-040: needs-attention fallback ─────────────────────────────────
