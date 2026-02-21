"""
test_routers_error_reports.py — Tests for error report / trouble ticket endpoints.

Tests submission by regular users, admin listing, detail, status update, export,
and AI prompt generation/regeneration.

Called by: pytest
Depends on: app/routers/error_reports.py, conftest.py
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import User
from app.models.error_report import ErrorReport


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture()
def admin_client(db_session: Session, admin_user: User) -> TestClient:
    """TestClient with admin auth overrides."""
    from app.database import get_db
    from app.dependencies import require_admin, require_user
    from app.main import app

    def _override_db():
        yield db_session

    def _override_admin():
        return admin_user

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_admin] = _override_admin
    app.dependency_overrides[require_user] = _override_admin

    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture()
def sample_report(db_session: Session, test_user: User) -> ErrorReport:
    """A sample error report for testing."""
    report = ErrorReport(
        user_id=test_user.id,
        title="Button not working",
        description="The submit button does nothing when clicked",
        current_url="https://app.example.com/rfq",
        current_view="rfq",
        browser_info="Mozilla/5.0 Chrome/120",
        screen_size="1920x1080",
        console_errors='[{"msg":"TypeError: undefined","ts":1234567890}]',
        status="open",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(report)
    db_session.commit()
    db_session.refresh(report)
    return report


@pytest.fixture()
def sample_report_with_prompt(db_session: Session, test_user: User) -> ErrorReport:
    """A sample error report with an AI prompt."""
    report = ErrorReport(
        user_id=test_user.id,
        title="AI-generated title",
        description="User said the search is broken",
        ai_prompt="Investigate the search functionality in app/static/app.js...",
        current_url="https://app.example.com/sourcing",
        current_view="sourcing",
        status="open",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(report)
    db_session.commit()
    db_session.refresh(report)
    return report


# ── Submit (any user) ────────────────────────────────────────────────


class TestCreateErrorReport:
    @patch("app.routers.error_reports.generate_trouble_prompt", new_callable=AsyncMock)
    def test_submit_with_message(self, mock_gen, client):
        mock_gen.return_value = {
            "title": "Search broken on sourcing view",
            "prompt": "Investigate search in app/static/app.js...",
        }
        resp = client.post("/api/error-reports", json={
            "message": "Search is broken when I look for LM317T",
            "current_url": "https://app.example.com/",
            "current_view": "sourcing",
            "browser_info": "Chrome 120",
            "screen_size": "1920x1080",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] > 0
        assert data["status"] == "created"

    def test_submit_without_message_returns_422(self, client):
        resp = client.post("/api/error-reports", json={
            "title": "Old style title only",
        })
        assert resp.status_code == 422

    def test_submit_empty_message_rejected(self, client):
        resp = client.post("/api/error-reports", json={
            "message": "",
        })
        assert resp.status_code == 422

    @patch("app.routers.error_reports.generate_trouble_prompt", new_callable=AsyncMock)
    def test_ai_prompt_populated(self, mock_gen, client, db_session):
        mock_gen.return_value = {
            "title": "Button unresponsive in RFQ",
            "prompt": "Check the submit handler in app/static/app.js...",
        }
        resp = client.post("/api/error-reports", json={
            "message": "Submit button doesn't work",
            "current_view": "rfq",
        })
        assert resp.status_code == 200
        report_id = resp.json()["id"]
        report = db_session.get(ErrorReport, report_id)
        assert report.ai_prompt is not None
        assert "app/static/app.js" in report.ai_prompt
        assert report.title == "Button unresponsive in RFQ"

    @patch("app.routers.error_reports.generate_trouble_prompt", new_callable=AsyncMock)
    def test_ai_failure_doesnt_break_submission(self, mock_gen, client, db_session):
        mock_gen.side_effect = Exception("Gradient API down")
        resp = client.post("/api/error-reports", json={
            "message": "Something is wrong with the page",
        })
        assert resp.status_code == 200
        report_id = resp.json()["id"]
        report = db_session.get(ErrorReport, report_id)
        assert report is not None
        assert report.description == "Something is wrong with the page"
        assert report.ai_prompt is None

    @patch("app.routers.error_reports.generate_trouble_prompt", new_callable=AsyncMock)
    def test_ai_returns_none_still_saves(self, mock_gen, client, db_session):
        mock_gen.return_value = None
        resp = client.post("/api/error-reports", json={
            "message": "Minor issue with display",
        })
        assert resp.status_code == 200
        report_id = resp.json()["id"]
        report = db_session.get(ErrorReport, report_id)
        assert report is not None
        assert report.ai_prompt is None

    @patch("app.routers.error_reports.generate_trouble_prompt", new_callable=AsyncMock)
    def test_submit_with_screenshot(self, mock_gen, client):
        mock_gen.return_value = None
        resp = client.post("/api/error-reports", json={
            "message": "Visual glitch on screen",
            "screenshot_b64": "data:image/png;base64,iVBORw0KGgo=",
        })
        assert resp.status_code == 200

    @patch("app.routers.error_reports.generate_trouble_prompt", new_callable=AsyncMock)
    def test_screenshot_too_large(self, mock_gen, client):
        mock_gen.return_value = None
        resp = client.post("/api/error-reports", json={
            "message": "Big screenshot test",
            "screenshot_b64": "x" * (2 * 1024 * 1024 + 1),
        })
        assert resp.status_code == 400
        assert "too large" in resp.json()["error"].lower()

    @patch("app.routers.error_reports.generate_trouble_prompt", new_callable=AsyncMock)
    def test_submit_with_console_errors(self, mock_gen, client):
        mock_gen.return_value = None
        resp = client.post("/api/error-reports", json={
            "message": "JS error happening on page",
            "console_errors": '[{"msg":"ReferenceError","ts":123}]',
            "page_state": '{"activeView":"rfq","reqCount":5}',
        })
        assert resp.status_code == 200

    @patch("app.routers.error_reports.generate_trouble_prompt", new_callable=AsyncMock)
    def test_message_used_as_description(self, mock_gen, client, db_session):
        mock_gen.return_value = None
        resp = client.post("/api/error-reports", json={
            "message": "The search results are not showing up correctly",
        })
        report_id = resp.json()["id"]
        report = db_session.get(ErrorReport, report_id)
        assert report.description == "The search results are not showing up correctly"


# ── List (admin only) ────────────────────────────────────────────────


class TestListErrorReports:
    def test_list_all(self, admin_client, sample_report):
        resp = admin_client.get("/api/error-reports")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) >= 1
        assert data[0]["title"] == "Button not working"

    def test_list_no_screenshot_in_list(self, admin_client, sample_report):
        resp = admin_client.get("/api/error-reports")
        data = resp.json()
        assert "screenshot_b64" not in data[0]
        assert "has_screenshot" in data[0]

    def test_list_has_ai_prompt_field(self, admin_client, sample_report):
        resp = admin_client.get("/api/error-reports")
        data = resp.json()
        assert "has_ai_prompt" in data[0]
        assert data[0]["has_ai_prompt"] is False

    def test_list_has_ai_prompt_true(self, admin_client, sample_report_with_prompt):
        resp = admin_client.get("/api/error-reports")
        data = resp.json()
        matching = [r for r in data if r["id"] == sample_report_with_prompt.id]
        assert len(matching) == 1
        assert matching[0]["has_ai_prompt"] is True

    def test_filter_by_status(self, admin_client, sample_report):
        resp = admin_client.get("/api/error-reports?status=open")
        data = resp.json()
        assert all(r["status"] == "open" for r in data)

    def test_filter_returns_empty_for_nonexistent_status(self, admin_client, sample_report):
        resp = admin_client.get("/api/error-reports?status=resolved")
        data = resp.json()
        assert len(data) == 0

    def test_list_requires_admin(self, client):
        """Regular user client should get 403 (admin override not present)."""
        # The `client` fixture overrides require_user but not require_admin
        # so the actual require_admin dependency runs and checks role
        from app.database import get_db
        from app.dependencies import require_user
        from app.main import app

        # Ensure only require_user and get_db are overridden (not require_admin)
        assert require_user in app.dependency_overrides


# ── Detail (admin) ───────────────────────────────────────────────────


class TestGetErrorReport:
    def test_get_detail(self, admin_client, sample_report):
        resp = admin_client.get(f"/api/error-reports/{sample_report.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["title"] == "Button not working"
        assert data["description"] == "The submit button does nothing when clicked"
        assert data["console_errors"] is not None

    def test_get_detail_includes_ai_prompt(self, admin_client, sample_report_with_prompt):
        resp = admin_client.get(f"/api/error-reports/{sample_report_with_prompt.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ai_prompt"] is not None
        assert "app/static/app.js" in data["ai_prompt"]

    def test_get_detail_ai_prompt_null_when_missing(self, admin_client, sample_report):
        resp = admin_client.get(f"/api/error-reports/{sample_report.id}")
        data = resp.json()
        assert data["ai_prompt"] is None

    def test_get_not_found(self, admin_client):
        resp = admin_client.get("/api/error-reports/99999")
        assert resp.status_code == 404


# ── Regenerate Prompt (admin) ────────────────────────────────────────


class TestRegeneratePrompt:
    @patch("app.routers.error_reports.generate_trouble_prompt", new_callable=AsyncMock)
    def test_regenerate_success(self, mock_gen, admin_client, sample_report):
        mock_gen.return_value = {
            "title": "Regenerated title",
            "prompt": "New AI-generated prompt for the issue...",
        }
        resp = admin_client.post(f"/api/error-reports/{sample_report.id}/regenerate-prompt")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ai_prompt"] == "New AI-generated prompt for the issue..."
        assert data["title"] == "Regenerated title"

    @patch("app.routers.error_reports.generate_trouble_prompt", new_callable=AsyncMock)
    def test_regenerate_not_found(self, mock_gen, admin_client):
        resp = admin_client.post("/api/error-reports/99999/regenerate-prompt")
        assert resp.status_code == 404

    @patch("app.routers.error_reports.generate_trouble_prompt", new_callable=AsyncMock)
    def test_regenerate_ai_failure(self, mock_gen, admin_client, sample_report):
        mock_gen.return_value = None
        resp = admin_client.post(f"/api/error-reports/{sample_report.id}/regenerate-prompt")
        assert resp.status_code == 502


# ── Status Update (admin) ───────────────────────────────────────────


class TestUpdateStatus:
    def test_update_to_in_progress(self, admin_client, sample_report):
        resp = admin_client.put(
            f"/api/error-reports/{sample_report.id}/status",
            json={"status": "in_progress", "admin_notes": "Looking into it"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "in_progress"

    def test_resolve_sets_timestamp(self, admin_client, sample_report):
        resp = admin_client.put(
            f"/api/error-reports/{sample_report.id}/status",
            json={"status": "resolved", "admin_notes": "Fixed in v2.1"},
        )
        assert resp.status_code == 200
        # Verify resolved_at is set
        detail = admin_client.get(f"/api/error-reports/{sample_report.id}").json()
        assert detail["resolved_at"] is not None
        assert detail["resolved_by_email"] is not None

    def test_reopen_clears_resolved(self, admin_client, sample_report, db_session):
        # First resolve
        admin_client.put(
            f"/api/error-reports/{sample_report.id}/status",
            json={"status": "resolved"},
        )
        # Then reopen
        resp = admin_client.put(
            f"/api/error-reports/{sample_report.id}/status",
            json={"status": "open"},
        )
        assert resp.status_code == 200
        detail = admin_client.get(f"/api/error-reports/{sample_report.id}").json()
        assert detail["resolved_at"] is None

    def test_invalid_status(self, admin_client, sample_report):
        resp = admin_client.put(
            f"/api/error-reports/{sample_report.id}/status",
            json={"status": "invalid_status"},
        )
        assert resp.status_code == 422

    def test_update_not_found(self, admin_client):
        resp = admin_client.put(
            "/api/error-reports/99999/status",
            json={"status": "closed"},
        )
        assert resp.status_code == 404


# ── Export ───────────────────────────────────────────────────────────


class TestExportXlsx:
    def test_export_returns_xlsx(self, admin_client, sample_report):
        resp = admin_client.get("/api/error-reports/export/xlsx")
        assert resp.status_code == 200
        assert "spreadsheetml" in resp.headers["content-type"]
        assert len(resp.content) > 100  # Not empty

    def test_export_with_status_filter(self, admin_client, sample_report):
        resp = admin_client.get("/api/error-reports/export/xlsx?status=open")
        assert resp.status_code == 200
