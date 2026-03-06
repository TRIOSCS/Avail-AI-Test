"""Test Find Trouble endpoints.

Tests the /api/trouble-tickets/find-trouble/* endpoints that power the
automated site audit feature (Playwright sweep + Claude agent prompts).

Called by: pytest
Depends on: conftest.py (db_session, admin_user), app/routers/trouble_tickets.py
"""
import pytest
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from app.models import User


@pytest.fixture()
def admin_client(db_session: Session, admin_user: User) -> TestClient:
    """TestClient authenticated as admin for find-trouble endpoints."""
    from app.database import get_db
    from app.dependencies import require_admin, require_user
    from app.main import app

    def _override_db():
        yield db_session

    def _override_admin():
        return admin_user

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_user] = _override_admin
    app.dependency_overrides[require_admin] = _override_admin

    with TestClient(app) as c:
        yield c

    app.dependency_overrides.clear()


def test_find_trouble_prompts(admin_client):
    """GET /find-trouble/prompts returns structured prompts for all UI areas."""
    resp = admin_client.get("/api/trouble-tickets/find-trouble/prompts")
    assert resp.status_code == 200
    prompts = resp.json()["prompts"]
    assert len(prompts) >= 15
    assert all("area" in p and "prompt" in p for p in prompts)


def test_find_trouble_progress_not_found(admin_client):
    """GET /find-trouble/{job_id} returns 404 for unknown job."""
    resp = admin_client.get("/api/trouble-tickets/find-trouble/nonexistent")
    assert resp.status_code == 404
