"""tests/test_unmatched_activities_ui.py — Data-Ops unmatched-activity review card
(HTMX).

Covers the thin HTMX UI over the existing Phase 2A unmatched-activity queue service
(app/services/activity_service.py get_unmatched_activities / count_unmatched_activities /
attribute_activity / dismiss_activity): the lazy-loaded card partial, the attribute-to-
company/vendor forms, and the dismiss action — mounted on the Settings Data-Ops tab
alongside the vendor/company/contact dedup sections.

Called by: pytest
Depends on: conftest.py fixtures (admin_user, test_user, test_company, test_vendor_card,
    db_session), app.routers.htmx.settings, app.models.ActivityLog
"""

import os

os.environ["TESTING"] = "1"

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.models import ActivityLog

CARD_URL = "/v2/partials/data-ops/unmatched-activities"


# ── Helpers ────────────────────────────────────────────────────────────


def _make_client(db_session, user):
    """Return a TestClient authenticated as *user* (mirrors
    test_unmatched_activities.py)."""
    from app.database import get_db
    from app.dependencies import require_admin, require_buyer, require_user
    from app.main import app

    def _override_db():
        yield db_session

    def _override_user():
        return user

    def _override_buyer():
        return user

    def _override_admin():
        if user.role != "admin":
            from fastapi import HTTPException

            raise HTTPException(403, "Admin access required")
        return user

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_user] = _override_user
    app.dependency_overrides[require_buyer] = _override_buyer
    app.dependency_overrides[require_admin] = _override_admin

    try:
        client = TestClient(app)
        yield client
    finally:
        for dep in [get_db, require_user, require_buyer, require_admin]:
            app.dependency_overrides.pop(dep, None)


@pytest.fixture()
def admin_client(db_session, admin_user):
    yield from _make_client(db_session, admin_user)


@pytest.fixture()
def buyer_client(db_session, test_user):
    yield from _make_client(db_session, test_user)


def _make_unmatched(db_session, user_id, email="unknown@x.com", ext_id="ui-1", subject="Stock offer"):
    a = ActivityLog(
        user_id=user_id,
        activity_type="email_received",
        channel="email",
        company_id=None,
        vendor_card_id=None,
        contact_email=email,
        contact_name="Random Person",
        subject=subject,
        external_id=ext_id,
        created_at=datetime.now(UTC),
    )
    db_session.add(a)
    db_session.commit()
    return a


# ── (a) admin GET renders a seeded unmatched activity + count ──────────


class TestCardRender:
    def test_admin_get_renders_activity_and_count(self, admin_client, db_session, admin_user):
        a = _make_unmatched(db_session, admin_user.id)

        resp = admin_client.get(CARD_URL)
        assert resp.status_code == 200
        html = resp.text
        assert "Stock offer" in html
        assert "unknown@x.com" in html
        assert f'data-activity="{a.id}"' in html
        # count badge reflects the single seeded row
        assert "1" in html

    # ── (f) empty queue → "No unmatched activity" state ─────────────

    def test_empty_queue_renders_empty_state(self, admin_client, db_session):
        resp = admin_client.get(CARD_URL)
        assert resp.status_code == 200
        assert "No unmatched activity" in resp.text


# ── (b) non-admin GET → 403 ─────────────────────────────────────────────


class TestAdminGate:
    def test_non_admin_get_forbidden(self, buyer_client, db_session, test_user):
        _make_unmatched(db_session, test_user.id)

        resp = buyer_client.get(CARD_URL)
        assert resp.status_code == 403

    def test_non_admin_attribute_forbidden(self, buyer_client, db_session, test_user):
        a = _make_unmatched(db_session, test_user.id)

        resp = buyer_client.post(
            f"{CARD_URL}/{a.id}/attribute",
            data={"entity_type": "company", "entity_id": "1"},
        )
        assert resp.status_code == 403

    def test_non_admin_dismiss_forbidden(self, buyer_client, db_session, test_user):
        a = _make_unmatched(db_session, test_user.id)

        resp = buyer_client.post(f"{CARD_URL}/{a.id}/dismiss")
        assert resp.status_code == 403


# ── (c) POST attribute with a valid company id ──────────────────────────


class TestAttribute:
    def test_attribute_to_valid_company_removes_row(self, admin_client, db_session, admin_user, test_company):
        a = _make_unmatched(db_session, admin_user.id)

        resp = admin_client.post(
            f"{CARD_URL}/{a.id}/attribute",
            data={"entity_type": "company", "entity_id": str(test_company.id)},
        )
        assert resp.status_code == 200
        assert f'data-activity="{a.id}"' not in resp.text
        assert "No unmatched activity" in resp.text

        db_session.refresh(a)
        assert a.company_id == test_company.id
        assert a.vendor_card_id is None

    def test_attribute_to_valid_vendor_removes_row(self, admin_client, db_session, admin_user, test_vendor_card):
        a = _make_unmatched(db_session, admin_user.id, ext_id="ui-2")

        resp = admin_client.post(
            f"{CARD_URL}/{a.id}/attribute",
            data={"entity_type": "vendor", "entity_id": str(test_vendor_card.id)},
        )
        assert resp.status_code == 200
        assert f'data-activity="{a.id}"' not in resp.text

        db_session.refresh(a)
        assert a.vendor_card_id == test_vendor_card.id
        assert a.company_id is None

    # ── (d) POST attribute with a nonexistent entity id → friendly error, no 500 ──

    def test_attribute_nonexistent_company_is_friendly(self, admin_client, db_session, admin_user):
        a = _make_unmatched(db_session, admin_user.id, ext_id="ui-3")

        resp = admin_client.post(
            f"{CARD_URL}/{a.id}/attribute",
            data={"entity_type": "company", "entity_id": "999999"},
        )
        assert resp.status_code == 200
        # The row is still present — attribution did not silently succeed.
        assert f'data-activity="{a.id}"' in resp.text

        db_session.refresh(a)
        assert a.company_id is None

    def test_attribute_invalid_entity_type_is_friendly(self, admin_client, db_session, admin_user):
        a = _make_unmatched(db_session, admin_user.id, ext_id="ui-4")

        resp = admin_client.post(
            f"{CARD_URL}/{a.id}/attribute",
            data={"entity_type": "bogus", "entity_id": "1"},
        )
        assert resp.status_code == 200
        assert f'data-activity="{a.id}"' in resp.text

    def test_attribute_nonexistent_activity_is_friendly(self, admin_client, db_session, test_company):
        resp = admin_client.post(
            f"{CARD_URL}/999999/attribute",
            data={"entity_type": "company", "entity_id": str(test_company.id)},
        )
        assert resp.status_code == 200
        # Friendly toast naming the activity, not a 500 or bare error page.
        trigger = resp.headers.get("HX-Trigger", "")
        assert "Activity not found." in trigger
        assert '"type": "error"' in trigger
        # The card itself still renders (heading is always present, with rows or not).
        assert "Unmatched Activity" in resp.text


# ── (e) POST dismiss sets dismissed_at and removes the row ─────────────


class TestDismiss:
    def test_dismiss_removes_row_and_sets_timestamp(self, admin_client, db_session, admin_user):
        a = _make_unmatched(db_session, admin_user.id, ext_id="ui-5")

        resp = admin_client.post(f"{CARD_URL}/{a.id}/dismiss")
        assert resp.status_code == 200
        assert f'data-activity="{a.id}"' not in resp.text
        assert "No unmatched activity" in resp.text

        db_session.refresh(a)
        assert a.dismissed_at is not None

    def test_dismiss_nonexistent_is_friendly(self, admin_client, db_session):
        resp = admin_client.post(f"{CARD_URL}/999999/dismiss")
        assert resp.status_code == 200
        # Friendly toast naming the activity, not a 500 or bare error page.
        trigger = resp.headers.get("HX-Trigger", "")
        assert "Activity not found." in trigger
        assert '"type": "error"' in trigger
        # The card itself still renders (heading is always present, with rows or not).
        assert "Unmatched Activity" in resp.text
