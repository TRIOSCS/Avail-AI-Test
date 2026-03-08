"""Tests to close remaining coverage gaps.

Covers:
1. Dashboard helpers — _ensure_aware edge cases
2. Companies — substring duplicate check
3. Notification service — CRUD operations
4. Tagging AI — module import coverage

Called by: pytest
Depends on: conftest fixtures, app modules
"""

from datetime import datetime, timezone

from app.models import Company

# ══════════════════════════════════════════════════════════════════════
#  1. DASHBOARD — _ensure_aware + edge cases
# ══════════════════════════════════════════════════════════════════════


class TestDashboardHelpers:
    def test_ensure_aware_naive(self):
        from app.routers.dashboard import _ensure_aware

        naive = datetime(2024, 1, 1, 12, 0, 0)
        result = _ensure_aware(naive)
        assert result.tzinfo is not None

    def test_ensure_aware_already_aware(self):
        from app.routers.dashboard import _ensure_aware

        aware = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        result = _ensure_aware(aware)
        assert result == aware

    def test_ensure_aware_none(self):
        from app.routers.dashboard import _ensure_aware

        result = _ensure_aware(None)
        assert result is None


# ══════════════════════════════════════════════════════════════════════
#  2. COMPANIES — substring duplicate check
# ══════════════════════════════════════════════════════════════════════


class TestCompanySubstringMatch:
    def test_company_duplicate_substring(self, client, db_session):
        """Cover line 371: substring match in check-duplicate."""
        co = Company(name="Microchip Technology", is_active=True, created_at=datetime.now(timezone.utc))
        db_session.add(co)
        db_session.commit()

        resp = client.get("/api/companies/check-duplicate", params={"name": "Microchip"})
        assert resp.status_code == 200
        data = resp.json()
        assert len(data.get("matches", [])) > 0


# ══════════════════════════════════════════════════════════════════════
#  3. NOTIFICATION SERVICE
# ══════════════════════════════════════════════════════════════════════


class TestNotificationService:
    def test_create_notification(self, db_session, test_user):
        from app.services.notification_service import create_notification

        notif = create_notification(
            db_session,
            user_id=test_user.id,
            event_type="diagnosed",
            title="Test",
            body="Body",
            ticket_id=None,
        )
        assert notif.id is not None
        assert notif.event_type == "diagnosed"
        assert notif.is_read is False

    def test_get_unread(self, db_session, test_user):
        from app.services.notification_service import create_notification, get_unread

        create_notification(db_session, user_id=test_user.id, event_type="fixed", title="T1")
        create_notification(db_session, user_id=test_user.id, event_type="failed", title="T2")
        result = get_unread(db_session, test_user.id)
        assert len(result) == 2
        assert result[0]["title"] == "T2"  # newest first

    def test_get_all(self, db_session, test_user):
        from app.services.notification_service import create_notification, get_all

        create_notification(db_session, user_id=test_user.id, event_type="e", title="T")
        result = get_all(db_session, test_user.id)
        assert result["total"] >= 1
        assert result["unread_count"] >= 1
        assert len(result["items"]) >= 1

    def test_mark_read(self, db_session, test_user):
        from app.services.notification_service import create_notification, mark_read

        notif = create_notification(db_session, user_id=test_user.id, event_type="e", title="T")
        assert mark_read(db_session, notif.id, test_user.id) is True
        assert mark_read(db_session, 99999, test_user.id) is False

    def test_mark_all_read(self, db_session, test_user):
        from app.services.notification_service import create_notification, mark_all_read

        create_notification(db_session, user_id=test_user.id, event_type="e", title="T1")
        create_notification(db_session, user_id=test_user.id, event_type="e", title="T2")
        count = mark_all_read(db_session, test_user.id)
        assert count >= 2


# ══════════════════════════════════════════════════════════════════════
#  4. TAGGING_AI — module import coverage
# ══════════════════════════════════════════════════════════════════════


class TestTaggingAiImport:
    def test_module_imports(self):
        """Cover module-level imports and constants."""
        from app.services.tagging_ai import _CLASSIFY_PROMPT, _SYSTEM

        assert "classify" in _CLASSIFY_PROMPT.lower()
        assert len(_SYSTEM) > 0
