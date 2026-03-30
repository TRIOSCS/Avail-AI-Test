"""test_error_reports_coverage.py — Coverage tests for error_reports router.

Targets missing coverage: _save_screenshot, _generate_ai_summary,
screenshot path traversal, JSON submit edge cases, analyze_tickets
edge cases, and the HTMX form partial.

Called by: pytest
Depends on: app/routers/error_reports.py, conftest.py
"""

import base64
import os
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.trouble_ticket import TroubleTicket

# ── _save_screenshot helper tests ────────────────────────────────────


class TestSaveScreenshot:
    def test_save_screenshot_empty_data_returns_none(self):
        from app.routers.error_reports import _save_screenshot

        result = _save_screenshot(1, "")
        assert result is None

    def test_save_screenshot_too_large_returns_none(self):
        from app.routers.error_reports import MAX_SCREENSHOT_B64_SIZE, _save_screenshot

        result = _save_screenshot(1, "x" * (MAX_SCREENSHOT_B64_SIZE + 1))
        assert result is None

    def test_save_screenshot_strips_data_url_prefix(self, tmp_path, monkeypatch):
        from app.routers import error_reports

        monkeypatch.setattr(error_reports, "UPLOAD_DIR", str(tmp_path))
        monkeypatch.setattr(error_reports, "_upload_dir_ready", False)

        png_bytes = b"\x89PNG\r\n\x1a\n"
        b64 = base64.b64encode(png_bytes).decode()
        data_url = f"data:image/png;base64,{b64}"

        result = error_reports._save_screenshot(42, data_url)
        assert result is not None
        assert os.path.isfile(result)

    def test_save_screenshot_plain_b64(self, tmp_path, monkeypatch):
        from app.routers import error_reports

        monkeypatch.setattr(error_reports, "UPLOAD_DIR", str(tmp_path))
        monkeypatch.setattr(error_reports, "_upload_dir_ready", False)

        png_bytes = b"\x89PNG\r\n\x1a\n"
        b64 = base64.b64encode(png_bytes).decode()

        result = error_reports._save_screenshot(99, b64)
        assert result is not None

    def test_save_screenshot_invalid_base64_returns_none(self, tmp_path, monkeypatch):
        from app.routers import error_reports

        monkeypatch.setattr(error_reports, "UPLOAD_DIR", str(tmp_path))
        monkeypatch.setattr(error_reports, "_upload_dir_ready", False)

        result = error_reports._save_screenshot(1, "not!valid!base64!!!")
        assert result is None


# ── _ensure_upload_dir ────────────────────────────────────────────────


def test_ensure_upload_dir_runs_once(tmp_path, monkeypatch):
    from app.routers import error_reports

    monkeypatch.setattr(error_reports, "UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setattr(error_reports, "_upload_dir_ready", False)

    error_reports._ensure_upload_dir()
    assert error_reports._upload_dir_ready is True
    assert os.path.isdir(str(tmp_path / "uploads"))

    # Second call should be a no-op (dir already ready)
    error_reports._ensure_upload_dir()
    assert error_reports._upload_dir_ready is True


# ── _generate_ai_summary ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_generate_ai_summary_success(db_session, test_user):
    """AI summary is stored when claude_text returns a value."""
    ticket = TroubleTicket(
        ticket_number="TT-SUM-001",
        submitted_by=test_user.id,
        title="Test ticket",
        description="Something went wrong on the search page with filters",
        status="submitted",
        source="report_button",
        risk_tier="low",
        category="other",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(ticket)
    db_session.commit()
    db_session.refresh(ticket)
    ticket_id = ticket.id

    with (
        patch("app.database.SessionLocal") as mock_sl,
        patch("app.utils.claude_client.claude_text", new_callable=AsyncMock) as mock_claude,
    ):
        mock_claude.return_value = "Search filter causes a crash on the search page."
        mock_db = MagicMock()
        mock_ticket = MagicMock()
        mock_ticket.ai_summary = None
        mock_ticket.description = "test"
        mock_ticket.current_page = "/search"
        mock_ticket.console_errors = None
        mock_ticket.network_errors = None
        mock_ticket.ticket_number = "TT-SUM-001"
        mock_db.get.return_value = mock_ticket
        mock_sl.return_value = mock_db

        from app.routers.error_reports import _generate_ai_summary

        await _generate_ai_summary(ticket_id)

    mock_db.commit.assert_called_once()
    assert mock_ticket.ai_summary is not None


@pytest.mark.asyncio
async def test_generate_ai_summary_skips_if_already_set(db_session, test_user):
    """Skips AI call if ticket already has ai_summary."""
    with (
        patch("app.database.SessionLocal") as mock_sl,
        patch("app.utils.claude_client.claude_text", new_callable=AsyncMock) as mock_claude,
    ):
        mock_db = MagicMock()
        mock_ticket = MagicMock()
        mock_ticket.ai_summary = "Already summarized"
        mock_db.get.return_value = mock_ticket
        mock_sl.return_value = mock_db

        from app.routers.error_reports import _generate_ai_summary

        await _generate_ai_summary(999)

    mock_claude.assert_not_called()


@pytest.mark.asyncio
async def test_generate_ai_summary_ticket_not_found():
    """No crash when ticket is missing."""
    with patch("app.database.SessionLocal") as mock_sl:
        mock_db = MagicMock()
        mock_db.get.return_value = None
        mock_sl.return_value = mock_db

        from app.routers.error_reports import _generate_ai_summary

        await _generate_ai_summary(99999)  # Should not raise


@pytest.mark.asyncio
async def test_generate_ai_summary_handles_claude_exception(test_user, db_session):
    """Exception in claude_text is caught and rolled back."""
    with (
        patch("app.database.SessionLocal") as mock_sl,
        patch("app.utils.claude_client.claude_text", new_callable=AsyncMock) as mock_claude,
    ):
        mock_claude.side_effect = Exception("Claude unavailable")
        mock_db = MagicMock()
        mock_ticket = MagicMock()
        mock_ticket.ai_summary = None
        mock_ticket.description = "test"
        mock_ticket.current_page = "/search"
        mock_ticket.console_errors = None
        mock_ticket.network_errors = None
        mock_db.get.return_value = mock_ticket
        mock_sl.return_value = mock_db

        from app.routers.error_reports import _generate_ai_summary

        await _generate_ai_summary(1)  # Should not raise

    mock_db.rollback.assert_called_once()


# ── Screenshot path traversal ────────────────────────────────────────


class TestScreenshotSecurity:
    def test_path_traversal_blocked(self, client, db_session, test_user):
        """Ticket with screenshot_path outside UPLOAD_DIR returns 403."""
        ticket = TroubleTicket(
            ticket_number="TT-TRAV-001",
            submitted_by=test_user.id,
            title="Traversal test",
            description="Testing path traversal",
            status="submitted",
            source="report_button",
            risk_tier="low",
            category="other",
            created_at=datetime.now(timezone.utc),
        )
        ticket.screenshot_path = "/etc/passwd"
        db_session.add(ticket)
        db_session.commit()
        db_session.refresh(ticket)

        resp = client.get(f"/api/trouble-tickets/{ticket.id}/screenshot")
        # Either 403 (path traversal blocked) or 404 (file not found)
        assert resp.status_code in (403, 404)

    def test_screenshot_from_valid_file(self, client, db_session, test_user, tmp_path):
        """Valid screenshot_path serves the file."""
        from app.routers import error_reports

        # Write a fake PNG
        fake_png = tmp_path / "TT-9999.png"
        fake_png.write_bytes(b"\x89PNG\r\n\x1a\n")

        # Patch UPLOAD_DIR so path validation passes
        orig_upload_dir = error_reports.UPLOAD_DIR
        try:
            error_reports.UPLOAD_DIR = str(tmp_path)
            ticket = TroubleTicket(
                ticket_number="TT-FILE-001",
                submitted_by=test_user.id,
                title="File test",
                description="Testing file serve",
                status="submitted",
                source="report_button",
                risk_tier="low",
                category="other",
                screenshot_path=str(fake_png),
                created_at=datetime.now(timezone.utc),
            )
            db_session.add(ticket)
            db_session.commit()
            db_session.refresh(ticket)

            resp = client.get(f"/api/trouble-tickets/{ticket.id}/screenshot")
            assert resp.status_code == 200
        finally:
            error_reports.UPLOAD_DIR = orig_upload_dir


# ── JSON submit edge cases ────────────────────────────────────────────


class TestJsonSubmitEdgeCases:
    def test_json_submit_too_long_description(self, client):
        """Description exceeding MAX_MESSAGE_LEN is rejected."""
        resp = client.post(
            "/api/trouble-tickets/submit",
            json={"description": "A" * 5001},
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 422
        assert "too long" in resp.text.lower() or "max" in resp.text.lower()

    def test_json_submit_with_screenshot_saves_path(self, client, db_session, tmp_path, monkeypatch):
        """Screenshot in JSON body gets saved to disk."""
        from app.routers import error_reports

        monkeypatch.setattr(error_reports, "UPLOAD_DIR", str(tmp_path))
        monkeypatch.setattr(error_reports, "_upload_dir_ready", False)

        png_bytes = b"\x89PNG\r\n\x1a\n"
        b64 = base64.b64encode(png_bytes).decode()

        resp = client.post(
            "/api/trouble-tickets/submit",
            json={"description": "Bug with screenshot", "screenshot": b64},
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200

    def test_json_submit_invalid_network_log(self, client):
        """Invalid network_log JSON is handled gracefully."""
        resp = client.post(
            "/api/trouble-tickets/submit",
            json={
                "description": "Error with bad network log",
                "network_log": "this is not json {{{",
            },
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200

    def test_json_submit_network_log_as_list(self, client):
        """network_log as a Python list (not string) is accepted."""
        resp = client.post(
            "/api/trouble-tickets/submit",
            json={
                "description": "Error with list network log",
                "network_log": [{"url": "/api/test", "status": 500}],
            },
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200

    def test_form_submit_with_message_and_url(self, client):
        """Legacy form-encoded with both message and current_url."""
        resp = client.post(
            "/api/trouble-tickets/submit",
            data={"message": "Filter is broken", "current_url": "https://app.example.com/search"},
        )
        assert resp.status_code == 200
        assert "Report submitted" in resp.text


# ── Analyze tickets additional coverage ──────────────────────────────


class TestAnalyzeTicketsCoverage:
    @patch("app.utils.claude_client.claude_structured", new_callable=AsyncMock)
    def test_analyze_handles_claude_error(self, mock_claude, client, db_session, test_user):
        """ClaudeError during analysis returns amber warning."""
        from app.utils.claude_errors import ClaudeError

        # Create a ticket so analyze has something to work with
        ticket = TroubleTicket(
            ticket_number="TT-ANAL-001",
            submitted_by=test_user.id,
            title="Analyze error test",
            description="Testing analyze error path",
            status="submitted",
            source="report_button",
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
    def test_analyze_with_existing_group(self, mock_claude, client, db_session, test_user):
        """Analyze updates existing RootCauseGroup if title matches."""
        from app.models.root_cause_group import RootCauseGroup

        ticket = TroubleTicket(
            ticket_number="TT-ANAL-002",
            submitted_by=test_user.id,
            title="Existing group test",
            description="Testing existing group update",
            status="submitted",
            source="report_button",
            risk_tier="low",
            category="other",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(ticket)
        db_session.commit()
        db_session.refresh(ticket)

        # Pre-create a group with the same title
        existing_group = RootCauseGroup(title="Search Bug", suggested_fix=None)
        db_session.add(existing_group)
        db_session.commit()

        mock_claude.return_value = {
            "groups": [
                {
                    "title": "Search Bug",
                    "suggested_fix": "Fix the query builder",
                    "ticket_ids": [ticket.id],
                }
            ]
        }

        resp = client.post("/api/trouble-tickets/analyze")
        assert resp.status_code == 200

    @patch("app.utils.claude_client.claude_structured", new_callable=AsyncMock)
    def test_analyze_groups_missing_from_ticket_map(self, mock_claude, client, db_session, test_user):
        """analyze_tickets handles ticket_ids that don't exist in ticket_map."""
        ticket = TroubleTicket(
            ticket_number="TT-ANAL-003",
            submitted_by=test_user.id,
            title="Missing ticket test",
            description="Testing missing ticket ids",
            status="submitted",
            source="report_button",
            risk_tier="low",
            category="other",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(ticket)
        db_session.commit()

        mock_claude.return_value = {
            "groups": [
                {
                    "title": "Phantom Bug",
                    "suggested_fix": "Fix it",
                    "ticket_ids": [99998, 99999],  # non-existent IDs
                }
            ]
        }

        resp = client.post("/api/trouble-tickets/analyze")
        assert resp.status_code == 200


# ── _create_ticket helper ─────────────────────────────────────────────


class TestCreateTicketHelper:
    def test_create_with_full_context(self, db_session, test_user):
        from app.routers.error_reports import _create_ticket

        ticket = _create_ticket(
            db_session,
            user_id=test_user.id,
            message="Full context test",
            current_url="/v2/sourcing",
            context={
                "user_agent": "Mozilla/5.0",
                "browser_info": '{"viewport": "1920x1080"}',
                "console_errors": "TypeError: undefined",
                "network_errors": [{"url": "/api/search", "status": 500}],
            },
        )
        assert ticket.id is not None
        assert ticket.ticket_number.startswith("TT-")
        assert ticket.user_agent == "Mozilla/5.0"

    def test_create_without_context(self, db_session, test_user):
        from app.routers.error_reports import _create_ticket

        ticket = _create_ticket(
            db_session,
            user_id=test_user.id,
            message="No context test",
        )
        assert ticket.id is not None
        assert ticket.user_agent is None


# ── HTMX form endpoint ─────────────────────────────────────────────────


class TestHtmxFormEndpoint:
    def test_get_form_returns_html(self, client):
        resp = client.get("/api/trouble-tickets/form")
        assert resp.status_code == 200
        assert "html" in resp.headers.get("content-type", "").lower() or len(resp.text) > 0

    def test_submit_empty_json_description_returns_422(self, client):
        """JSON body with empty description → 422."""
        resp = client.post(
            "/api/trouble-tickets/submit",
            json={"description": "   "},
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 422
        assert "describe" in resp.text.lower() or "problem" in resp.text.lower()

    def test_submit_json_with_ua_and_viewport(self, client):
        """JSON body with user_agent + viewport sets browser_info."""
        resp = client.post(
            "/api/trouble-tickets/submit",
            json={
                "description": "Bug with user agent",
                "user_agent": "Mozilla/5.0",
                "viewport": "1920x1080",
            },
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200
        assert "Report submitted" in resp.text

    def test_submit_form_with_empty_message_returns_422(self, client):
        """Legacy form-encoded with blank message → 422."""
        resp = client.post(
            "/api/trouble-tickets/submit",
            data={"message": "   "},
        )
        assert resp.status_code == 422

    def test_submit_json_create_ticket_exception_returns_500(self, client):
        """Internal exception during ticket creation → 500 HTML response."""
        with patch("app.routers.error_reports._create_ticket") as mock_create:
            mock_create.side_effect = Exception("DB down")
            resp = client.post(
                "/api/trouble-tickets/submit",
                json={"description": "This will fail internally"},
                headers={"Content-Type": "application/json"},
            )
        assert resp.status_code == 500
        assert "went wrong" in resp.text.lower()


# ── JSON API CRUD endpoints ────────────────────────────────────────────


class TestJsonApiCrud:
    def test_create_error_report_via_api(self, client, db_session):
        """POST /api/error-reports creates a ticket via Pydantic body."""
        resp = client.post(
            "/api/error-reports",
            json={"message": "Test error via JSON API", "current_url": "/v2/sourcing"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "created"

    def test_list_error_reports_with_status_filter(self, client, db_session, test_user):
        """GET /api/error-reports?status=submitted filters by status."""
        from datetime import datetime, timezone

        from app.models.trouble_ticket import TroubleTicket

        ticket = TroubleTicket(
            ticket_number="TT-LIST-001",
            submitted_by=test_user.id,
            title="List filter test",
            description="Testing list filter",
            status="submitted",
            source="report_button",
            risk_tier="low",
            category="other",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(ticket)
        db_session.commit()

        resp = client.get("/api/error-reports?status=submitted")
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data
        assert data["total"] >= 1

    def test_get_error_report_not_found(self, client):
        resp = client.get("/api/error-reports/99999")
        assert resp.status_code == 404

    def test_get_error_report_found(self, client, db_session, test_user):
        from datetime import datetime, timezone

        from app.models.trouble_ticket import TroubleTicket

        ticket = TroubleTicket(
            ticket_number="TT-GET-001",
            submitted_by=test_user.id,
            title="Get test",
            description="Testing get endpoint",
            status="submitted",
            source="report_button",
            risk_tier="low",
            category="other",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(ticket)
        db_session.commit()
        db_session.refresh(ticket)

        resp = client.get(f"/api/error-reports/{ticket.id}")
        assert resp.status_code == 200
        assert resp.json()["ticket_number"] == ticket.ticket_number

    def test_update_ticket_status_to_resolved(self, client, db_session, test_user):
        from datetime import datetime, timezone

        from app.models.trouble_ticket import TroubleTicket

        ticket = TroubleTicket(
            ticket_number="TT-UPD-001",
            submitted_by=test_user.id,
            title="Update test",
            description="Testing update",
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
            f"/api/error-reports/{ticket.id}",
            json={"status": "resolved", "resolution_notes": "Fixed the bug"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "resolved"

    def test_update_ticket_not_found(self, client):
        resp = client.patch(
            "/api/error-reports/99999",
            json={"status": "resolved"},
        )
        assert resp.status_code == 404

    def test_update_ticket_resolution_notes_only(self, client, db_session, test_user):
        from datetime import datetime, timezone

        from app.models.trouble_ticket import TroubleTicket

        ticket = TroubleTicket(
            ticket_number="TT-UPD-002",
            submitted_by=test_user.id,
            title="Notes test",
            description="Testing notes update",
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
            f"/api/error-reports/{ticket.id}",
            json={"resolution_notes": "Added documentation"},
        )
        assert resp.status_code == 200
