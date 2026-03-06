"""
tests/test_auth.py — Tests for authentication endpoints

Covers the agent-session endpoint (POST /auth/agent-session) used by
headless Playwright test agents to obtain a session cookie.

Called by: pytest
Depends on: app.routers.auth, conftest fixtures
"""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.models import User

from app.database import get_db
from app.main import app


@pytest.fixture()
def raw_client(db_session):
    """TestClient WITHOUT auth overrides — exercises real session/auth logic."""

    def _override_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture()
def agent_user(db_session) -> User:
    """The agent@availai.local service user."""
    user = User(
        email="agent@availai.local",
        name="Agent",
        role="admin",
        azure_id="agent-service",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def test_agent_session_returns_cookie(raw_client, agent_user):
    """Valid agent key + existing agent user -> 200 with session cookie."""
    resp = raw_client.post(
        "/auth/agent-session",
        headers={"x-agent-key": "test-agent-key-secret"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["user"] == "agent@availai.local"
    # Session cookie should be set
    assert "session" in resp.cookies or any("session" in c for c in resp.headers.get_list("set-cookie"))


def test_agent_session_rejects_bad_key(raw_client, agent_user):
    """Wrong agent key -> 401."""
    resp = raw_client.post(
        "/auth/agent-session",
        headers={"x-agent-key": "wrong-key"},
    )
    assert resp.status_code == 401
    assert "error" in resp.json()


def test_agent_session_rejects_missing_key(raw_client, agent_user):
    """No x-agent-key header -> 401."""
    resp = raw_client.post("/auth/agent-session")
    assert resp.status_code == 401
    assert "error" in resp.json()


def test_agent_session_rejects_missing_user(raw_client):
    """Valid key but no agent user in DB -> 401."""
    resp = raw_client.post(
        "/auth/agent-session",
        headers={"x-agent-key": "test-agent-key-secret"},
    )
    assert resp.status_code == 401
    assert resp.json()["error"] == "Agent user not found"
