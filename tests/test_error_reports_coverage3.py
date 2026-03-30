"""test_error_reports_coverage3.py — Coverage boost for error_reports router.

Targets lines 182-193 (invalid JSON body), 197-255 (form-encoded path + success),
277 (screenshot 404), 284-287 (screenshot_b64 fallback + no-screenshot 404),
384 (update_ticket with non-resolved status transition).

Called by: pytest
Depends on: app/routers/error_reports.py, conftest.py
"""

import os

os.environ["TESTING"] = "1"

import base64
from datetime import datetime, timezone
from unittest.mock import patch

from app.models.trouble_ticket import TroubleTicket


# ── Invalid JSON body ─────────────────────────────────────────────────────


class TestInvalidJsonBody:
    def test_submit_invalid_json_body_returns_422(self, client):
        """Sending a non-JSON body with application/json content-type returns 422."""
        resp = client.post(
            "/api/trouble-tickets/submit",
            content=b"this is not json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 422
        assert "Invalid request" in resp.text or "invalid" in resp.text.lower()


# ── Form-encoded path coverage ────────────────────────────────────────────


class TestFormEncodedPath:
    def test_form_submit_success(self, client):
        """Form-encoded POST with a valid message returns 200 with ticket number."""
        resp = client.post(
            "/api/trouble-tickets/submit",
            data={"message": "The search results are broken", "current_url": "/v2/sourcing"},
        )
        assert resp.status_code == 200
        assert "Report submitted" in resp.text

    def test_form_submit_empty_message_returns_422(self, client):
        """Form-encoded POST with empty message is rejected."""
        resp = client.post(
            "/api/trouble-tickets/submit",
            data={"message": ""},
        )
        assert resp.status_code == 422

    def test_form_submit_whitespace_only_returns_422(self, client):
        """Form-encoded POST with whitespace-only message is rejected."""
        resp = client.post(
            "/api/trouble-tickets/submit",
            data={"message": "   "},
        )
        assert resp.status_code == 422

    def test_form_submit_message_too_long_returns_422(self, client):
        """Form-encoded POST with message exceeding MAX_MESSAGE_LEN is rejected."""
        resp = client.post(
            "/api/trouble-tickets/submit",
            data={"message": "X" * 5001},
        )
        assert resp.status_code == 422
        assert "too long" in resp.text.lower() or "max" in resp.text.lower()

    def test_form_submit_without_current_url(self, client):
        """Form-encoded POST without current_url still succeeds."""
        resp = client.post(
            "/api/trouble-tickets/submit",
            data={"message": "Bug without URL context"},
        )
        assert resp.status_code == 200
        assert "Report submitted" in resp.text


# ── Screenshot endpoint coverage ─────────────────────────────────────────


class TestScreenshotEndpointCoverage:
    def test_screenshot_ticket_not_found_returns_404(self, client):
        """GET /api/trouble-tickets/{id}/screenshot returns 404 when ticket missing."""
        resp = client.get("/api/trouble-tickets/99998/screenshot")
        assert resp.status_code == 404

    def test_screenshot_b64_fallback_serves_image(self, client, db_session, test_user):
        """Ticket with screenshot_b64 but no screenshot_path serves the PNG bytes."""
        png_bytes = b"\x89PNG\r\n\x1a\n"
        b64_data = base64.b64encode(png_bytes).decode()

        ticket = TroubleTicket(
            ticket_number="TT-B64-001",
            submitted_by=test_user.id,
            title="B64 screenshot test",
            description="Testing b64 fallback",
            status="submitted",
            source="report_button",
            risk_tier="low",
            category="other",
            screenshot_b64=b64_data,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(ticket)
        db_session.commit()
        db_session.refresh(ticket)

        resp = client.get(f"/api/trouble-tickets/{ticket.id}/screenshot")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/png"

    def test_screenshot_no_image_at_all_returns_404(self, client, db_session, test_user):
        """Ticket with neither screenshot_path nor screenshot_b64 returns 404."""
        ticket = TroubleTicket(
            ticket_number="TT-NOPIC-001",
            submitted_by=test_user.id,
            title="No screenshot",
            description="No image attached",
            status="submitted",
            source="report_button",
            risk_tier="low",
            category="other",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(ticket)
        db_session.commit()
        db_session.refresh(ticket)

        resp = client.get(f"/api/trouble-tickets/{ticket.id}/screenshot")
        assert resp.status_code == 404


# ── update_ticket with non-resolved status ───────────────────────────────


class TestUpdateTicketStatusVariants:
    def test_update_status_to_in_progress(self, client, db_session, test_user):
        """PATCH with status=in_progress does NOT set resolved_at."""
        ticket = TroubleTicket(
            ticket_number="TT-INPROG-001",
            submitted_by=test_user.id,
            title="In progress test",
            description="Testing in_progress status",
            status="submitted",
            source="report_button",
            risk_tier="low",
            category="other",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(ticket)
        db_session.commit()
        db_session.refresh(ticket)

        resp = client.patch(
            f"/api/trouble-tickets/{ticket.id}",
            json={"status": "in_progress"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "in_progress"

        db_session.refresh(ticket)
        # resolved_at should NOT be set for non-resolved transitions
        assert ticket.resolved_at is None

    def test_update_ticket_status_only_no_notes(self, client, db_session, test_user):
        """PATCH with only status field (no resolution_notes) is accepted."""
        ticket = TroubleTicket(
            ticket_number="TT-STATONLY-001",
            submitted_by=test_user.id,
            title="Status only test",
            description="Testing status-only update",
            status="submitted",
            source="report_button",
            risk_tier="low",
            category="other",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(ticket)
        db_session.commit()
        db_session.refresh(ticket)

        resp = client.patch(
            f"/api/trouble-tickets/{ticket.id}",
            json={"status": "resolved"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "resolved"

    def test_update_trouble_ticket_path_also_works(self, client, db_session, test_user):
        """PATCH /api/trouble-tickets/{id} mirrors /api/error-reports/{id}."""
        ticket = TroubleTicket(
            ticket_number="TT-TTPATH-001",
            submitted_by=test_user.id,
            title="Trouble ticket path test",
            description="Testing trouble ticket update path",
            status="submitted",
            source="report_button",
            risk_tier="low",
            category="other",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(ticket)
        db_session.commit()
        db_session.refresh(ticket)

        resp = client.patch(
            f"/api/trouble-tickets/{ticket.id}",
            json={"resolution_notes": "Fixed by hotfix"},
        )
        assert resp.status_code == 200


# ── Analyze tickets: ClaudeUnavailableError path ─────────────────────────


class TestAnalyzeClaudeUnavailablePath:
    def test_analyze_tickets_claude_unavailable_returns_fallback(
        self, client, db_session, test_user
    ):
        """ClaudeUnavailableError during analyze returns amber fallback HTML."""
        from unittest.mock import AsyncMock

        ticket = TroubleTicket(
            ticket_number="TT-UNAVAIL-001",
            submitted_by=test_user.id,
            title="Unavailable test",
            description="Testing ClaudeUnavailableError path",
            status="submitted",
            source="report_button",
            risk_tier="low",
            category="other",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(ticket)
        db_session.commit()

        from app.utils.claude_errors import ClaudeUnavailableError

        with patch(
            "app.utils.claude_client.claude_structured",
            new_callable=AsyncMock,
            side_effect=ClaudeUnavailableError("Service down"),
        ):
            resp = client.post("/api/trouble-tickets/analyze")

        assert resp.status_code == 200
        assert "try again" in resp.text.lower() or "no results" in resp.text.lower()

    def test_analyze_returns_no_groups_key_in_result(self, client, db_session, test_user):
        """When claude_structured returns dict without 'groups', fallback HTML is shown."""
        from unittest.mock import AsyncMock

        ticket = TroubleTicket(
            ticket_number="TT-NOGROUPS-001",
            submitted_by=test_user.id,
            title="No groups test",
            description="Testing missing groups key",
            status="submitted",
            source="report_button",
            risk_tier="low",
            category="other",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(ticket)
        db_session.commit()

        with patch(
            "app.utils.claude_client.claude_structured",
            new_callable=AsyncMock,
            return_value={"unexpected_key": []},
        ):
            resp = client.post("/api/trouble-tickets/analyze")

        assert resp.status_code == 200
        assert "no results" in resp.text.lower() or "try again" in resp.text.lower()
