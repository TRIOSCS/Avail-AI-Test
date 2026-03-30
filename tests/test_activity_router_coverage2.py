"""tests/test_activity_router_coverage2.py — Additional coverage for activity router.

Targets uncovered branches in app/routers/v13_features/activity.py:
- graph_webhook (validation token, JSON error, invalid payload, service errors)
- teams_webhook (MVP mode, validation token, handler errors)
- acs_webhook (not configured, eventgrid handshake, call events)
- initiate_call_endpoint
- company/vendor activity endpoints (404 paths)
- email click / phone call (no_match paths)
- unmatched activity queue (list, attribute, dismiss)
- activity status endpoints

Called by: pytest
Depends on: conftest.py fixtures, app.routers.v13_features.activity
"""

import os

os.environ["TESTING"] = "1"

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models import ActivityLog, Company, User, VendorCard


# ── Graph Webhook ────────────────────────────────────────────────────


class TestGraphWebhook:
    def test_validation_token_returns_plaintext(self, client):
        """GET /api/webhooks/graph?validationToken=xyz returns token as plain text."""
        resp = client.post("/api/webhooks/graph?validationToken=my-validation-token")
        assert resp.status_code == 200
        assert resp.text == "my-validation-token"

    def test_invalid_json_returns_400(self, client):
        """Malformed request body returns 400."""
        resp = client.post(
            "/api/webhooks/graph",
            content=b"not-json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400

    def test_no_valid_notifications_returns_403(self, client):
        """Empty/unmatched notifications return 403."""
        with patch("app.services.webhook_service.validate_notifications", return_value=[]):
            resp = client.post(
                "/api/webhooks/graph",
                json={"value": []},
            )
        assert resp.status_code == 403

    def test_handle_notification_exception_returns_500(self, client):
        """If handle_notification raises, endpoint returns 500."""
        with patch("app.services.webhook_service.validate_notifications", return_value=[{"id": "1"}]):
            with patch(
                "app.services.webhook_service.handle_notification",
                new=AsyncMock(side_effect=RuntimeError("Processing error")),
            ):
                resp = client.post(
                    "/api/webhooks/graph",
                    json={"value": [{"id": "1"}]},
                )
        assert resp.status_code == 500

    def test_valid_notification_accepted(self, client):
        """Valid webhook notification returns 200 accepted."""
        with patch("app.services.webhook_service.validate_notifications", return_value=[{"id": "1"}]):
            with patch("app.services.webhook_service.handle_notification", new=AsyncMock(return_value=None)):
                resp = client.post(
                    "/api/webhooks/graph",
                    json={"value": [{"id": "1"}]},
                )
        assert resp.status_code == 200
        assert resp.json()["status"] == "accepted"


# ── Teams Webhook ────────────────────────────────────────────────────


class TestTeamsWebhook:
    def test_mvp_mode_returns_404(self, client, monkeypatch):
        """Teams webhook returns 404 when MVP mode is enabled."""
        from app.config import settings

        monkeypatch.setattr(settings, "mvp_mode", True)
        resp = client.post("/api/webhooks/teams", json={})
        assert resp.status_code == 404

    def test_validation_token_returns_plaintext(self, client, monkeypatch):
        """validationToken query param returns it as plain text."""
        from app.config import settings

        monkeypatch.setattr(settings, "mvp_mode", False)
        resp = client.post("/api/webhooks/teams?validationToken=teams-token")
        assert resp.status_code == 200
        assert resp.text == "teams-token"

    def test_invalid_json_returns_400(self, client, monkeypatch):
        """Malformed JSON returns 400."""
        from app.config import settings

        monkeypatch.setattr(settings, "mvp_mode", False)
        resp = client.post(
            "/api/webhooks/teams",
            content=b"bad",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400

    def test_no_valid_notifications_returns_403(self, client, monkeypatch):
        """Empty validated list returns 403."""
        from app.config import settings

        monkeypatch.setattr(settings, "mvp_mode", False)
        with patch("app.services.webhook_service.validate_notifications", return_value=[]):
            resp = client.post("/api/webhooks/teams", json={"value": []})
        assert resp.status_code == 403

    def test_handler_exception_returns_500(self, client, monkeypatch):
        """Teams handler exception returns 500."""
        from app.config import settings

        monkeypatch.setattr(settings, "mvp_mode", False)
        with patch("app.services.webhook_service.validate_notifications", return_value=[{"id": "x"}]):
            with patch(
                "app.services.webhook_service.handle_teams_notification",
                new=AsyncMock(side_effect=Exception("Teams error")),
            ):
                resp = client.post("/api/webhooks/teams", json={"value": [{"id": "x"}]})
        assert resp.status_code == 500

    def test_valid_teams_notification_accepted(self, client, monkeypatch):
        """Valid teams notification returns accepted."""
        from app.config import settings

        monkeypatch.setattr(settings, "mvp_mode", False)
        with patch("app.services.webhook_service.validate_notifications", return_value=[{"id": "x"}]):
            with patch(
                "app.services.webhook_service.handle_teams_notification",
                new=AsyncMock(return_value=None),
            ):
                resp = client.post("/api/webhooks/teams", json={"value": [{"id": "x"}]})
        assert resp.status_code == 200
        assert resp.json()["status"] == "accepted"


# ── ACS Webhook ─────────────────────────────────────────────────────


class TestAcsWebhook:
    def test_acs_not_configured_returns_503(self, client, monkeypatch):
        """ACS webhook returns 503 when acs_connection_string is not set."""
        from app.config import settings

        monkeypatch.setattr(settings, "acs_connection_string", None)
        resp = client.post("/api/webhooks/acs", json=[])
        assert resp.status_code == 503

    def test_invalid_json_returns_400(self, client, monkeypatch):
        """Malformed JSON returns 400."""
        from app.config import settings

        monkeypatch.setattr(settings, "acs_connection_string", "Endpoint=sb://test;")
        resp = client.post(
            "/api/webhooks/acs",
            content=b"not-json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400

    def test_eventgrid_validation_handshake(self, client, monkeypatch):
        """EventGrid subscription validation handshake returns validation code."""
        from app.config import settings

        monkeypatch.setattr(settings, "acs_connection_string", "Endpoint=sb://test;")
        payload = [
            {
                "eventType": "Microsoft.EventGrid.SubscriptionValidationEvent",
                "data": {"validationCode": "validation-code-123"},
            }
        ]
        resp = client.post("/api/webhooks/acs", json=payload)
        assert resp.status_code == 200
        assert resp.json()["validationResponse"] == "validation-code-123"

    def test_call_completed_event_handled(self, client, monkeypatch, db_session):
        """CallCompleted event triggers log_call_activity."""
        from app.config import settings

        monkeypatch.setattr(settings, "acs_connection_string", "Endpoint=sb://test;")
        mock_call_data = {
            "direction": "inbound",
            "to_phone": "+15551234567",
            "duration_seconds": 120,
            "call_connection_id": "conn-123",
        }
        payload = [{"type": "Microsoft.Communication.CallCompleted", "data": {"foo": "bar"}}]
        with patch("app.services.acs_service.handle_call_completed", return_value=mock_call_data):
            with patch("app.services.activity_service.log_call_activity", return_value=MagicMock(id=1)):
                resp = client.post("/api/webhooks/acs", json=payload)
        assert resp.status_code == 200
        assert resp.json()["status"] == "accepted"

    def test_call_disconnected_event_handled(self, client, monkeypatch):
        """CallDisconnected event is also processed."""
        from app.config import settings

        monkeypatch.setattr(settings, "acs_connection_string", "Endpoint=sb://test;")
        mock_call_data = {
            "direction": "outbound",
            "to_phone": "+15559876543",
            "duration_seconds": 60,
            "call_connection_id": "conn-456",
        }
        payload = [{"type": "Microsoft.Communication.CallDisconnected", "data": {}}]
        with patch("app.services.acs_service.handle_call_completed", return_value=mock_call_data):
            with patch("app.services.activity_service.log_call_activity", return_value=MagicMock(id=2)):
                resp = client.post("/api/webhooks/acs", json=payload)
        assert resp.status_code == 200

    def test_call_event_no_call_data_skipped(self, client, monkeypatch):
        """If handle_call_completed returns None, skip logging."""
        from app.config import settings

        monkeypatch.setattr(settings, "acs_connection_string", "Endpoint=sb://test;")
        payload = [{"type": "Microsoft.Communication.CallCompleted", "data": {}}]
        with patch("app.services.acs_service.handle_call_completed", return_value=None):
            resp = client.post("/api/webhooks/acs", json=payload)
        assert resp.status_code == 200

    def test_non_call_event_accepted(self, client, monkeypatch):
        """Non-call events are accepted without processing."""
        from app.config import settings

        monkeypatch.setattr(settings, "acs_connection_string", "Endpoint=sb://test;")
        payload = [{"type": "SomethingElse", "data": {}}]
        resp = client.post("/api/webhooks/acs", json=payload)
        assert resp.status_code == 200


# ── Initiate Call ────────────────────────────────────────────────────


class TestInitiateCall:
    def test_acs_not_configured_returns_503(self, client, monkeypatch):
        """Returns 503 when ACS not configured."""
        from app.config import settings

        monkeypatch.setattr(settings, "acs_connection_string", None)
        resp = client.post("/api/calls/initiate", json={"to_phone": "+15551234567"})
        assert resp.status_code == 503

    def test_missing_to_phone_returns_422(self, client, monkeypatch):
        """Returns 422 when to_phone is missing."""
        from app.config import settings

        monkeypatch.setattr(settings, "acs_connection_string", "Endpoint=sb://test;")
        resp = client.post("/api/calls/initiate", json={})
        assert resp.status_code == 422

    def test_invalid_json_returns_400(self, client, monkeypatch):
        """Malformed JSON returns 400."""
        from app.config import settings

        monkeypatch.setattr(settings, "acs_connection_string", "Endpoint=sb://test;")
        resp = client.post(
            "/api/calls/initiate",
            content=b"bad-json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400

    def test_initiate_call_success(self, client, monkeypatch):
        """Successful call initiation returns result."""
        from app.config import settings

        monkeypatch.setattr(settings, "acs_connection_string", "Endpoint=sb://test;")
        monkeypatch.setattr(settings, "acs_from_phone", "+15550000000")
        monkeypatch.setattr(settings, "acs_callback_url", "https://example.com/acs")
        mock_result = {"call_id": "abc123", "status": "initiated"}
        with patch("app.services.acs_service.initiate_call", new=AsyncMock(return_value=mock_result)):
            resp = client.post("/api/calls/initiate", json={"to_phone": "+15551234567"})
        assert resp.status_code == 200
        assert resp.json()["call_id"] == "abc123"

    def test_initiate_call_failure_returns_500(self, client, monkeypatch):
        """Failed call initiation returns 500."""
        from app.config import settings

        monkeypatch.setattr(settings, "acs_connection_string", "Endpoint=sb://test;")
        monkeypatch.setattr(settings, "acs_from_phone", "+15550000000")
        monkeypatch.setattr(settings, "acs_callback_url", None)
        with patch("app.services.acs_service.initiate_call", new=AsyncMock(return_value=None)):
            resp = client.post("/api/calls/initiate", json={"to_phone": "+15551234567"})
        assert resp.status_code == 500


# ── Company Activities ───────────────────────────────────────────────


class TestCompanyActivities:
    def test_get_company_activities_returns_list(self, client, test_company, db_session, test_user):
        """GET /api/companies/{id}/activities returns activity list."""
        activity = ActivityLog(
            user_id=test_user.id,
            activity_type="email_sent",
            channel="email",
            company_id=test_company.id,
            contact_email="test@example.com",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(activity)
        db_session.commit()

        resp = client.get(f"/api/companies/{test_company.id}/activities")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) >= 1

    def test_log_company_call_404_company(self, client):
        """POST call log for missing company returns 404."""
        resp = client.post(
            "/api/companies/99999/activities/call",
            json={"phone": "+15551234567", "direction": "outbound"},
        )
        assert resp.status_code == 404

    def test_log_company_call_success(self, client, test_company, db_session):
        """POST call log creates an activity record."""
        with patch(
            "app.services.activity_service.log_company_call",
            return_value=MagicMock(id=42),
        ):
            resp = client.post(
                f"/api/companies/{test_company.id}/activities/call",
                json={"phone": "+15551234567", "direction": "outbound", "duration_seconds": 90},
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "logged"
        assert resp.json()["activity_id"] == 42

    def test_log_company_note_404_company(self, client):
        """POST note for missing company returns 404."""
        resp = client.post(
            "/api/companies/99999/activities/note",
            json={"notes": "Test note"},
        )
        assert resp.status_code == 404

    def test_log_company_note_success(self, client, test_company, db_session):
        """POST note creates an activity record."""
        with patch(
            "app.services.activity_service.log_company_note",
            return_value=MagicMock(id=77),
        ):
            resp = client.post(
                f"/api/companies/{test_company.id}/activities/note",
                json={"notes": "Important note", "contact_name": "Jane"},
            )
        assert resp.status_code == 200
        assert resp.json()["activity_id"] == 77


# ── Vendor Activities ────────────────────────────────────────────────


class TestVendorActivities:
    def test_get_vendor_activities_returns_list(self, client, test_vendor_card, db_session, test_user):
        """GET /api/vendors/{id}/activities returns activity list."""
        activity = ActivityLog(
            user_id=test_user.id,
            activity_type="phone_call",
            channel="phone",
            vendor_card_id=test_vendor_card.id,
            contact_phone="+15551234567",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(activity)
        db_session.commit()

        resp = client.get(f"/api/vendors/{test_vendor_card.id}/activities")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_log_vendor_call_404_vendor(self, client):
        """POST call log for missing vendor returns 404."""
        resp = client.post(
            "/api/vendors/99999/activities/call",
            json={"phone": "+15551234567", "direction": "outbound"},
        )
        assert resp.status_code == 404

    def test_log_vendor_call_success(self, client, test_vendor_card):
        """POST vendor call log returns logged status."""
        with patch(
            "app.services.activity_service.log_vendor_call",
            return_value=MagicMock(id=55),
        ):
            resp = client.post(
                f"/api/vendors/{test_vendor_card.id}/activities/call",
                json={"phone": "+15551234567", "direction": "outbound", "duration_seconds": 120},
            )
        assert resp.status_code == 200
        assert resp.json()["activity_id"] == 55

    def test_log_vendor_note_404_vendor(self, client):
        """POST note for missing vendor returns 404."""
        resp = client.post(
            "/api/vendors/99999/activities/note",
            json={"notes": "Test note"},
        )
        assert resp.status_code == 404

    def test_log_vendor_note_success(self, client, test_vendor_card):
        """POST vendor note returns logged status."""
        with patch(
            "app.services.activity_service.log_vendor_note",
            return_value=MagicMock(id=66),
        ):
            resp = client.post(
                f"/api/vendors/{test_vendor_card.id}/activities/note",
                json={"notes": "Vendor note"},
            )
        assert resp.status_code == 200
        assert resp.json()["activity_id"] == 66


# ── User Activities ──────────────────────────────────────────────────


class TestUserActivities:
    def test_get_user_activities_returns_list(self, client, test_user, db_session):
        """GET /api/users/{id}/activities returns list."""
        activity = ActivityLog(
            user_id=test_user.id,
            activity_type="email_sent",
            channel="email",
            contact_email="test@example.com",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(activity)
        db_session.commit()

        resp = client.get(f"/api/users/{test_user.id}/activities")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)


# ── Email Click ──────────────────────────────────────────────────────


class TestEmailClick:
    def test_log_email_click_empty_email_skipped(self, client):
        """Empty email returns skipped status."""
        resp = client.post("/api/activities/email", json={"email": "   "})
        assert resp.status_code == 200
        assert resp.json()["status"] == "skipped"

    def test_log_email_click_no_match(self, client):
        """Email that doesn't match any contact returns no_match."""
        with patch("app.services.activity_service.log_email_activity", return_value=None):
            resp = client.post(
                "/api/activities/email",
                json={"email": "unknown@example.com", "contact_name": "Unknown"},
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "no_match"

    def test_log_email_click_success(self, client):
        """Matched email returns logged status."""
        with patch("app.services.activity_service.log_email_activity", return_value=MagicMock(id=99)):
            resp = client.post(
                "/api/activities/email",
                json={"email": "known@example.com"},
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "logged"
        assert resp.json()["activity_id"] == 99


# ── Phone Call ───────────────────────────────────────────────────────


class TestPhoneCall:
    def test_log_phone_call_no_match(self, client):
        """Unmatched phone number returns no_match."""
        with patch("app.services.activity_service.log_call_activity", return_value=None):
            resp = client.post(
                "/api/activities/call",
                json={"phone": "+15559999999", "direction": "outbound"},
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "no_match"

    def test_log_phone_call_success(self, client):
        """Matched phone call returns logged status."""
        with patch("app.services.activity_service.log_call_activity", return_value=MagicMock(id=88)):
            resp = client.post(
                "/api/activities/call",
                json={"phone": "+15551234567", "direction": "inbound", "duration_seconds": 60},
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "logged"


# ── Unmatched Activity Queue ─────────────────────────────────────────


class TestUnmatchedActivities:
    def test_list_unmatched_activities(self, client, db_session):
        """GET /api/activities/unmatched returns paginated list."""
        resp = client.get("/api/activities/unmatched")
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data
        assert "total" in data

    def test_list_unmatched_with_pagination(self, client):
        """Accepts limit/offset query params."""
        resp = client.get("/api/activities/unmatched?limit=10&offset=0")
        assert resp.status_code == 200

    def test_attribute_activity_company_not_found(self, client):
        """Attribute to non-existent company returns 404."""
        resp = client.post(
            "/api/activities/99999/attribute",
            json={"entity_type": "company", "entity_id": 99999},
        )
        assert resp.status_code == 404

    def test_attribute_activity_vendor_not_found(self, client):
        """Attribute to non-existent vendor returns 404."""
        resp = client.post(
            "/api/activities/99999/attribute",
            json={"entity_type": "vendor", "entity_id": 99999},
        )
        assert resp.status_code == 404

    def test_attribute_activity_activity_not_found(self, client, test_company):
        """Attribute to existing company but activity not found returns 404."""
        with patch("app.services.activity_service.attribute_activity", return_value=None):
            resp = client.post(
                "/api/activities/99999/attribute",
                json={"entity_type": "company", "entity_id": test_company.id},
            )
        assert resp.status_code == 404

    def test_attribute_activity_success(self, client, test_company, db_session, test_user):
        """Successful attribution returns attributed status."""
        activity = ActivityLog(
            user_id=test_user.id,
            activity_type="email_sent",
            channel="email",
            contact_email="test@test.com",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(activity)
        db_session.commit()

        mock_activity = MagicMock()
        mock_activity.id = activity.id
        mock_activity.user_id = test_user.id
        mock_activity.user = test_user
        mock_activity.activity_type = "email_sent"
        mock_activity.channel = "email"
        mock_activity.company_id = test_company.id
        mock_activity.vendor_card_id = None
        mock_activity.contact_email = "test@test.com"
        mock_activity.contact_phone = None
        mock_activity.contact_name = None
        mock_activity.subject = None
        mock_activity.duration_seconds = None
        mock_activity.created_at = datetime.now(timezone.utc)
        mock_activity.dismissed_at = None

        with patch("app.services.activity_service.attribute_activity", return_value=mock_activity):
            resp = client.post(
                f"/api/activities/{activity.id}/attribute",
                json={"entity_type": "company", "entity_id": test_company.id},
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "attributed"

    def test_dismiss_activity_not_found(self, client):
        """Dismiss non-existent activity returns 404."""
        with patch("app.services.activity_service.dismiss_activity", return_value=None):
            resp = client.post("/api/activities/99999/dismiss")
        assert resp.status_code == 404

    def test_dismiss_activity_success(self, client, db_session, test_user):
        """Successful dismiss returns dismissed status."""
        activity = ActivityLog(
            user_id=test_user.id,
            activity_type="email_sent",
            channel="email",
            contact_email="test@test.com",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(activity)
        db_session.commit()

        mock_result = MagicMock()
        mock_result.id = activity.id
        with patch("app.services.activity_service.dismiss_activity", return_value=mock_result):
            resp = client.post(f"/api/activities/{activity.id}/dismiss")
        assert resp.status_code == 200
        assert resp.json()["status"] == "dismissed"


# ── Activity Status ──────────────────────────────────────────────────


class TestActivityStatus:
    def test_vendor_activity_status_not_found(self, client):
        """GET vendor activity status for non-existent vendor returns 404."""
        resp = client.get("/api/vendors/99999/activity-status")
        assert resp.status_code == 404

    def test_vendor_activity_status_no_activity(self, client, test_vendor_card, monkeypatch):
        """Vendor with no activity returns no_activity status."""
        from app.config import settings

        with patch("app.services.activity_service.days_since_last_vendor_activity", return_value=None):
            resp = client.get(f"/api/vendors/{test_vendor_card.id}/activity-status")
        assert resp.status_code == 200
        assert resp.json()["status"] == "no_activity"

    def test_vendor_activity_status_green(self, client, test_vendor_card, monkeypatch):
        """Vendor with recent activity returns green."""
        from app.config import settings

        monkeypatch.setattr(settings, "customer_warning_days", 30)
        with patch("app.services.activity_service.days_since_last_vendor_activity", return_value=5):
            resp = client.get(f"/api/vendors/{test_vendor_card.id}/activity-status")
        assert resp.status_code == 200
        assert resp.json()["status"] == "green"

    def test_vendor_activity_status_yellow(self, client, test_vendor_card, monkeypatch):
        """Vendor with moderate inactivity returns yellow."""
        from app.config import settings

        monkeypatch.setattr(settings, "customer_warning_days", 10)
        monkeypatch.setattr(settings, "vendor_protection_warn_days", 60)
        with patch("app.services.activity_service.days_since_last_vendor_activity", return_value=20):
            resp = client.get(f"/api/vendors/{test_vendor_card.id}/activity-status")
        assert resp.status_code == 200
        assert resp.json()["status"] == "yellow"

    def test_vendor_activity_status_red(self, client, test_vendor_card, monkeypatch):
        """Vendor with high inactivity returns red."""
        from app.config import settings

        monkeypatch.setattr(settings, "customer_warning_days", 10)
        monkeypatch.setattr(settings, "vendor_protection_warn_days", 30)
        with patch("app.services.activity_service.days_since_last_vendor_activity", return_value=90):
            resp = client.get(f"/api/vendors/{test_vendor_card.id}/activity-status")
        assert resp.status_code == 200
        assert resp.json()["status"] == "red"

    def test_company_activity_status_not_found(self, client):
        """GET company activity status for non-existent company returns 404."""
        resp = client.get("/api/companies/99999/activity-status")
        assert resp.status_code == 404

    def test_company_activity_status_no_activity(self, client, test_company):
        """Company with no activity returns no_activity status."""
        with patch("app.services.activity_service.days_since_last_activity", return_value=None):
            resp = client.get(f"/api/companies/{test_company.id}/activity-status")
        assert resp.status_code == 200
        assert resp.json()["status"] == "no_activity"

    def test_company_activity_status_green(self, client, test_company, monkeypatch):
        """Company with recent activity returns green."""
        from app.config import settings

        monkeypatch.setattr(settings, "customer_warning_days", 30)
        with patch("app.services.activity_service.days_since_last_activity", return_value=5):
            resp = client.get(f"/api/companies/{test_company.id}/activity-status")
        assert resp.status_code == 200
        assert resp.json()["status"] == "green"

    def test_company_activity_status_yellow(self, client, test_company, monkeypatch, db_session):
        """Non-strategic company with moderate inactivity returns yellow."""
        from app.config import settings

        monkeypatch.setattr(settings, "customer_warning_days", 10)
        monkeypatch.setattr(settings, "customer_inactivity_days", 60)
        test_company.is_strategic = False
        db_session.commit()
        with patch("app.services.activity_service.days_since_last_activity", return_value=30):
            resp = client.get(f"/api/companies/{test_company.id}/activity-status")
        assert resp.status_code == 200
        assert resp.json()["status"] == "yellow"

    def test_company_activity_status_red(self, client, test_company, monkeypatch, db_session):
        """Company with high inactivity returns red."""
        from app.config import settings

        monkeypatch.setattr(settings, "customer_warning_days", 10)
        monkeypatch.setattr(settings, "customer_inactivity_days", 45)
        test_company.is_strategic = False
        db_session.commit()
        with patch("app.services.activity_service.days_since_last_activity", return_value=90):
            resp = client.get(f"/api/companies/{test_company.id}/activity-status")
        assert resp.status_code == 200
        assert resp.json()["status"] == "red"

    def test_company_activity_status_strategic(self, client, test_company, monkeypatch, db_session):
        """Strategic company uses strategic_inactivity_days threshold."""
        from app.config import settings

        monkeypatch.setattr(settings, "customer_warning_days", 10)
        monkeypatch.setattr(settings, "strategic_inactivity_days", 90)
        test_company.is_strategic = True
        db_session.commit()
        with patch("app.services.activity_service.days_since_last_activity", return_value=50):
            resp = client.get(f"/api/companies/{test_company.id}/activity-status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_strategic"] is True
        assert data["inactivity_limit"] == 90
