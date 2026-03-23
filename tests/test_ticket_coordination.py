"""Test trouble ticket endpoints (simplified CRUD).

The active-areas and similar-check endpoints were removed during
simplification. These tests verify the existing simplified ticket
CRUD endpoints.

Called by: pytest
Depends on: app/routers/error_reports.py, conftest fixtures
"""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import User
from app.models.trouble_ticket import TroubleTicket


@pytest.fixture()
def admin_client(db_session: Session, admin_user: User) -> TestClient:
    """TestClient with admin auth overrides."""
    from app.database import get_db
    from app.dependencies import require_admin, require_user
    from app.main import app

    def _override_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_user] = lambda: admin_user
    app.dependency_overrides[require_admin] = lambda: admin_user

    try:
        with TestClient(app) as c:
            yield c
    finally:
        for dep in [get_db, require_user, require_admin]:
            app.dependency_overrides.pop(dep, None)


def test_create_ticket(admin_client):
    """Submit a trouble ticket via POST."""
    resp = admin_client.post(
        "/api/trouble-tickets",
        json={"message": "Something is broken on the search page"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "id" in data
    assert data["status"] == "created"


def test_list_tickets_empty(admin_client):
    """List returns empty when no report_button tickets exist."""
    resp = admin_client.get("/api/trouble-tickets")
    assert resp.status_code == 200
    assert resp.json()["items"] == []


def test_list_tickets_with_data(admin_client, db_session):
    """List returns tickets created via the report button."""
    t = TroubleTicket(
        ticket_number="TT-COORD-001",
        submitted_by=1,
        title="Test ticket",
        description="Test description",
        source="report_button",
        status="submitted",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(t)
    db_session.commit()
    resp = admin_client.get("/api/trouble-tickets")
    assert resp.status_code == 200
    assert len(resp.json()["items"]) >= 1


def test_get_ticket_by_id(admin_client, db_session):
    """Get a single ticket by ID."""
    t = TroubleTicket(
        ticket_number="TT-COORD-002",
        submitted_by=1,
        title="Detail test",
        description="Detail description",
        source="report_button",
        status="submitted",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(t)
    db_session.commit()
    db_session.refresh(t)

    resp = admin_client.get(f"/api/trouble-tickets/{t.id}")
    assert resp.status_code == 200
    assert resp.json()["title"] == "Detail test"


def test_get_ticket_not_found(admin_client):
    """Non-existent ticket returns 404."""
    resp = admin_client.get("/api/trouble-tickets/99999")
    assert resp.status_code == 404


def test_create_ticket_validates_message(admin_client):
    """Empty message should be rejected."""
    resp = admin_client.post(
        "/api/trouble-tickets",
        json={"message": ""},
    )
    assert resp.status_code == 422
