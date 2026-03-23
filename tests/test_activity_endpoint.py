"""Tests for POST /api/activity/call-initiated endpoint.

Covers: valid calls, invalid phone, rate limiting, vendor resolution,
requirement-to-requisition resolution, unknown entity IDs.
"""

import pytest

from app.models import ActivityLog


@pytest.fixture(autouse=True)
def _clear_rate_limit():
    """Clear in-memory rate limiter between tests."""
    from app.routers.activity import _call_log

    _call_log.clear()
    yield
    _call_log.clear()


class TestCallInitiated:
    def test_basic_call(self, client, db_session):
        resp = client.post(
            "/api/activity/call-initiated",
            json={
                "phone_number": "4155551234",
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["id"] is not None

        record = db_session.get(ActivityLog, data["id"])
        assert record.activity_type == "phone_call"
        assert record.channel == "phone"
        assert record.contact_phone == "+14155551234"
        assert "Call to" in record.subject

    def test_with_vendor_card(self, client, db_session, test_vendor_card):
        resp = client.post(
            "/api/activity/call-initiated",
            json={
                "phone_number": "(415) 555-1234",
                "vendor_card_id": test_vendor_card.id,
            },
        )
        assert resp.status_code == 201
        record = db_session.get(ActivityLog, resp.json()["id"])
        assert record.vendor_card_id == test_vendor_card.id
        assert "Arrow Electronics" in record.subject

    def test_with_requirement(self, client, db_session, test_requisition):
        req_item = test_requisition.requirements[0]
        resp = client.post(
            "/api/activity/call-initiated",
            json={
                "phone_number": "+14155551234",
                "requirement_id": req_item.id,
            },
        )
        assert resp.status_code == 201
        record = db_session.get(ActivityLog, resp.json()["id"])
        assert record.requisition_id == test_requisition.id

    def test_invalid_phone_returns_400(self, client):
        resp = client.post(
            "/api/activity/call-initiated",
            json={
                "phone_number": "call john",
            },
        )
        assert resp.status_code == 400

    def test_empty_phone_returns_400(self, client):
        resp = client.post(
            "/api/activity/call-initiated",
            json={
                "phone_number": "",
            },
        )
        assert resp.status_code == 400

    def test_unknown_vendor_id_returns_500(self, client):
        """Unknown vendor FK causes DB integrity error, now properly surfaced as 500."""
        resp = client.post(
            "/api/activity/call-initiated",
            json={
                "phone_number": "4155551234",
                "vendor_card_id": 99999,
            },
        )
        assert resp.status_code == 500

    def test_unknown_requirement_id_still_succeeds(self, client):
        """Unknown requirement_id is gracefully handled (set to None), so call
        succeeds."""
        resp = client.post(
            "/api/activity/call-initiated",
            json={
                "phone_number": "4155551234",
                "requirement_id": 99999,
            },
        )
        assert resp.status_code == 201

    def test_rate_limit(self, client):
        # First 10 should succeed
        for _ in range(10):
            resp = client.post(
                "/api/activity/call-initiated",
                json={
                    "phone_number": "4155551234",
                },
            )
            assert resp.status_code == 201

        # 11th should be rate limited
        resp = client.post(
            "/api/activity/call-initiated",
            json={
                "phone_number": "4155551234",
            },
        )
        assert resp.status_code == 429

    def test_origin_recorded(self, client, db_session):
        resp = client.post(
            "/api/activity/call-initiated",
            json={
                "phone_number": "4155551234",
                "origin": "vendor_detail",
            },
        )
        assert resp.status_code == 201
        record = db_session.get(ActivityLog, resp.json()["id"])
        assert "origin=vendor_detail" in record.notes

    def test_with_company_id(self, client, db_session, test_company):
        resp = client.post(
            "/api/activity/call-initiated",
            json={
                "phone_number": "4155551234",
                "company_id": test_company.id,
            },
        )
        assert resp.status_code == 201
        record = db_session.get(ActivityLog, resp.json()["id"])
        assert record.company_id == test_company.id

    def test_db_error_returns_500(self, client, db_session):
        """DB commit failure should return HTTP 500, not silently return {id: None}."""
        original_commit = db_session.commit
        db_session.commit = lambda: (_ for _ in ()).throw(RuntimeError("DB down"))
        try:
            resp = client.post(
                "/api/activity/call-initiated",
                json={"phone_number": "4155551234"},
            )
        finally:
            db_session.commit = original_commit
        assert resp.status_code == 500
        assert resp.json()["error"] == "Failed to record phone contact"


class TestLastCall:
    def test_no_calls(self, client, test_vendor_card):
        resp = client.get(f"/api/activity/vendors/{test_vendor_card.id}/last-call")
        assert resp.status_code == 200
        assert resp.json()["last_call"] is None

    def test_with_call(self, client, db_session, test_vendor_card, test_user):
        # Create a call activity
        record = ActivityLog(
            user_id=test_user.id,
            activity_type="phone_call",
            channel="phone",
            vendor_card_id=test_vendor_card.id,
            contact_phone="+14155551234",
            subject="Call to Arrow Electronics",
        )
        db_session.add(record)
        db_session.commit()

        resp = client.get(f"/api/activity/vendors/{test_vendor_card.id}/last-call")
        assert resp.status_code == 200
        data = resp.json()
        assert data["last_call"] is not None
        assert data["last_call"]["user_name"] == "Test Buyer"
        assert data["is_current_user"] is True

    def test_nonexistent_vendor(self, client):
        resp = client.get("/api/activity/vendors/99999/last-call")
        assert resp.status_code == 200
        assert resp.json()["last_call"] is None
