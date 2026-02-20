"""Tests for Phase 2A: Unmatched Activity Queue.

Covers:
- log_email_activity creates unmatched records (null company/vendor)
- log_call_activity creates unmatched records
- get_unmatched_activities filters correctly
- attribute_activity assigns to company or vendor
- dismiss_activity marks dismissed_at
- API endpoints: list, attribute, dismiss (admin only)
- Non-admin cannot access unmatched queue

Called by: pytest
Depends on: conftest.py fixtures, app.services.activity_service, app.routers.v13_features
"""

import os

os.environ["TESTING"] = "1"

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.models import ActivityLog

# ── Helpers ────────────────────────────────────────────────────────────


def _make_client(db_session, user):
    """Return a TestClient authenticated as the given user."""
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

    client = TestClient(app)
    yield client
    app.dependency_overrides.clear()


@pytest.fixture()
def admin_client(db_session, admin_user):
    yield from _make_client(db_session, admin_user)


@pytest.fixture()
def buyer_client(db_session, test_user):
    yield from _make_client(db_session, test_user)


# ── Service-level tests ───────────────────────────────────────────────


class TestUnmatchedEmailLogging:
    """log_email_activity should create records even when no match found."""

    def test_unmatched_email_creates_activity(self, db_session, test_user):
        """An email with no matching company/vendor still gets logged."""
        from app.services.activity_service import log_email_activity

        record = log_email_activity(
            user_id=test_user.id,
            direction="received",
            email_addr="unknown@randomdomain.xyz",
            subject="Stock list attached",
            external_id="graph-unmatched-001",
            contact_name="Random Person",
            db=db_session,
        )
        db_session.commit()

        assert record is not None
        assert record.company_id is None
        assert record.vendor_card_id is None
        assert record.contact_email == "unknown@randomdomain.xyz"
        assert record.activity_type == "email_received"

    def test_matched_email_still_works(self, db_session, test_user, test_company):
        """Emails matching a company still attribute correctly."""
        from app.models import CustomerSite
        from app.services.activity_service import log_email_activity

        site = CustomerSite(
            company_id=test_company.id,
            site_name="Acme HQ",
            contact_email="buyer@acme-electronics.com",
            is_active=True,
        )
        db_session.add(site)
        db_session.flush()

        record = log_email_activity(
            user_id=test_user.id,
            direction="sent",
            email_addr="buyer@acme-electronics.com",
            subject="RFQ follow-up",
            external_id="graph-matched-001",
            contact_name="Buyer Person",
            db=db_session,
        )
        db_session.commit()

        assert record is not None
        assert record.company_id == test_company.id
        assert record.vendor_card_id is None


class TestUnmatchedCallLogging:
    """log_call_activity should create records even when no match found."""

    def test_unmatched_call_creates_activity(self, db_session, test_user):
        from app.services.activity_service import log_call_activity

        record = log_call_activity(
            user_id=test_user.id,
            direction="outbound",
            phone="+1-555-999-0000",
            duration_seconds=120,
            external_id="call-unmatched-001",
            contact_name="Unknown Caller",
            db=db_session,
        )
        db_session.commit()

        assert record is not None
        assert record.company_id is None
        assert record.vendor_card_id is None
        assert record.contact_phone == "+1-555-999-0000"


class TestUnmatchedQueueService:
    """Test get_unmatched_activities, attribute, and dismiss functions."""

    def _create_unmatched(self, db_session, user_id, email="x@unknown.com", ext_id=None):
        a = ActivityLog(
            user_id=user_id,
            activity_type="email_received",
            channel="email",
            company_id=None,
            vendor_card_id=None,
            contact_email=email,
            external_id=ext_id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(a)
        db_session.flush()
        return a

    def test_get_unmatched_returns_only_null_entities(
        self, db_session, test_user, test_company
    ):
        from app.services.activity_service import get_unmatched_activities

        # One unmatched
        unmatched = self._create_unmatched(db_session, test_user.id, "a@x.com", "u1")
        # One matched (has company)
        matched = ActivityLog(
            user_id=test_user.id,
            activity_type="email_sent",
            channel="email",
            company_id=test_company.id,
            contact_email="b@known.com",
            external_id="m1",
        )
        db_session.add(matched)
        db_session.commit()

        results = get_unmatched_activities(db_session)
        assert len(results) == 1
        assert results[0].id == unmatched.id

    def test_dismissed_excluded(self, db_session, test_user):
        from app.services.activity_service import (
            dismiss_activity,
            get_unmatched_activities,
        )

        a = self._create_unmatched(db_session, test_user.id, "d@x.com", "d1")
        db_session.commit()

        assert len(get_unmatched_activities(db_session)) == 1

        dismiss_activity(a.id, db_session)
        db_session.commit()

        assert len(get_unmatched_activities(db_session)) == 0

    def test_count_unmatched(self, db_session, test_user):
        from app.services.activity_service import count_unmatched_activities

        assert count_unmatched_activities(db_session) == 0
        self._create_unmatched(db_session, test_user.id, "c1@x.com", "c1")
        self._create_unmatched(db_session, test_user.id, "c2@x.com", "c2")
        db_session.commit()
        assert count_unmatched_activities(db_session) == 2

    def test_attribute_to_company(self, db_session, test_user, test_company):
        from app.services.activity_service import attribute_activity

        a = self._create_unmatched(db_session, test_user.id, "attr@x.com", "a1")
        db_session.commit()

        result = attribute_activity(a.id, "company", test_company.id, db_session)
        db_session.commit()

        assert result is not None
        assert result.company_id == test_company.id
        assert result.vendor_card_id is None

    def test_attribute_to_vendor(self, db_session, test_user, test_vendor_card):
        from app.services.activity_service import attribute_activity

        a = self._create_unmatched(db_session, test_user.id, "v@x.com", "v1")
        db_session.commit()

        result = attribute_activity(
            a.id, "vendor", test_vendor_card.id, db_session
        )
        db_session.commit()

        assert result is not None
        assert result.vendor_card_id == test_vendor_card.id
        assert result.company_id is None

    def test_attribute_invalid_type(self, db_session, test_user):
        from app.services.activity_service import attribute_activity

        a = self._create_unmatched(db_session, test_user.id, "bad@x.com", "b1")
        db_session.commit()

        result = attribute_activity(a.id, "invalid", 1, db_session)
        assert result is None

    def test_attribute_nonexistent_activity(self, db_session):
        from app.services.activity_service import attribute_activity

        result = attribute_activity(99999, "company", 1, db_session)
        assert result is None

    def test_dismiss_sets_timestamp(self, db_session, test_user):
        from app.services.activity_service import dismiss_activity

        a = self._create_unmatched(db_session, test_user.id, "dis@x.com", "dis1")
        db_session.commit()
        assert a.dismissed_at is None

        result = dismiss_activity(a.id, db_session)
        db_session.commit()

        assert result.dismissed_at is not None

    def test_dismiss_nonexistent(self, db_session):
        from app.services.activity_service import dismiss_activity

        result = dismiss_activity(99999, db_session)
        assert result is None


# ── API endpoint tests ─────────────────────────────────────────────────


class TestUnmatchedEndpoints:
    """Test the unmatched activity API endpoints."""

    def _seed_unmatched(self, db_session, user_id, count=3):
        for i in range(count):
            db_session.add(ActivityLog(
                user_id=user_id,
                activity_type="email_received",
                channel="email",
                company_id=None,
                vendor_card_id=None,
                contact_email=f"unknown{i}@test.com",
                external_id=f"api-test-{i}",
                created_at=datetime.now(timezone.utc),
            ))
        db_session.commit()

    def test_list_unmatched_admin(self, admin_client, db_session, admin_user):
        self._seed_unmatched(db_session, admin_user.id)

        resp = admin_client.get("/api/activities/unmatched")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 3
        assert len(data["items"]) == 3

    def test_list_unmatched_non_admin_forbidden(self, buyer_client, db_session, test_user):
        self._seed_unmatched(db_session, test_user.id)

        resp = buyer_client.get("/api/activities/unmatched")
        assert resp.status_code == 403

    def test_attribute_endpoint(
        self, admin_client, db_session, admin_user, test_company
    ):
        a = ActivityLog(
            user_id=admin_user.id,
            activity_type="email_received",
            channel="email",
            contact_email="x@y.com",
            external_id="attr-api-1",
        )
        db_session.add(a)
        db_session.commit()

        resp = admin_client.post(
            f"/api/activities/{a.id}/attribute",
            json={"entity_type": "company", "entity_id": test_company.id},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "attributed"
        assert data["activity"]["company_id"] == test_company.id

    def test_attribute_nonexistent_activity(self, admin_client, test_company):
        resp = admin_client.post(
            "/api/activities/99999/attribute",
            json={"entity_type": "company", "entity_id": test_company.id},
        )
        assert resp.status_code == 404

    def test_attribute_nonexistent_company(self, admin_client, db_session, admin_user):
        a = ActivityLog(
            user_id=admin_user.id,
            activity_type="email_received",
            channel="email",
            contact_email="x@y.com",
            external_id="attr-api-2",
        )
        db_session.add(a)
        db_session.commit()

        resp = admin_client.post(
            f"/api/activities/{a.id}/attribute",
            json={"entity_type": "company", "entity_id": 99999},
        )
        assert resp.status_code == 404

    def test_dismiss_endpoint(self, admin_client, db_session, admin_user):
        a = ActivityLog(
            user_id=admin_user.id,
            activity_type="email_received",
            channel="email",
            contact_email="dismiss@y.com",
            external_id="dis-api-1",
        )
        db_session.add(a)
        db_session.commit()

        resp = admin_client.post(f"/api/activities/{a.id}/dismiss")
        assert resp.status_code == 200
        assert resp.json()["status"] == "dismissed"

        # Verify it no longer shows in unmatched list
        resp2 = admin_client.get("/api/activities/unmatched")
        assert resp2.json()["total"] == 0

    def test_dismiss_nonexistent(self, admin_client):
        resp = admin_client.post("/api/activities/99999/dismiss")
        assert resp.status_code == 404

    def test_dismiss_non_admin_forbidden(self, buyer_client, db_session, test_user):
        a = ActivityLog(
            user_id=test_user.id,
            activity_type="email_received",
            channel="email",
            contact_email="x@y.com",
            external_id="dis-api-buyer",
        )
        db_session.add(a)
        db_session.commit()

        resp = buyer_client.post(f"/api/activities/{a.id}/dismiss")
        assert resp.status_code == 403

    def test_activity_to_dict_includes_dismissed_at(
        self, admin_client, db_session, admin_user
    ):
        a = ActivityLog(
            user_id=admin_user.id,
            activity_type="email_received",
            channel="email",
            contact_email="dict@y.com",
            external_id="dict-1",
        )
        db_session.add(a)
        db_session.commit()

        resp = admin_client.get("/api/activities/unmatched")
        item = resp.json()["items"][0]
        assert "dismissed_at" in item
        assert item["dismissed_at"] is None
