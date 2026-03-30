"""tests/test_auth_router_coverage.py — Coverage tests for app/routers/auth.py.

Targets uncovered branches:
- _password_login_enabled(): ENABLE_PASSWORD_LOGIN env var paths
- _verify_password(): happy path and edge cases
- password_login endpoint: success, invalid credentials, disabled
- password_login_form: enabled/disabled
- logout GET method

Called by: pytest
Depends on: conftest.py, app/routers/auth.py
"""

import os

os.environ["TESTING"] = "1"

import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session

from app.models import User


# ── Helper: generate a real PBKDF2 password hash ────────────────────


def _hash_password(password: str) -> str:
    """Create a PBKDF2-HMAC-SHA256 hash in the format expected by _verify_password."""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return f"{base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


# ── _password_login_enabled tests ───────────────────────────────────


class TestPasswordLoginEnabled:
    def test_enabled_when_testing_is_set(self):
        """Returns True when TESTING=1 env var is set (always True in test env)."""
        from app.routers.auth import _password_login_enabled

        # TESTING=1 is set in conftest so this should always return True
        assert _password_login_enabled() is True

    def test_disabled_without_enable_flag(self, monkeypatch):
        """Returns False when TESTING is unset and ENABLE_PASSWORD_LOGIN is false."""
        from app.routers.auth import _password_login_enabled

        monkeypatch.delenv("TESTING", raising=False)
        monkeypatch.setenv("ENABLE_PASSWORD_LOGIN", "false")
        assert _password_login_enabled() is False

    def test_disabled_on_https_url(self, monkeypatch):
        """Returns False when APP_URL is HTTPS (production guard)."""
        from app.routers.auth import _password_login_enabled

        monkeypatch.delenv("TESTING", raising=False)
        monkeypatch.setenv("ENABLE_PASSWORD_LOGIN", "true")
        monkeypatch.setenv("APP_URL", "https://app.example.com")
        assert _password_login_enabled() is False

    def test_enabled_on_http_url(self, monkeypatch):
        """Returns True when APP_URL is HTTP (development mode)."""
        from app.routers.auth import _password_login_enabled

        monkeypatch.delenv("TESTING", raising=False)
        monkeypatch.setenv("ENABLE_PASSWORD_LOGIN", "true")
        monkeypatch.setenv("APP_URL", "http://localhost:8000")
        assert _password_login_enabled() is True

    def test_enabled_on_empty_url(self, monkeypatch):
        """Returns True when APP_URL is empty and ENABLE_PASSWORD_LOGIN=true."""
        from app.routers.auth import _password_login_enabled

        monkeypatch.delenv("TESTING", raising=False)
        monkeypatch.setenv("ENABLE_PASSWORD_LOGIN", "true")
        monkeypatch.setenv("APP_URL", "")
        assert _password_login_enabled() is True


# ── _verify_password tests ───────────────────────────────────────────


class TestVerifyPassword:
    def test_valid_password(self):
        """Returns True for correct password."""
        from app.routers.auth import _verify_password

        hashed = _hash_password("correct-password")
        assert _verify_password(hashed, "correct-password") is True

    def test_wrong_password(self):
        """Returns False for incorrect password."""
        from app.routers.auth import _verify_password

        hashed = _hash_password("correct-password")
        assert _verify_password(hashed, "wrong-password") is False

    def test_empty_stored_hash(self):
        """Returns False when stored hash is empty."""
        from app.routers.auth import _verify_password

        assert _verify_password("", "any-password") is False

    def test_no_dollar_sign_in_hash(self):
        """Returns False when stored hash has no $ separator."""
        from app.routers.auth import _verify_password

        assert _verify_password("noseparator", "password") is False

    def test_invalid_base64_in_hash(self):
        """Returns False for malformed base64 in stored hash."""
        from app.routers.auth import _verify_password

        # invalid base64 chars
        result = _verify_password("!@#$%^$!@#$%^", "password")
        assert result is False


# ── Password Login Endpoint ──────────────────────────────────────────


class TestPasswordLoginEndpoint:
    def test_login_success(self, client, db_session):
        """POST /auth/login with valid credentials returns 200."""
        password = "test-password-123"
        user = User(
            email="pwuser@trioscs.com",
            name="PW User",
            role="buyer",
            azure_id="pw-azure-001",
            password_hash=_hash_password(password),
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(user)
        db_session.commit()

        resp = client.post(
            "/auth/login",
            data={"email": "pwuser@trioscs.com", "password": password},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["user_email"] == "pwuser@trioscs.com"

    def test_login_wrong_password(self, client, db_session):
        """POST /auth/login with wrong password returns 401."""
        user = User(
            email="pwfail@trioscs.com",
            name="PW Fail",
            role="buyer",
            azure_id="pw-azure-002",
            password_hash=_hash_password("correct"),
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(user)
        db_session.commit()

        resp = client.post(
            "/auth/login",
            data={"email": "pwfail@trioscs.com", "password": "wrong"},
        )
        assert resp.status_code == 401
        assert resp.json()["error"] == "Invalid credentials"

    def test_login_user_not_found(self, client):
        """POST /auth/login with unknown email returns 401."""
        resp = client.post(
            "/auth/login",
            data={"email": "nobody@trioscs.com", "password": "any"},
        )
        assert resp.status_code == 401
        assert resp.json()["error"] == "Invalid credentials"

    def test_login_user_no_password_hash(self, client, db_session):
        """POST /auth/login with user that has no password_hash returns 401."""
        user = User(
            email="nohash@trioscs.com",
            name="No Hash",
            role="buyer",
            azure_id="pw-azure-003",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(user)
        db_session.commit()

        resp = client.post(
            "/auth/login",
            data={"email": "nohash@trioscs.com", "password": "any"},
        )
        assert resp.status_code == 401
        assert resp.json()["error"] == "Invalid credentials"

    def test_login_normalizes_email_to_lowercase(self, client, db_session):
        """Login accepts uppercase email and normalizes it."""
        password = "test-password-456"
        user = User(
            email="casetest@trioscs.com",
            name="Case Test",
            role="buyer",
            azure_id="pw-azure-004",
            password_hash=_hash_password(password),
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(user)
        db_session.commit()

        resp = client.post(
            "/auth/login",
            data={"email": "CASETEST@TRIOSCS.COM", "password": password},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_login_disabled_returns_403(self, client, monkeypatch):
        """POST /auth/login returns 403 when password login is disabled."""
        from unittest.mock import patch

        with patch("app.routers.auth._password_login_enabled", return_value=False):
            resp = client.post(
                "/auth/login",
                data={"email": "test@trioscs.com", "password": "any"},
            )
        assert resp.status_code == 403
        assert resp.json()["error"] == "Password login disabled"

    def test_login_returns_user_role(self, client, db_session):
        """Login response includes user role."""
        password = "role-test-pass"
        user = User(
            email="roletest@trioscs.com",
            name="Role Test",
            role="admin",
            azure_id="pw-azure-005",
            password_hash=_hash_password(password),
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(user)
        db_session.commit()

        resp = client.post(
            "/auth/login",
            data={"email": "roletest@trioscs.com", "password": password},
        )
        assert resp.status_code == 200
        assert resp.json()["user_role"] == "admin"

    def test_login_role_defaults_to_buyer_when_none(self, client, db_session):
        """Login response defaults role to 'buyer' when user.role is None."""
        password = "role-default-pass"
        user = User(
            email="norole@trioscs.com",
            name="No Role",
            role=None,
            azure_id="pw-azure-006",
            password_hash=_hash_password(password),
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(user)
        db_session.commit()

        resp = client.post(
            "/auth/login",
            data={"email": "norole@trioscs.com", "password": password},
        )
        assert resp.status_code == 200
        assert resp.json()["user_role"] == "buyer"


# ── Login Form ───────────────────────────────────────────────────────


class TestPasswordLoginForm:
    def test_login_form_returns_html_when_enabled(self, client):
        """GET /auth/login-form returns HTML login form when password login enabled."""
        # TESTING=1 always enables password login
        resp = client.get("/auth/login-form")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "Local Password Login" in resp.text

    def test_login_form_redirects_when_disabled(self, client):
        """GET /auth/login-form redirects to /auth/login when disabled."""
        from unittest.mock import patch

        with patch("app.routers.auth._password_login_enabled", return_value=False):
            resp = client.get("/auth/login-form", follow_redirects=False)
        assert resp.status_code in (302, 307)
        assert "/auth/login" in resp.headers["location"]


# ── Logout GET ───────────────────────────────────────────────────────


class TestLogoutGet:
    def test_logout_get_redirects(self, client):
        """GET /auth/logout clears session and redirects."""
        resp = client.get("/auth/logout", follow_redirects=False)
        assert resp.status_code == 302
        assert "/v2/requisitions" in resp.headers["location"]
