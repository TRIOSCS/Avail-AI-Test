"""test_routers_error_reports.py — Tests for the error report / trouble ticket router.

The error_reports router provides ticket CRUD: submit, list, detail, and
status update (PATCH). Includes both JSON API and HTMX form endpoints.

Called by: pytest
Depends on: app/routers/error_reports.py, conftest.py
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session

from app.models import User
from app.models.trouble_ticket import TroubleTicket

# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture()
def sample_report(db_session: Session, test_user: User) -> TroubleTicket:
    """A sample trouble ticket (source=report_button) for testing."""
    ticket = TroubleTicket(
        ticket_number="TT-TEST-001",
        submitted_by=test_user.id,
        title="Button not working",
        description="The submit button does nothing when clicked",
        current_page="https://app.example.com/rfq",
        status="submitted",
        source="report_button",
        risk_tier="low",
        category="other",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(ticket)
    db_session.commit()
    db_session.refresh(ticket)
    return ticket


@pytest.fixture()
def second_report(db_session: Session, test_user: User) -> TroubleTicket:
    """A second trouble ticket for list tests."""
    ticket = TroubleTicket(
        ticket_number="TT-TEST-002",
        submitted_by=test_user.id,
        title="Search results empty",
        description="No results returned for valid part number",
        current_page="https://app.example.com/sourcing",
        status="resolved",
        source="report_button",
        risk_tier="low",
        category="other",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(ticket)
    db_session.commit()
    db_session.refresh(ticket)
    return ticket


# ── Submit (any user) ────────────────────────────────────────────────


class TestCreateErrorReport:
    def test_submit_with_message(self, client):
        resp = client.post(
            "/api/error-reports",
            json={
                "message": "Search is broken when I look for LM317T",
                "current_url": "https://app.example.com/",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] > 0
        assert data["status"] == "created"

    def test_submit_without_message_returns_422(self, client):
        resp = client.post(
            "/api/error-reports",
            json={
                "current_url": "https://example.com",
            },
        )
        assert resp.status_code == 422

    def test_submit_empty_message_rejected(self, client):
        resp = client.post(
            "/api/error-reports",
            json={
                "message": "",
            },
        )
        assert resp.status_code == 422

    def test_message_stored_as_description(self, client, db_session):
        resp = client.post(
            "/api/error-reports",
            json={
                "message": "The search results are not showing up correctly",
            },
        )
        assert resp.status_code == 200
        ticket_id = resp.json()["id"]
        ticket = db_session.get(TroubleTicket, ticket_id)
        assert ticket.description == "The search results are not showing up correctly"

    def test_title_derived_from_message(self, client, db_session):
        msg = "Short bug report"
        resp = client.post(
            "/api/error-reports",
            json={"message": msg},
        )
        assert resp.status_code == 200
        ticket_id = resp.json()["id"]
        ticket = db_session.get(TroubleTicket, ticket_id)
        assert ticket.title == msg[:120]

    def test_long_title_truncated(self, client, db_session):
        msg = "A" * 200
        resp = client.post(
            "/api/error-reports",
            json={"message": msg},
        )
        assert resp.status_code == 200
        ticket_id = resp.json()["id"]
        ticket = db_session.get(TroubleTicket, ticket_id)
        assert len(ticket.title) == 120
        assert ticket.description == msg

    def test_current_url_stored(self, client, db_session):
        resp = client.post(
            "/api/error-reports",
            json={
                "message": "Page is broken",
                "current_url": "https://app.example.com/rfq",
            },
        )
        assert resp.status_code == 200
        ticket_id = resp.json()["id"]
        ticket = db_session.get(TroubleTicket, ticket_id)
        assert ticket.current_page == "https://app.example.com/rfq"

    def test_source_set_to_report_button(self, client, db_session):
        resp = client.post(
            "/api/error-reports",
            json={"message": "Something broke"},
        )
        assert resp.status_code == 200
        ticket_id = resp.json()["id"]
        ticket = db_session.get(TroubleTicket, ticket_id)
        assert ticket.source == "report_button"

    def test_submit_via_trouble_tickets_path(self, client):
        """The /api/trouble-tickets alias should also work."""
        resp = client.post(
            "/api/trouble-tickets",
            json={"message": "Alt path test"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "created"

    def test_ticket_number_derived_from_id(self, client, db_session):
        resp = client.post(
            "/api/error-reports",
            json={"message": "First ticket"},
        )
        ticket_id = resp.json()["id"]
        ticket = db_session.get(TroubleTicket, ticket_id)
        assert ticket.ticket_number == f"TT-{ticket.id:04d}"

    def test_sequential_ticket_numbers(self, client, db_session):
        resp1 = client.post("/api/error-reports", json={"message": "Ticket one"})
        resp2 = client.post("/api/error-reports", json={"message": "Ticket two"})
        t1 = db_session.get(TroubleTicket, resp1.json()["id"])
        t2 = db_session.get(TroubleTicket, resp2.json()["id"])
        assert t2.id > t1.id
        assert t2.ticket_number > t1.ticket_number


# ── List ─────────────────────────────────────────────────────────────


class TestListErrorReports:
    def test_list_returns_items_and_total(self, client, sample_report):
        resp = client.get("/api/error-reports")
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data
        assert "total" in data
        assert isinstance(data["items"], list)
        assert data["total"] >= 1

    def test_list_item_fields(self, client, sample_report):
        resp = client.get("/api/error-reports")
        data = resp.json()
        item = data["items"][0]
        assert "id" in item
        assert "ticket_number" in item
        assert "title" in item
        assert "status" in item
        assert "created_at" in item
        assert item["title"] == "Button not working"

    def test_filter_by_status(self, client, sample_report):
        resp = client.get("/api/error-reports?status=submitted")
        data = resp.json()
        assert data["total"] >= 1
        assert all(r["status"] == "submitted" for r in data["items"])

    def test_filter_returns_empty_for_nonexistent_status(self, client, sample_report):
        resp = client.get("/api/error-reports?status=nonexistent")
        data = resp.json()
        assert data["total"] == 0
        assert len(data["items"]) == 0

    def test_list_only_report_button_source(self, client, db_session, test_user, sample_report):
        """Tickets with source != 'report_button' should not appear."""
        other = TroubleTicket(
            ticket_number="TT-OTHER-001",
            submitted_by=test_user.id,
            title="Ticket form submission",
            description="From ticket form",
            status="submitted",
            source="ticket_form",
            risk_tier="low",
            category="other",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(other)
        db_session.commit()

        resp = client.get("/api/error-reports")
        data = resp.json()
        ids = [i["id"] for i in data["items"]]
        assert sample_report.id in ids
        assert other.id not in ids

    def test_list_via_trouble_tickets_path(self, client, sample_report):
        resp = client.get("/api/trouble-tickets")
        assert resp.status_code == 200
        assert resp.json()["total"] >= 1


# ── Detail ───────────────────────────────────────────────────────────


class TestGetErrorReport:
    def test_get_detail(self, client, sample_report):
        resp = client.get(f"/api/error-reports/{sample_report.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["title"] == "Button not working"
        assert data["description"] == "The submit button does nothing when clicked"
        assert data["status"] == "submitted"
        assert data["current_page"] == "https://app.example.com/rfq"

    def test_get_detail_fields(self, client, sample_report):
        resp = client.get(f"/api/error-reports/{sample_report.id}")
        data = resp.json()
        for field in (
            "id",
            "ticket_number",
            "title",
            "description",
            "status",
            "risk_tier",
            "category",
            "current_page",
            "created_at",
        ):
            assert field in data, f"Missing field: {field}"

    def test_get_not_found(self, client):
        resp = client.get("/api/error-reports/99999")
        assert resp.status_code == 404

    def test_get_via_trouble_tickets_path(self, client, sample_report):
        resp = client.get(f"/api/trouble-tickets/{sample_report.id}")
        assert resp.status_code == 200
        assert resp.json()["title"] == "Button not working"

    def test_get_detail_includes_resolution_fields(self, client, sample_report):
        resp = client.get(f"/api/error-reports/{sample_report.id}")
        data = resp.json()
        assert "resolution_notes" in data
        assert "resolved_at" in data


# ── Update (PATCH) ──────────────────────────────────────────────────


class TestUpdateTicket:
    def test_resolve_ticket(self, client, sample_report):
        resp = client.patch(
            f"/api/trouble-tickets/{sample_report.id}",
            json={"status": "resolved", "resolution_notes": "Fixed the button onclick handler"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "resolved"

    def test_resolve_sets_resolved_at_and_by(self, client, sample_report, db_session, test_user):
        client.patch(
            f"/api/trouble-tickets/{sample_report.id}",
            json={"status": "resolved"},
        )
        db_session.expire_all()
        ticket = db_session.get(TroubleTicket, sample_report.id)
        assert ticket.resolved_at is not None
        assert ticket.resolved_by_id == test_user.id

    def test_update_status_to_in_progress(self, client, sample_report):
        resp = client.patch(
            f"/api/trouble-tickets/{sample_report.id}",
            json={"status": "in_progress"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "in_progress"

    def test_wont_fix_does_not_set_resolved_at(self, client, sample_report, db_session):
        client.patch(
            f"/api/trouble-tickets/{sample_report.id}",
            json={"status": "wont_fix"},
        )
        db_session.expire_all()
        ticket = db_session.get(TroubleTicket, sample_report.id)
        assert ticket.resolved_at is None

    def test_update_sets_updated_at(self, client, sample_report, db_session):
        client.patch(
            f"/api/trouble-tickets/{sample_report.id}",
            json={"resolution_notes": "Looking into it"},
        )
        db_session.expire_all()
        ticket = db_session.get(TroubleTicket, sample_report.id)
        assert ticket.updated_at is not None

    def test_update_resolution_notes_only(self, client, sample_report):
        resp = client.patch(
            f"/api/trouble-tickets/{sample_report.id}",
            json={"resolution_notes": "Investigating..."},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "submitted"

    def test_invalid_status_rejected(self, client, sample_report):
        resp = client.patch(
            f"/api/trouble-tickets/{sample_report.id}",
            json={"status": "invalid_status"},
        )
        assert resp.status_code == 422

    def test_update_not_found(self, client):
        resp = client.patch(
            "/api/trouble-tickets/99999",
            json={"status": "resolved"},
        )
        assert resp.status_code == 404

    def test_update_via_error_reports_path(self, client, sample_report):
        resp = client.patch(
            f"/api/error-reports/{sample_report.id}",
            json={"status": "wont_fix"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "wont_fix"


# ── HTMX Form / Submit ──────────────────────────────────────────


class TestTroubleTicketForm:
    def test_form_returns_html(self, client):
        resp = client.get("/api/trouble-tickets/form")
        assert resp.status_code == 200
        assert "Report a Problem" in resp.text
        assert 'id="tr-description"' in resp.text

    def test_submit_creates_ticket(self, client, db_session):
        resp = client.post(
            "/api/trouble-tickets/submit",
            data={"message": "The filter button is broken", "current_url": "https://app.example.com/search"},
        )
        assert resp.status_code == 200
        assert "Report submitted" in resp.text
        assert "TT-" in resp.text

    def test_submit_empty_message_rejected(self, client):
        resp = client.post(
            "/api/trouble-tickets/submit",
            data={"message": "   "},
        )
        assert resp.status_code == 422

    def test_submit_stores_in_db(self, client, db_session):
        client.post(
            "/api/trouble-tickets/submit",
            data={"message": "Cannot save column changes"},
        )
        ticket = (
            db_session.query(TroubleTicket).filter(TroubleTicket.description == "Cannot save column changes").first()
        )
        assert ticket is not None
        assert ticket.source == "report_button"
        assert ticket.status == "submitted"

    def test_submit_db_error_returns_html(self, client, db_session):
        """If ticket creation fails, the HTMX endpoint returns an HTML error, not
        JSON."""
        from unittest.mock import patch

        with patch("app.routers.error_reports._create_ticket", side_effect=Exception("DB down")):
            resp = client.post(
                "/api/trouble-tickets/submit",
                data={"message": "Something broke on the search page"},
            )
        assert resp.status_code == 500
        assert "Something went wrong" in resp.text
        assert "text/html" in resp.headers.get("content-type", "")


# ── Screenshot serving ───────────────────────────────────────────


class TestScreenshot:
    def test_screenshot_not_found(self, client):
        resp = client.get("/api/trouble-tickets/99999/screenshot")
        assert resp.status_code == 404

    def test_screenshot_no_screenshot(self, client, sample_report):
        resp = client.get(f"/api/trouble-tickets/{sample_report.id}/screenshot")
        assert resp.status_code == 404

    def test_screenshot_legacy_b64(self, client, sample_report, db_session):
        import base64

        sample_report.screenshot_b64 = base64.b64encode(b"fakepng").decode()
        db_session.commit()
        resp = client.get(f"/api/trouble-tickets/{sample_report.id}/screenshot")
        assert resp.status_code == 200


# ── New JSON submit flow ─────────────────────────────────────────


class TestNewSubmitFlow:
    def test_json_submit_minimal(self, client):
        resp = client.post(
            "/api/trouble-tickets/submit",
            json={"description": "Button doesn't work"},
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200
        assert "Report submitted" in resp.text

    def test_json_submit_with_context(self, client):
        resp = client.post(
            "/api/trouble-tickets/submit",
            json={
                "description": "Search results empty",
                "page_url": "/v2/search",
                "user_agent": "Mozilla/5.0",
                "viewport": "1920x1080",
                "error_log": '[{"msg":"TypeError","ts":"2026-03-21"}]',
                "network_log": '[{"url":"/api/search","status":500}]',
            },
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200

    def test_json_submit_empty_description_422(self, client):
        resp = client.post(
            "/api/trouble-tickets/submit", json={"description": ""}, headers={"Content-Type": "application/json"}
        )
        assert resp.status_code == 422

    def test_json_submit_no_body_422(self, client):
        resp = client.post(
            "/api/trouble-tickets/submit", content=b"not json", headers={"Content-Type": "application/json"}
        )
        assert resp.status_code == 422

    def test_legacy_form_submit_still_works(self, client):
        resp = client.post(
            "/api/trouble-tickets/submit", data={"message": "Old form still works", "current_url": "/v2/test"}
        )
        assert resp.status_code == 200
        assert "Report submitted" in resp.text
