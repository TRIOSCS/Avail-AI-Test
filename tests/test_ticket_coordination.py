"""Test agent coordination endpoints (active-areas + similar check).

Called by: pytest
Depends on: app/routers/trouble_tickets.py, conftest fixtures
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

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

    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_active_areas_empty(admin_client):
    resp = admin_client.get("/api/trouble-tickets/active-areas")
    assert resp.status_code == 200
    assert resp.json()["areas"] == []


def test_active_areas_returns_tested_areas(admin_client, db_session):
    t = TroubleTicket(
        ticket_number="TT-20260306-090",
        submitted_by=1,
        title="Search broken",
        description="Search is not working",
        source="playwright",
        tested_area="search",
        status="submitted",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(t)
    db_session.commit()
    resp = admin_client.get("/api/trouble-tickets/active-areas")
    assert "search" in resp.json()["areas"]


def test_active_areas_excludes_non_agent_sources(admin_client, db_session):
    """Tickets from ticket_form source should not appear in active areas."""
    t = TroubleTicket(
        ticket_number="TT-20260306-091",
        submitted_by=1,
        title="Manual report",
        description="User filed manually",
        source="ticket_form",
        tested_area="dashboard",
        status="submitted",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(t)
    db_session.commit()
    resp = admin_client.get("/api/trouble-tickets/active-areas")
    assert resp.json()["areas"] == []


def test_similar_check_no_matches(admin_client):
    """With no open tickets, similar returns empty matches."""
    with patch(
        "app.services.ticket_consolidation.find_similar_ticket",
        new_callable=AsyncMock,
        return_value=None,
    ):
        resp = admin_client.get(
            "/api/trouble-tickets/similar",
            params={"title": "Something broke"},
        )
    assert resp.status_code == 200
    assert resp.json()["matches"] == []


def test_similar_check_requires_title(admin_client):
    """Title parameter is required and must be at least 3 chars."""
    resp = admin_client.get("/api/trouble-tickets/similar", params={"title": "ab"})
    assert resp.status_code == 422


def test_create_ticket_with_agent_fields(admin_client):
    """Agent fields (tested_area etc.) are accepted on creation."""
    resp = admin_client.post(
        "/api/trouble-tickets",
        json={
            "title": "Agent found broken search",
            "description": "Playwright detected error on search page",
            "source": "playwright",
            "tested_area": "search",
            "dom_snapshot": "<html>...</html>",
            "network_errors": [{"url": "/api/search", "status": 500}],
            "performance_timings": {"ttfb": 1200},
            "reproduction_steps": ["Navigate to search", "Enter MPN", "Click search"],
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert "id" in data
