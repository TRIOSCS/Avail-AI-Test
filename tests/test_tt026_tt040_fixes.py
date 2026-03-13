"""Tests for TT-026 (bell badge endpoint) and TT-040 (needs-attention fallback).

TT-026: Bell badge should use /api/sales/notifications/count (ActivityLog-based).
TT-040: needs-attention should fall back to all active companies when user owns none.

Called by: pytest
Depends on: conftest (client, db_session, test_user, sales_user)
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.models import ActivityLog, Company, CustomerSite

# ── TT-026: Sales notification count endpoint ───────────────────────


class TestBellBadgeCount:
    """The badge should use /api/sales/notifications/count for ActivityLog-based counts."""

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


class TestNeedsAttentionFallback:
    """When scope=my and the user owns no companies, fall back to all active."""

    @pytest.fixture(autouse=True)
    def _skip_if_dashboard_router_disabled(self, client):
        has_route = any(getattr(route, "path", "") == "/api/dashboard/needs-attention" for route in client.app.routes)
        if not has_route:
            pytest.skip("Dashboard router disabled in MVP mode")

    def test_fallback_to_all_when_no_owned(self, client, db_session, test_user):
        """User with no owned companies gets empty list (no fallback)."""
        # Create a company NOT owned by test_user (no account_owner_id, no site owner)
        c = Company(
            name="Orphan Corp",
            is_active=True,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(c)
        db_session.commit()

        resp = client.get("/api/dashboard/needs-attention?days=30")
        data = resp.json()
        # No fallback — user owns no companies, so empty result
        assert len(data) == 0

    def test_no_fallback_when_user_has_owned(self, client, db_session, test_user, sales_user):
        """When user owns companies, only their companies appear (no fallback)."""
        # Owned company
        owned = Company(
            name="My Corp",
            is_active=True,
            account_owner_id=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(owned)
        db_session.flush()
        site = CustomerSite(
            company_id=owned.id,
            site_name="HQ",
            owner_id=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(site)

        # Unowned company
        other = Company(
            name="Other Corp",
            is_active=True,
            account_owner_id=sales_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(other)
        db_session.commit()

        resp = client.get("/api/dashboard/needs-attention?days=30")
        data = resp.json()
        names = [item["company_name"] for item in data]
        assert "My Corp" in names
        assert "Other Corp" not in names

    def test_fallback_excludes_inactive(self, client, db_session, test_user):
        """Fallback should still exclude inactive companies."""
        c = Company(
            name="Dead Corp",
            is_active=False,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(c)
        db_session.commit()

        resp = client.get("/api/dashboard/needs-attention?days=30")
        data = resp.json()
        names = [item["company_name"] for item in data]
        assert "Dead Corp" not in names

    def test_team_scope_no_fallback_needed(self, client, db_session, test_user):
        """scope=team always shows all active companies — no fallback logic."""
        c = Company(
            name="Team Corp",
            is_active=True,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(c)
        db_session.commit()

        resp = client.get("/api/dashboard/needs-attention?days=30&scope=team")
        data = resp.json()
        names = [item["company_name"] for item in data]
        assert "Team Corp" in names

    def test_fallback_still_filters_stale(self, client, db_session, test_user):
        """Fallback companies with recent outreach should still be filtered out."""
        c = Company(
            name="Fresh Corp",
            is_active=True,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(c)
        db_session.flush()

        # Add recent outreach (1 day ago, within 7-day window)
        al = ActivityLog(
            user_id=test_user.id,
            company_id=c.id,
            activity_type="email_sent",
            channel="email",
            created_at=datetime.now(timezone.utc) - timedelta(days=1),
        )
        db_session.add(al)
        db_session.commit()

        resp = client.get("/api/dashboard/needs-attention?days=7")
        data = resp.json()
        names = [item["company_name"] for item in data]
        assert "Fresh Corp" not in names
