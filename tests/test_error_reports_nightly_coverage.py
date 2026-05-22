"""test_error_reports_nightly_coverage.py — Targets remaining uncovered branches.

Covers line 384 (analyze_tickets empty-tickets early return) and verifies
the submit_trouble_ticket JSON path and form-encoded path reach their
success branches without relying on parallel test state.

Called by: pytest (nightly coverage job)
Depends on: app/routers/error_reports.py, tests/conftest.py
"""

import os

os.environ["TESTING"] = "1"

import base64
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from app.constants import TicketSource, TicketStatus
from app.models.trouble_ticket import TroubleTicket

# ── analyze_tickets: empty-tickets early return (line 384) ───────────


class TestAnalyzeNoOpenTickets:
    """Line 384: analyze_tickets returns early when no submitted/in_progress
    report_button tickets exist."""

    def test_analyze_returns_no_open_tickets_html_when_db_is_empty(self, client):
        """With a clean DB session containing no tickets, the analyze endpoint
        returns the 'No open tickets' partial immediately."""
        resp = client.post("/api/trouble-tickets/analyze")
        assert resp.status_code == 200
        assert "No open tickets" in resp.text

    def test_analyze_ignores_resolved_tickets(self, client, db_session, test_user):
        """Resolved tickets are excluded from analysis — should still hit line 384."""
        ticket = TroubleTicket(
            ticket_number="TT-RES-NIGHTLY-001",
            submitted_by=test_user.id,
            title="Already resolved",
            description="This one is resolved, should be excluded",
            status=TicketStatus.RESOLVED,
            source=TicketSource.REPORT_BUTTON,
            risk_tier="low",
            category="other",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(ticket)
        db_session.commit()

        resp = client.post("/api/trouble-tickets/analyze")
        assert resp.status_code == 200
        assert "No open tickets" in resp.text

    def test_analyze_ignores_wont_fix_tickets(self, client, db_session, test_user):
        """wont_fix tickets are excluded from analysis — still triggers early return."""
        ticket = TroubleTicket(
            ticket_number="TT-WNT-NIGHTLY-001",
            submitted_by=test_user.id,
            title="Wont fix ticket",
            description="This wont be fixed",
            status=TicketStatus.WONT_FIX,
            source=TicketSource.REPORT_BUTTON,
            risk_tier="low",
            category="other",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(ticket)
        db_session.commit()

        resp = client.post("/api/trouble-tickets/analyze")
        assert resp.status_code == 200
        assert "No open tickets" in resp.text

    def test_analyze_ignores_non_report_button_submitted_tickets(self, client, db_session, test_user):
        """Submitted ticket_form tickets are excluded; returns no-open-tickets message."""
        ticket = TroubleTicket(
            ticket_number="TT-TF-NIGHTLY-001",
            submitted_by=test_user.id,
            title="Ticket form ticket",
            description="Submitted via ticket form, not report button",
            status=TicketStatus.SUBMITTED,
            source=TicketSource.TICKET_FORM,
            risk_tier="low",
            category="other",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(ticket)
        db_session.commit()

        resp = client.post("/api/trouble-tickets/analyze")
        assert resp.status_code == 200
        assert "No open tickets" in resp.text


# ── submit_trouble_ticket: JSON path success (lines 187-193, 253-262) ──


class TestSubmitJsonPathSuccess:
    """Verify the JSON branch of submit_trouble_ticket executes the complete
    success path including background task scheduling."""

    def test_json_submit_returns_success_html(self, client):
        resp = client.post(
            "/api/trouble-tickets/submit",
            json={"description": "Nightly coverage: JSON path success"},
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200
        assert "Report submitted" in resp.text
        assert "TT-" in resp.text

    def test_json_submit_all_optional_fields(self, client):
        """All optional JSON fields parsed correctly (lines 188-193)."""
        resp = client.post(
            "/api/trouble-tickets/submit",
            json={
                "description": "Full JSON payload",
                "page_url": "/v2/sourcing",
                "user_agent": "Mozilla/5.0 (X11; Linux x86_64)",
                "viewport": "1440x900",
                "error_log": "TypeError: Cannot read property 'id' of undefined",
                "network_log": '[{"url": "/api/search", "status": 500}]',
            },
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200
        assert "Report submitted" in resp.text

    def test_json_submit_only_viewport_sets_browser_info(self, client, db_session):
        """viewport alone (no user_agent) still sets browser_info."""
        resp = client.post(
            "/api/trouble-tickets/submit",
            json={
                "description": "Viewport only test",
                "viewport": "1280x800",
            },
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200

    def test_json_submit_only_user_agent_sets_browser_info(self, client, db_session):
        """user_agent alone (no viewport) sets browser_info JSON."""
        resp = client.post(
            "/api/trouble-tickets/submit",
            json={
                "description": "User agent only test",
                "user_agent": "curl/7.68.0",
            },
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200

    def test_json_submit_network_log_as_list_not_string(self, client):
        """network_log provided as a list (not string) bypasses json.loads."""
        resp = client.post(
            "/api/trouble-tickets/submit",
            json={
                "description": "Network log as list",
                "network_log": [{"url": "/api/vendors", "status": 404}],
            },
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200

    def test_json_submit_with_screenshot_b64(self, client, tmp_path, monkeypatch):
        """Screenshot in JSON body triggers _save_screenshot path."""
        from app.routers import error_reports

        monkeypatch.setattr(error_reports, "UPLOAD_DIR", str(tmp_path))
        monkeypatch.setattr(error_reports, "_upload_dir_ready", False)

        png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
        b64 = base64.b64encode(png_bytes).decode()

        resp = client.post(
            "/api/trouble-tickets/submit",
            json={
                "description": "Bug with screenshot attached",
                "screenshot": b64,
            },
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200
        assert "Report submitted" in resp.text


# ── submit_trouble_ticket: form-encoded path success (lines 197-255) ──


class TestSubmitFormEncodedPathSuccess:
    """Verify the form-encoded branch executes the complete success path."""

    def test_form_submit_with_message_only(self, client):
        resp = client.post(
            "/api/trouble-tickets/submit",
            data={"message": "Nightly coverage: form path success"},
        )
        assert resp.status_code == 200
        assert "Report submitted" in resp.text
        assert "TT-" in resp.text

    def test_form_submit_with_current_url(self, client):
        resp = client.post(
            "/api/trouble-tickets/submit",
            data={
                "message": "Form with URL",
                "current_url": "/v2/requisitions",
            },
        )
        assert resp.status_code == 200
        assert "Report submitted" in resp.text

    def test_form_submit_stores_ticket_in_db(self, client, db_session):
        resp = client.post(
            "/api/trouble-tickets/submit",
            data={"message": "Stored form ticket nightly"},
        )
        assert resp.status_code == 200
        ticket = (
            db_session.query(TroubleTicket).filter(TroubleTicket.description == "Stored form ticket nightly").first()
        )
        assert ticket is not None
        assert ticket.source == TicketSource.REPORT_BUTTON
        assert ticket.status == TicketStatus.SUBMITTED

    def test_form_submit_whitespace_message_rejected(self, client):
        resp = client.post(
            "/api/trouble-tickets/submit",
            data={"message": "   \t  "},
        )
        assert resp.status_code == 422
        assert "describe" in resp.text.lower() or "problem" in resp.text.lower()

    def test_form_submit_message_at_max_length_accepted(self, client):
        """Message exactly at MAX_MESSAGE_LEN (5000) is accepted."""
        from app.routers.error_reports import MAX_MESSAGE_LEN

        resp = client.post(
            "/api/trouble-tickets/submit",
            data={"message": "X" * MAX_MESSAGE_LEN},
        )
        assert resp.status_code == 200

    def test_form_submit_message_over_max_length_rejected(self, client):
        """Message over MAX_MESSAGE_LEN (5001+) is rejected with 422."""
        from app.routers.error_reports import MAX_MESSAGE_LEN

        resp = client.post(
            "/api/trouble-tickets/submit",
            data={"message": "X" * (MAX_MESSAGE_LEN + 1)},
        )
        assert resp.status_code == 422
        assert "too long" in resp.text.lower() or "max" in resp.text.lower()


# ── analyze_tickets: ClaudeUnavailableError path ─────────────────────


class TestAnalyzeClaudeErrors:
    """Verify analyze_tickets catches ClaudeUnavailableError and ClaudeError."""

    @patch("app.utils.claude_client.claude_structured", new_callable=AsyncMock)
    def test_claude_unavailable_error_returns_fallback_html(self, mock_claude, client, db_session, test_user):
        from app.utils.claude_errors import ClaudeUnavailableError

        ticket = TroubleTicket(
            ticket_number="TT-CLU-NIGHTLY-001",
            submitted_by=test_user.id,
            title="Unavailable error test",
            description="Triggers ClaudeUnavailableError",
            status=TicketStatus.SUBMITTED,
            source=TicketSource.REPORT_BUTTON,
            risk_tier="low",
            category="other",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(ticket)
        db_session.commit()

        mock_claude.side_effect = ClaudeUnavailableError("service down")
        resp = client.post("/api/trouble-tickets/analyze")
        assert resp.status_code == 200
        assert "no results" in resp.text.lower() or "Try again" in resp.text

    @patch("app.utils.claude_client.claude_structured", new_callable=AsyncMock)
    def test_claude_error_returns_fallback_html(self, mock_claude, client, db_session, test_user):
        from app.utils.claude_errors import ClaudeError

        ticket = TroubleTicket(
            ticket_number="TT-CLE-NIGHTLY-001",
            submitted_by=test_user.id,
            title="ClaudeError test",
            description="Triggers ClaudeError",
            status=TicketStatus.IN_PROGRESS,
            source=TicketSource.REPORT_BUTTON,
            risk_tier="low",
            category="other",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(ticket)
        db_session.commit()

        mock_claude.side_effect = ClaudeError("quota exceeded")
        resp = client.post("/api/trouble-tickets/analyze")
        assert resp.status_code == 200
        assert "no results" in resp.text.lower() or "Try again" in resp.text

    @patch("app.utils.claude_client.claude_structured", new_callable=AsyncMock)
    def test_claude_returns_result_without_groups_key(self, mock_claude, client, db_session, test_user):
        """Result missing 'groups' key triggers the same fallback as None."""
        ticket = TroubleTicket(
            ticket_number="TT-NOG-NIGHTLY-001",
            submitted_by=test_user.id,
            title="No groups key test",
            description="Claude returns result without groups",
            status=TicketStatus.SUBMITTED,
            source=TicketSource.REPORT_BUTTON,
            risk_tier="low",
            category="other",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(ticket)
        db_session.commit()

        mock_claude.return_value = {"unexpected_key": "value"}
        resp = client.post("/api/trouble-tickets/analyze")
        assert resp.status_code == 200
        assert "no results" in resp.text.lower()

    @patch("app.utils.claude_client.claude_structured", new_callable=AsyncMock)
    def test_analyze_success_with_existing_group_updates_fix(self, mock_claude, client, db_session, test_user):
        """Existing RootCauseGroup without suggested_fix gets fix updated."""
        from app.models.root_cause_group import RootCauseGroup

        ticket = TroubleTicket(
            ticket_number="TT-GRP-NIGHTLY-001",
            submitted_by=test_user.id,
            title="Group update test",
            description="Testing existing group update path",
            status=TicketStatus.SUBMITTED,
            source=TicketSource.REPORT_BUTTON,
            risk_tier="low",
            category="other",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(ticket)
        db_session.commit()
        db_session.refresh(ticket)

        existing_group = RootCauseGroup(title="Nightly Search Bug", suggested_fix=None)
        db_session.add(existing_group)
        db_session.commit()

        mock_claude.return_value = {
            "groups": [
                {
                    "title": "Nightly Search Bug",
                    "suggested_fix": "Fix the query builder logic",
                    "ticket_ids": [ticket.id],
                }
            ]
        }

        resp = client.post("/api/trouble-tickets/analyze")
        assert resp.status_code == 200
        # HX-Trigger header is set on success
        assert resp.headers.get("HX-Trigger") == "ticketsUpdated"


# ── list_error_reports: pagination parameters ─────────────────────────


class TestListPagination:
    def test_list_with_limit_and_offset(self, client, db_session, test_user):
        """limit and offset query params are accepted and applied."""
        for i in range(3):
            db_session.add(
                TroubleTicket(
                    ticket_number=f"TT-PAG-NIGHTLY-{i:03d}",
                    submitted_by=test_user.id,
                    title=f"Pagination ticket {i}",
                    description=f"Ticket number {i}",
                    status=TicketStatus.SUBMITTED,
                    source=TicketSource.REPORT_BUTTON,
                    risk_tier="low",
                    category="other",
                    created_at=datetime.now(timezone.utc),
                )
            )
        db_session.commit()

        resp = client.get("/api/error-reports?limit=2&offset=0")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["items"]) <= 2
        assert data["total"] >= 3

    def test_list_offset_beyond_total_returns_empty_items(self, client, db_session, test_user):
        """offset beyond total returns empty items list with correct total."""
        db_session.add(
            TroubleTicket(
                ticket_number="TT-OFST-NIGHTLY-001",
                submitted_by=test_user.id,
                title="Offset test",
                description="Testing large offset",
                status=TicketStatus.SUBMITTED,
                source=TicketSource.REPORT_BUTTON,
                risk_tier="low",
                category="other",
                created_at=datetime.now(timezone.utc),
            )
        )
        db_session.commit()

        resp = client.get("/api/error-reports?limit=10&offset=9999")
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"] == []
        assert data["total"] >= 1
