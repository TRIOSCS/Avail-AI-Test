"""test_sightings_log_activity.py — Tests for inline log-note quick action.

Tests the POST /v2/partials/sightings/{requirement_id}/log-activity endpoint.

Called by: pytest
Depends on: conftest fixtures (client, db_session, test_user, test_requisition)
"""

import pytest
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

    @pytest.mark.parametrize(
        ("channel", "notes", "expected"),
        [
            pytest.param(
                "call",
                "Spoke with rep about pricing",
                {"activity_type": "call_logged", "direction": "outbound", "channel": "call"},
                id="call",
            ),
            pytest.param(
                "email",
                "Sent follow-up email",
                {"activity_type": "email_sent", "channel": "email"},
                id="email",
            ),
        ],
    )
    def test_channel_maps_to_activity(
        self,
        client: TestClient,
        db_session: Session,
        test_requisition: Requisition,
        channel: str,
        notes: str,
        expected: dict,
    ):
        """Each channel maps to its canonical activity type / direction."""
        req_id = _get_requirement_id(db_session, test_requisition)

        resp = client.post(
            f"/v2/partials/sightings/{req_id}/log-activity",
            data={"notes": notes, "channel": channel},
        )
        assert resp.status_code == 200

        record = db_session.query(ActivityLog).filter(ActivityLog.requirement_id == req_id).first()
        assert record is not None
        for field, value in expected.items():
            assert getattr(record, field) == value

    @pytest.mark.parametrize(
        ("data", "case"),
        [
            ({"notes": "   ", "channel": "note"}, "whitespace-only notes"),
            ({"notes": "Some note", "channel": "fax"}, "invalid channel"),
        ],
        ids=["empty_notes", "invalid_channel"],
    )
    def test_invalid_input_returns_400(
        self, client: TestClient, db_session: Session, test_requisition: Requisition, data: dict, case: str
    ):
        """Whitespace-only notes and unknown channels are rejected with 400."""
        req_id = _get_requirement_id(db_session, test_requisition)

        resp = client.post(f"/v2/partials/sightings/{req_id}/log-activity", data=data)
        assert resp.status_code == 400, case

    def test_missing_notes_returns_422(self, client: TestClient, db_session: Session, test_requisition: Requisition):
        """Missing notes field should return 422 (FastAPI validation)."""
        req_id = _get_requirement_id(db_session, test_requisition)

        resp = client.post(
            f"/v2/partials/sightings/{req_id}/log-activity",
            data={"channel": "note"},
        )
        assert resp.status_code == 422

    @pytest.mark.parametrize(
        ("vendor_name", "notes", "expected_contact_name"),
        [
            pytest.param("Arrow Electronics", "Got quote from Arrow", "Arrow Electronics", id="provided"),
            pytest.param("", "General note", None, id="empty_stored_as_none"),
        ],
    )
    def test_vendor_name_stored_in_contact_name(
        self,
        client: TestClient,
        db_session: Session,
        test_requisition: Requisition,
        vendor_name: str,
        notes: str,
        expected_contact_name: str | None,
    ):
        """Vendor name maps to contact_name; an empty value is stored as None."""
        req_id = _get_requirement_id(db_session, test_requisition)

        resp = client.post(
            f"/v2/partials/sightings/{req_id}/log-activity",
            data={"notes": notes, "channel": "note", "vendor_name": vendor_name},
        )
        assert resp.status_code == 200

        record = db_session.query(ActivityLog).filter(ActivityLog.requirement_id == req_id).first()
        assert record is not None
        assert record.contact_name == expected_contact_name

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
