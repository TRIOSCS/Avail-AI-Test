"""
tests/test_routers_auth.py -- Tests for routers/auth.py

Covers: login redirect, OAuth callback (new/existing user, error cases,
admin promotion), logout, auth status, and index page.

Called by: pytest
Depends on: app/routers/auth.py, conftest.py
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import User


# ── Helpers ──────────────────────────────────────────────────────────


def _mock_token_response(access_token="mock-access-token", refresh_token="mock-refresh"):
    """Build a fake Azure token exchange response."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_in": 3600,
    }
    return resp


def _mock_graph_me(email="newuser@trioscs.com", name="New User", azure_id="az-001"):
    """Build a fake Graph /me response."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "mail": email,
        "displayName": name,
        "id": azure_id,
    }
    return resp


@pytest.fixture()
def auth_client(db_session: Session) -> TestClient:
    """TestClient WITHOUT auth overrides (for testing login/callback flows)."""
    from app.database import get_db
    from app.main import app

    def _override_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_db

    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ── Login ────────────────────────────────────────────────────────────


def test_login_redirects_to_azure(auth_client):
    """GET /auth/login returns 302 redirect to Microsoft."""
    resp = auth_client.get("/auth/login", follow_redirects=False)
    assert resp.status_code in (302, 307)
    assert "login.microsoftonline.com" in resp.headers["location"]
    assert "client_id=" in resp.headers["location"]


# ── Callback ─────────────────────────────────────────────────────────


def test_callback_missing_code(auth_client):
    """Callback without code param redirects to /."""
    resp = auth_client.get("/auth/callback", follow_redirects=False)
    assert resp.status_code in (302, 307)


@patch("app.routers.auth.http")
def test_callback_success_new_user(mock_http, auth_client, db_session):
    """Callback exchanges code, creates new user, redirects to /."""
    mock_http.post = AsyncMock(return_value=_mock_token_response())
    mock_http.get = AsyncMock(return_value=_mock_graph_me())

    resp = auth_client.get("/auth/callback?code=test-auth-code", follow_redirects=False)
    assert resp.status_code in (302, 307)

    # Verify user was created
    user = db_session.query(User).filter_by(email="newuser@trioscs.com").first()
    assert user is not None
    assert user.name == "New User"
    assert user.access_token == "mock-access-token"
    assert user.m365_connected is True


@patch("app.routers.auth.http")
def test_callback_success_existing_user(mock_http, auth_client, db_session, test_user):
    """Callback updates tokens for existing user."""
    mock_http.post = AsyncMock(return_value=_mock_token_response(
        access_token="updated-token",
        refresh_token="updated-refresh",
    ))
    mock_http.get = AsyncMock(return_value=_mock_graph_me(
        email=test_user.email, name=test_user.name,
    ))

    resp = auth_client.get("/auth/callback?code=test-auth-code", follow_redirects=False)
    assert resp.status_code in (302, 307)

    db_session.refresh(test_user)
    assert test_user.access_token == "updated-token"
    assert test_user.refresh_token == "updated-refresh"


@patch("app.routers.auth.http")
def test_callback_token_exchange_fails(mock_http, auth_client):
    """Azure returns error during token exchange -> redirect to /."""
    fail_resp = MagicMock()
    fail_resp.status_code = 400
    fail_resp.json.return_value = {"error": "invalid_grant"}
    mock_http.post = AsyncMock(return_value=fail_resp)

    resp = auth_client.get("/auth/callback?code=bad-code", follow_redirects=False)
    assert resp.status_code in (302, 307)


@patch("app.routers.auth.http")
def test_callback_graph_me_fails(mock_http, auth_client):
    """Graph /me failure -> redirect to /."""
    mock_http.post = AsyncMock(return_value=_mock_token_response())
    fail_resp = MagicMock()
    fail_resp.status_code = 401
    mock_http.get = AsyncMock(return_value=fail_resp)

    resp = auth_client.get("/auth/callback?code=test-code", follow_redirects=False)
    assert resp.status_code in (302, 307)


@patch("app.routers.auth.http")
def test_callback_auto_admin_promotion(mock_http, auth_client, db_session, monkeypatch):
    """User in ADMIN_EMAILS gets auto-promoted to admin."""
    from app.config import settings
    monkeypatch.setattr(settings, "admin_emails", ["promoted@trioscs.com"])

    mock_http.post = AsyncMock(return_value=_mock_token_response())
    mock_http.get = AsyncMock(return_value=_mock_graph_me(
        email="promoted@trioscs.com", name="Promoted User",
    ))

    auth_client.get("/auth/callback?code=test-code", follow_redirects=False)

    user = db_session.query(User).filter_by(email="promoted@trioscs.com").first()
    assert user is not None
    assert user.role == "admin"


# ── Logout ───────────────────────────────────────────────────────────


def test_logout_clears_session(client):
    """POST /auth/logout returns ok and clears session."""
    resp = client.post("/auth/logout")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


# ── Auth Status ──────────────────────────────────────────────────────


@patch("app.routers.auth.get_user")
def test_status_returns_m365_info(mock_get_user, client, test_user, db_session):
    """GET /auth/status returns connection info for authenticated user."""
    test_user.refresh_token = "test-refresh-token"
    db_session.commit()
    mock_get_user.return_value = test_user

    resp = client.get("/auth/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["connected"] is True
    assert data["user_email"] == test_user.email
    assert "users" in data


def test_status_unauthenticated(auth_client):
    """GET /auth/status without session returns connected=False."""
    resp = auth_client.get("/auth/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["connected"] is False


# ── Index ────────────────────────────────────────────────────────────


def test_index_serves_template(client):
    """GET / returns HTML page."""
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
