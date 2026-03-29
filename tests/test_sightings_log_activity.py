"""test_sightings_log_activity.py — Tests for inline log-note quick action.

Tests the POST /v2/partials/sightings/{requirement_id}/log-activity endpoint.

Called by: pytest
Depends on: conftest fixtures (client, db_session, test_user, test_requisition)
"""

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import ActivityLog, Requirement, Requisition, User

# ── Helpers ──────────────────────────────────────────────────────────


def _get_requirement_id(db_session: Session, test_requisition: Requisition) -> int:
    """Return the first requirement ID from the test requisition."""
    req = db_session.query(Requirement).filter(Requirement.requisition_id == test_requisition.id).first()
    assert req is not None, "test_requisition fixture should include a requirement"
    return req.id


# ── Tests ────────────────────────────────────────────────────────────


class TestLogActivity:
    """POST /v2/partials/sightings/{requirement_id}/log-activity."""

    def test_log_note_creates_activity(
        self, client: TestClient, db_session: Session, test_requisition: Requisition, test_user: User
    ):
        """Submitting a note creates an ActivityLog record and returns HTML."""
        req_id = _get_requirement_id(db_session, test_requisition)

        resp = client.post(
            f"/v2/partials/sightings/{req_id}/log-activity",
            data={"notes": "Called vendor, left voicemail", "channel": "note"},
        )
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")

        # Verify DB record
        record = db_session.query(ActivityLog).filter(ActivityLog.requirement_id == req_id).first()
        assert record is not None
        assert record.notes == "Called vendor, left voicemail"
        assert record.activity_type == "note"
        assert record.channel == "manual"
        assert record.user_id == test_user.id
        assert record.requisition_id == test_requisition.id

    def test_empty_notes_returns_400(self, client: TestClient, db_session: Session, test_requisition: Requisition):
        """Empty or whitespace-only notes should be rejected with 400."""
        req_id = _get_requirement_id(db_session, test_requisition)

        resp = client.post(
            f"/v2/partials/sightings/{req_id}/log-activity",
            data={"notes": "   ", "channel": "note"},
        )
        assert resp.status_code == 400

    def test_missing_notes_returns_422(self, client: TestClient, db_session: Session, test_requisition: Requisition):
        """Missing notes field should return 422 (FastAPI validation)."""
        req_id = _get_requirement_id(db_session, test_requisition)

        resp = client.post(
            f"/v2/partials/sightings/{req_id}/log-activity",
            data={"channel": "note"},
        )
        assert resp.status_code == 422

    def test_channel_call(self, client: TestClient, db_session: Session, test_requisition: Requisition):
        """Channel 'call' creates call_outbound activity type."""
        req_id = _get_requirement_id(db_session, test_requisition)

        resp = client.post(
            f"/v2/partials/sightings/{req_id}/log-activity",
            data={"notes": "Spoke with rep about pricing", "channel": "call"},
        )
        assert resp.status_code == 200

        record = db_session.query(ActivityLog).filter(ActivityLog.requirement_id == req_id).first()
        assert record is not None
        assert record.activity_type == "call_outbound"
        assert record.channel == "call"

    def test_channel_email(self, client: TestClient, db_session: Session, test_requisition: Requisition):
        """Channel 'email' creates email_sent activity type."""
        req_id = _get_requirement_id(db_session, test_requisition)

        resp = client.post(
            f"/v2/partials/sightings/{req_id}/log-activity",
            data={"notes": "Sent follow-up email", "channel": "email"},
        )
        assert resp.status_code == 200

        record = db_session.query(ActivityLog).filter(ActivityLog.requirement_id == req_id).first()
        assert record is not None
        assert record.activity_type == "email_sent"
        assert record.channel == "email"

    def test_invalid_channel_returns_400(self, client: TestClient, db_session: Session, test_requisition: Requisition):
        """Invalid channel value should be rejected."""
        req_id = _get_requirement_id(db_session, test_requisition)

        resp = client.post(
            f"/v2/partials/sightings/{req_id}/log-activity",
            data={"notes": "Some note", "channel": "fax"},
        )
        assert resp.status_code == 400

    def test_vendor_name_optional(self, client: TestClient, db_session: Session, test_requisition: Requisition):
        """Vendor name is stored in contact_name when provided."""
        req_id = _get_requirement_id(db_session, test_requisition)

        resp = client.post(
            f"/v2/partials/sightings/{req_id}/log-activity",
            data={
                "notes": "Got quote from Arrow",
                "channel": "note",
                "vendor_name": "Arrow Electronics",
            },
        )
        assert resp.status_code == 200

        record = db_session.query(ActivityLog).filter(ActivityLog.requirement_id == req_id).first()
        assert record is not None
        assert record.contact_name == "Arrow Electronics"

    def test_vendor_name_empty_stored_as_none(
        self, client: TestClient, db_session: Session, test_requisition: Requisition
    ):
        """Empty vendor_name should be stored as None, not empty string."""
        req_id = _get_requirement_id(db_session, test_requisition)

        resp = client.post(
            f"/v2/partials/sightings/{req_id}/log-activity",
            data={"notes": "General note", "channel": "note", "vendor_name": ""},
        )
        assert resp.status_code == 200

        record = db_session.query(ActivityLog).filter(ActivityLog.requirement_id == req_id).first()
        assert record is not None
        assert record.contact_name is None

    def test_nonexistent_requirement_returns_404(
        self, client: TestClient, db_session: Session, test_requisition: Requisition
    ):
        """Posting to a non-existent requirement should return 404."""
        resp = client.post(
            "/v2/partials/sightings/999999/log-activity",
            data={"notes": "Some note", "channel": "note"},
        )
        assert resp.status_code == 404

    def test_response_contains_new_activity(
        self, client: TestClient, db_session: Session, test_requisition: Requisition
    ):
        """The returned HTML should contain the newly logged note text."""
        req_id = _get_requirement_id(db_session, test_requisition)

        resp = client.post(
            f"/v2/partials/sightings/{req_id}/log-activity",
            data={"notes": "Unique test note XYZ123", "channel": "note"},
        )
        assert resp.status_code == 200
        assert "Unique test note XYZ123" in resp.text
