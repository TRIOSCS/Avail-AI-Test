"""
test_routers_error_reports.py — Tests for the simplified error report / trouble ticket router.

The error_reports router provides basic ticket CRUD: submit, list, detail.
No AI diagnosis, no admin-only gates, no status update or export endpoints.

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
