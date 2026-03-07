"""
tests/test_routers_auth.py -- Tests for routers/auth.py

Covers: login redirect, OAuth callback (new/existing user, error cases,
admin promotion), logout, auth status, and index page.

Called by: pytest
Depends on: app/routers/auth.py, conftest.py
"""

from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlparse

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


def _get_oauth_state(client):
    """Hit /auth/login and extract the state param from redirect."""
    resp = client.get("/auth/login", follow_redirects=False)
    parsed = urlparse(resp.headers["location"])
    params = parse_qs(parsed.query)
    return params["state"][0]


@pytest.fixture()
def auth_client(db_session: Session) -> TestClient:
    """TestClient WITHOUT auth overrides (for testing login/callback flows)."""
    from app.database import get_db
    from app.main import app

    def _override_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_db

    # Use https base_url so Secure session cookies work with TestClient
    with TestClient(app, base_url="https://testserver") as c:
        yield c
    app.dependency_overrides.clear()


# ── Login ────────────────────────────────────────────────────────────


def test_login_redirects_to_azure(auth_client):
    """GET /auth/login returns 302 redirect to Microsoft."""
    resp = auth_client.get("/auth/login", follow_redirects=False)
    assert resp.status_code in (302, 307)
    assert "login.microsoftonline.com" in resp.headers["location"]
    assert "client_id=" in resp.headers["location"]


def test_login_includes_state_param(auth_client):
    """GET /auth/login includes a state parameter in the redirect URL."""
    resp = auth_client.get("/auth/login", follow_redirects=False)
    location = resp.headers["location"]
    parsed = urlparse(location)
    params = parse_qs(parsed.query)
    assert "state" in params, "Missing OAuth state parameter"
    assert len(params["state"][0]) >= 32, "State token too short"


def test_login_url_encodes_scope(auth_client):
    """GET /auth/login properly URL-encodes the scope parameter."""
    resp = auth_client.get("/auth/login", follow_redirects=False)
    location = resp.headers["location"]
    # Spaces in scope should be encoded as + or %20, not raw spaces
    assert " " not in location.split("?", 1)[1], "Query string contains unencoded spaces"


@patch("app.routers.auth.http")
def test_callback_validates_state(mock_http, auth_client):
    """Callback rejects request when state param doesn't match session."""
    resp = auth_client.get(
        "/auth/callback?code=test-code&state=wrong-state",
        follow_redirects=False,
    )
    assert resp.status_code in (302, 307)
    mock_http.post.assert_not_called()


@patch("app.routers.auth.http")
def test_callback_missing_state_rejected(mock_http, auth_client):
    """Callback rejects request when state param is missing entirely."""
    resp = auth_client.get(
        "/auth/callback?code=test-code",
        follow_redirects=False,
    )
    assert resp.status_code in (302, 307)
    mock_http.post.assert_not_called()


# ── Callback ─────────────────────────────────────────────────────────


def test_callback_missing_code(auth_client):
    """Callback without code param redirects to /."""
    resp = auth_client.get("/auth/callback", follow_redirects=False)
    assert resp.status_code in (302, 307)


@patch("app.routers.auth.http")
def test_callback_success_new_user(mock_http, auth_client, db_session):
    """Callback exchanges code, creates new user, redirects to /."""
    state = _get_oauth_state(auth_client)
    mock_http.post = AsyncMock(return_value=_mock_token_response())
    mock_http.get = AsyncMock(return_value=_mock_graph_me())

    resp = auth_client.get(f"/auth/callback?code=test-auth-code&state={state}", follow_redirects=False)
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
    state = _get_oauth_state(auth_client)
    mock_http.post = AsyncMock(
        return_value=_mock_token_response(
            access_token="updated-token",
            refresh_token="updated-refresh",
        )
    )
    mock_http.get = AsyncMock(
        return_value=_mock_graph_me(
            email=test_user.email,
            name=test_user.name,
        )
    )

    resp = auth_client.get(f"/auth/callback?code=test-auth-code&state={state}", follow_redirects=False)
    assert resp.status_code in (302, 307)

    db_session.refresh(test_user)
    assert test_user.access_token == "updated-token"
    assert test_user.refresh_token == "updated-refresh"


@patch("app.routers.auth.http")
def test_callback_token_exchange_fails(mock_http, auth_client):
    """Azure returns error during token exchange -> redirect to /."""
    state = _get_oauth_state(auth_client)
    fail_resp = MagicMock()
    fail_resp.status_code = 400
    fail_resp.json.return_value = {"error": "invalid_grant"}
    mock_http.post = AsyncMock(return_value=fail_resp)

    resp = auth_client.get(f"/auth/callback?code=bad-code&state={state}", follow_redirects=False)
    assert resp.status_code in (302, 307)


@patch("app.routers.auth.http")
def test_callback_graph_me_fails(mock_http, auth_client):
    """Graph /me failure -> redirect to /."""
    state = _get_oauth_state(auth_client)
    mock_http.post = AsyncMock(return_value=_mock_token_response())
    fail_resp = MagicMock()
    fail_resp.status_code = 401
    mock_http.get = AsyncMock(return_value=fail_resp)

    resp = auth_client.get(f"/auth/callback?code=test-code&state={state}", follow_redirects=False)
    assert resp.status_code in (302, 307)


@patch("app.routers.auth.http")
def test_callback_auto_admin_promotion(mock_http, auth_client, db_session, monkeypatch):
    """User in ADMIN_EMAILS gets auto-promoted to admin."""
    state = _get_oauth_state(auth_client)
    from app.config import settings

    monkeypatch.setattr(settings, "admin_emails", ["promoted@trioscs.com"])

    mock_http.post = AsyncMock(return_value=_mock_token_response())
    mock_http.get = AsyncMock(
        return_value=_mock_graph_me(
            email="promoted@trioscs.com",
            name="Promoted User",
        )
    )

    auth_client.get(f"/auth/callback?code=test-code&state={state}", follow_redirects=False)

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


# ── Additional coverage tests ─────────────────────────────────────────

from datetime import datetime, timedelta, timezone

import httpx


class TestCallbackHTTPErrors:
    @patch("app.routers.auth.http")
    def test_callback_token_exchange_http_error(self, mock_http, auth_client):
        """httpx.HTTPError during token exchange -> redirect to /."""
        state = _get_oauth_state(auth_client)
        mock_http.post = AsyncMock(side_effect=httpx.HTTPError("Connection refused"))

        resp = auth_client.get(f"/auth/callback?code=test-code&state={state}", follow_redirects=False)
        assert resp.status_code in (302, 307)

    @patch("app.routers.auth.http")
    def test_callback_missing_access_token(self, mock_http, auth_client):
        """Token response missing access_token -> redirect to /."""
        state = _get_oauth_state(auth_client)
        token_resp = MagicMock()
        token_resp.status_code = 200
        token_resp.json.return_value = {
            "refresh_token": "mock-refresh",
            "expires_in": 3600,
            # access_token intentionally missing
        }
        mock_http.post = AsyncMock(return_value=token_resp)

        resp = auth_client.get(f"/auth/callback?code=test-code&state={state}", follow_redirects=False)
        assert resp.status_code in (302, 307)

    @patch("app.routers.auth.http")
    def test_callback_graph_me_http_error(self, mock_http, auth_client):
        """httpx.HTTPError during Graph /me call -> redirect to /."""
        state = _get_oauth_state(auth_client)
        mock_http.post = AsyncMock(return_value=_mock_token_response())
        mock_http.get = AsyncMock(side_effect=httpx.HTTPError("Timeout"))

        resp = auth_client.get(f"/auth/callback?code=test-code&state={state}", follow_redirects=False)
        assert resp.status_code in (302, 307)

    @patch("app.routers.auth.http")
    def test_callback_new_user_no_refresh_token(self, mock_http, auth_client, db_session):
        """Token response without refresh_token -> user created but no refresh_token stored."""
        state = _get_oauth_state(auth_client)
        token_resp = MagicMock()
        token_resp.status_code = 200
        token_resp.json.return_value = {
            "access_token": "mock-access-token",
            # no refresh_token
            "expires_in": 3600,
        }
        mock_http.post = AsyncMock(return_value=token_resp)
        mock_http.get = AsyncMock(return_value=_mock_graph_me(email="norefresh@trioscs.com", name="No Refresh"))

        resp = auth_client.get(f"/auth/callback?code=test-code&state={state}", follow_redirects=False)
        assert resp.status_code in (302, 307)

        user = db_session.query(User).filter_by(email="norefresh@trioscs.com").first()
        assert user is not None
        assert user.refresh_token is None
        assert user.access_token == "mock-access-token"

    @patch("app.routers.auth.http")
    def test_callback_first_login_no_inbox_scan(self, mock_http, auth_client, db_session):
        """New user with no last_inbox_scan -> logs backfill info."""
        state = _get_oauth_state(auth_client)
        mock_http.post = AsyncMock(return_value=_mock_token_response())
        mock_http.get = AsyncMock(return_value=_mock_graph_me(email="firsttime@trioscs.com", name="First Timer"))

        resp = auth_client.get(f"/auth/callback?code=test-code&state={state}", follow_redirects=False)
        assert resp.status_code in (302, 307)

        user = db_session.query(User).filter_by(email="firsttime@trioscs.com").first()
        assert user is not None
        assert user.last_inbox_scan is None

    @patch("app.routers.auth.http")
    def test_callback_uses_user_principal_name(self, mock_http, auth_client, db_session):
        """Profile missing 'mail' uses 'userPrincipalName' instead."""
        state = _get_oauth_state(auth_client)
        mock_http.post = AsyncMock(return_value=_mock_token_response())

        me_resp = MagicMock()
        me_resp.status_code = 200
        me_resp.json.return_value = {
            "mail": None,  # mail is None
            "userPrincipalName": "upn@trioscs.com",
            "displayName": "UPN User",
            "id": "az-upn",
        }
        mock_http.get = AsyncMock(return_value=me_resp)

        resp = auth_client.get(f"/auth/callback?code=test-code&state={state}", follow_redirects=False)
        assert resp.status_code in (302, 307)

        user = db_session.query(User).filter_by(email="upn@trioscs.com").first()
        assert user is not None
        assert user.name == "UPN User"


class TestAuthStatusExtended:
    @patch("app.routers.auth.get_user")
    def test_status_expired_token(self, mock_get_user, client, test_user, db_session):
        """User with expired token shows 'expired' status."""
        test_user.refresh_token = "test-refresh"
        test_user.m365_connected = True
        test_user.token_expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
        db_session.commit()
        mock_get_user.return_value = test_user

        resp = client.get("/auth/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["connected"] is True
        # Check user in users list has expired status
        for u in data["users"]:
            if u["id"] == test_user.id:
                assert u["status"] == "expired"

    @patch("app.routers.auth.get_user")
    def test_status_disconnected_user(self, mock_get_user, client, test_user, db_session):
        """User with m365_connected=False shows 'disconnected' status."""
        test_user.refresh_token = "test-refresh"
        test_user.m365_connected = False
        db_session.commit()
        mock_get_user.return_value = test_user

        resp = client.get("/auth/status")
        assert resp.status_code == 200
        data = resp.json()
        for u in data["users"]:
            if u["id"] == test_user.id:
                assert u["status"] == "disconnected"

    @patch("app.routers.auth.get_user")
    def test_status_with_timestamps(self, mock_get_user, client, test_user, db_session):
        """Auth status includes m365_last_healthy, last_inbox_scan, last_contacts_sync."""
        now = datetime.now(timezone.utc)
        test_user.refresh_token = "test-refresh"
        test_user.m365_connected = True
        test_user.token_expires_at = now + timedelta(hours=1)
        test_user.m365_last_healthy = now
        test_user.last_inbox_scan = now
        test_user.last_contacts_sync = now
        test_user.m365_error_reason = None
        db_session.commit()
        mock_get_user.return_value = test_user

        resp = client.get("/auth/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["m365_error"] is None
        assert data["m365_last_healthy"] is not None
        for u in data["users"]:
            if u["id"] == test_user.id:
                assert u["status"] == "connected"
                assert u["last_inbox_scan"] is not None
                assert u["last_contacts_sync"] is not None
                assert u["m365_last_healthy"] is not None

    @patch("app.routers.auth.get_user")
    def test_status_user_name_fallback(self, mock_get_user, client, test_user, db_session):
        """User with no name falls back to email prefix."""
        test_user.name = None
        test_user.refresh_token = "test-refresh"
        db_session.commit()
        mock_get_user.return_value = test_user

        resp = client.get("/auth/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["user_name"] == test_user.email.split("@")[0]


class TestMailboxSettingsError:
    @patch("app.routers.auth.http")
    def test_callback_mailbox_settings_exception(self, mock_http, auth_client, db_session):
        """fetch_and_store_mailbox_settings raises -> callback still succeeds."""
        state = _get_oauth_state(auth_client)
        mock_http.post = AsyncMock(return_value=_mock_token_response())
        mock_http.get = AsyncMock(return_value=_mock_graph_me(email="mailboxerr@trioscs.com", name="Mailbox Error"))

        with patch(
            "app.services.mailbox_intelligence.fetch_and_store_mailbox_settings",
            new_callable=AsyncMock,
            side_effect=RuntimeError("Mailbox API unavailable"),
        ):
            resp = auth_client.get(f"/auth/callback?code=test-code&state={state}", follow_redirects=False)
        assert resp.status_code in (302, 307)

        user = db_session.query(User).filter_by(email="mailboxerr@trioscs.com").first()
        assert user is not None
        assert user.access_token == "mock-access-token"


class TestIndexExtended:
    def test_index_unauthenticated(self, auth_client):
        """GET / without session still serves HTML page."""
        resp = auth_client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
