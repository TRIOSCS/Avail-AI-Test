"""Tests for Find Trouble service -- dedup, loop manager, endpoints.

Called by: pytest
Depends on: app.services.site_tester, app.services.find_trouble_service
"""

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import User
from app.models.trouble_ticket import TroubleTicket


# ── Fixtures ──────────────────────────────────────────────────────────

@pytest.fixture()
def ft_user(db_session: Session) -> User:
    user = User(email="ft@test.com", name="FT Tester", role="admin")
    db_session.add(user)
    db_session.commit()
    return user


def _make_admin_client(db_session, user):
    from app.database import get_db
    from app.dependencies import require_admin, require_user
    from app.main import app

    app.dependency_overrides[get_db] = lambda: (yield db_session).__next__() or db_session
    app.dependency_overrides[require_user] = lambda: user
    app.dependency_overrides[require_admin] = lambda: user

    def _override_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_db

    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture()
def admin_client(db_session, ft_user):
    yield from _make_admin_client(db_session, ft_user)


# ── Dedup Tests ───────────────────────────────────────────────────────

def test_dedup_skips_existing_open_ticket(db_session: Session, ft_user: User):
    """create_tickets_from_issues should skip issues matching an open ticket in same area."""
    from app.services.site_tester import create_tickets_from_issues

    existing = TroubleTicket(
        ticket_number="TT-20260306-901",
        title="Console errors on load: search",
        description="2 console error(s) on initial load",
        status="submitted",
        submitted_by=ft_user.id,
        source="playwright",
        current_view="search",
    )
    db_session.add(existing)
    db_session.commit()

    issues = [{
        "area": "search",
        "title": "Console errors on load: search",
        "description": "3 console error(s) on initial load",
        "url": "http://localhost:8000/#view-sourcing",
        "console_errors": ["[error] something"],
        "network_errors": [],
    }]

    count = asyncio.get_event_loop().run_until_complete(
        create_tickets_from_issues(issues, db_session)
    )
    assert count == 0


def test_dedup_creates_ticket_for_new_area(db_session: Session, ft_user: User):
    """create_tickets_from_issues should create tickets for areas with no open tickets."""
    from app.services.site_tester import create_tickets_from_issues

    issues = [{
        "area": "rfq",
        "title": "Network error on load: rfq",
        "description": "1 failed network request(s)",
        "url": "http://localhost:8000/#view-rfq",
        "console_errors": [],
        "network_errors": [{"url": "/api/rfq", "failure": "net::ERR"}],
    }]

    count = asyncio.get_event_loop().run_until_complete(
        create_tickets_from_issues(issues, db_session)
    )
    assert count == 1


def test_dedup_allows_resolved_area(db_session: Session, ft_user: User):
    """Resolved tickets should NOT block creation of new tickets in same area."""
    from app.services.site_tester import create_tickets_from_issues

    resolved = TroubleTicket(
        ticket_number="TT-20260306-902",
        title="Console errors on load: search",
        description="old issue",
        status="resolved",
        submitted_by=ft_user.id,
        source="playwright",
        current_view="search",
    )
    db_session.add(resolved)
    db_session.commit()

    issues = [{
        "area": "search",
        "title": "Console errors on load: search",
        "description": "new issue same title",
        "url": "http://localhost:8000/#view-sourcing",
        "console_errors": [],
        "network_errors": [],
    }]

    count = asyncio.get_event_loop().run_until_complete(
        create_tickets_from_issues(issues, db_session)
    )
    assert count == 1


# ── FindTroubleService Tests ─────────────────────────────────────────

def test_find_trouble_service_singleton():
    from app.services.find_trouble_service import FindTroubleService

    svc = FindTroubleService()
    assert svc.active_job is None
    assert svc.is_running is False


def test_find_trouble_service_status_when_not_running():
    from app.services.find_trouble_service import FindTroubleService

    svc = FindTroubleService()
    status = svc.get_status()
    assert status["running"] is False
    assert status["round"] == 0


def test_find_trouble_service_cannot_start_twice():
    from app.services.find_trouble_service import FindTroubleService

    svc = FindTroubleService()
    svc.active_job = {"running": True, "cancel": False}
    result = svc.try_start("http://localhost", "cookie")
    assert result is None
    svc.active_job = None


def test_find_trouble_service_stop():
    from app.services.find_trouble_service import FindTroubleService

    svc = FindTroubleService()
    assert svc.stop() is False  # Nothing running

    svc.active_job = {"running": True, "cancel": False}
    assert svc.stop() is True
    assert svc.active_job["cancel"] is True
    svc.active_job = None


def test_find_trouble_service_events():
    from app.services.find_trouble_service import FindTroubleService

    svc = FindTroubleService()
    svc._emit("test", "hello")
    svc._emit("test2", "world")
    assert len(svc.consume_events(after=0)) == 2
    assert len(svc.consume_events(after=1)) == 1
    assert len(svc.consume_events(after=2)) == 0


# ── Endpoint Tests ────────────────────────────────────────────────────

def test_find_trouble_start_endpoint(admin_client):
    with patch("app.services.find_trouble_service._service") as mock_svc:
        mock_svc.try_start.return_value = {"status": "started"}
        mock_svc.is_running = False
        resp = admin_client.post("/api/trouble-tickets/find-trouble")
        assert resp.status_code == 200


def test_find_trouble_start_already_running(admin_client):
    with patch("app.services.find_trouble_service._service") as mock_svc:
        mock_svc.try_start.return_value = None
        resp = admin_client.post("/api/trouble-tickets/find-trouble")
        assert resp.status_code == 409


def test_find_trouble_stop_endpoint(admin_client):
    with patch("app.services.find_trouble_service._service") as mock_svc:
        mock_svc.stop.return_value = True
        resp = admin_client.post("/api/trouble-tickets/find-trouble/stop")
        assert resp.status_code == 200


def test_find_trouble_prompts_endpoint(admin_client):
    resp = admin_client.get("/api/trouble-tickets/find-trouble/prompts")
    assert resp.status_code == 200
    data = resp.json()
    assert "prompts" in data
    assert len(data["prompts"]) >= 17
