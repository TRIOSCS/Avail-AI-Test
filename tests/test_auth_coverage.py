"""
tests/test_auth_coverage.py — Coverage tests for auth.py edge cases

Covers: _password_login_enabled(), _verify_password(), password_login endpoint,
password_login_form redirect when disabled.

Called by: pytest
Depends on: app.routers.auth, conftest fixtures
"""

import base64
import hashlib
import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import User

# ── _password_login_enabled tests ────────────────────────────────────


def test_password_login_enabled_when_testing():
    """TESTING=1 should enable password login."""
    from app.routers.auth import _password_login_enabled

    with patch.dict(os.environ, {"TESTING": "1"}):
        assert _password_login_enabled() is True


def test_password_login_enabled_via_env_flag():
    """ENABLE_PASSWORD_LOGIN=true should enable password login."""
    from app.routers.auth import _password_login_enabled

    with patch.dict(os.environ, {"TESTING": "0", "ENABLE_PASSWORD_LOGIN": "true"}):
        assert _password_login_enabled() is True


def test_password_login_disabled_by_default():
    """Without TESTING or ENABLE_PASSWORD_LOGIN, should be disabled."""
    from app.routers.auth import _password_login_enabled

    with patch.dict(os.environ, {"TESTING": "0", "ENABLE_PASSWORD_LOGIN": "false"}):
        assert _password_login_enabled() is False


# ── _verify_password tests ───────────────────────────────────────────


def _make_password_hash(password: str) -> str:
    """Create a PBKDF2-HMAC-SHA256 hash in salt_b64$hash_b64 format."""
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return f"{base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def test_verify_password_correct():
    """Correct password should verify."""
    from app.routers.auth import _verify_password

    stored = _make_password_hash("secret123")
    assert _verify_password(stored, "secret123") is True


def test_verify_password_wrong():
    """Wrong password should not verify."""
    from app.routers.auth import _verify_password

    stored = _make_password_hash("secret123")
    assert _verify_password(stored, "wrongpassword") is False


def test_verify_password_malformed_hash():
    """Malformed hash should return False (not crash)."""
    from app.routers.auth import _verify_password

    assert _verify_password("not-a-valid-hash", "anything") is False


# ── password_login endpoint tests ────────────────────────────────────


@pytest.fixture()
def password_client(db_session: Session) -> TestClient:
    """TestClient without auth overrides — uses real session-based auth."""
    from app.database import get_db
    from app.main import app

    def _override_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_db

    with TestClient(app) as c:
        yield c

    app.dependency_overrides.clear()


def test_password_login_disabled(db_session: Session, password_client: TestClient):
    """POST /auth/login with password login disabled returns 403."""
    with patch("app.routers.auth._password_login_enabled", return_value=False):
        resp = password_client.post(
            "/auth/login",
            data={"email": "test@example.com", "password": "pass"},
        )
    assert resp.status_code == 403
    assert resp.json()["error"] == "Password login disabled"


def test_password_login_user_not_found(db_session: Session, password_client: TestClient):
    """POST /auth/login with nonexistent user returns 401."""
    with patch("app.routers.auth._password_login_enabled", return_value=True):
        resp = password_client.post(
            "/auth/login",
            data={"email": "nonexistent@example.com", "password": "pass"},
        )
    assert resp.status_code == 401
    assert resp.json()["error"] == "Invalid credentials"


def test_password_login_no_password_hash(db_session: Session, password_client: TestClient):
    """User exists but has no password_hash — returns 401."""
    user = User(email="nohash@example.com", name="No Hash", role="buyer")
    db_session.add(user)
    db_session.commit()

    with patch("app.routers.auth._password_login_enabled", return_value=True):
        resp = password_client.post(
            "/auth/login",
            data={"email": "nohash@example.com", "password": "pass"},
        )
    assert resp.status_code == 401
    assert resp.json()["error"] == "Invalid credentials"


def test_password_login_wrong_password(db_session: Session, password_client: TestClient):
    """User exists with password_hash but wrong password — returns 401."""
    user = User(
        email="hashuser@example.com",
        name="Hash User",
        role="buyer",
        password_hash=_make_password_hash("correct_password"),
    )
    db_session.add(user)
    db_session.commit()

    with patch("app.routers.auth._password_login_enabled", return_value=True):
        resp = password_client.post(
            "/auth/login",
            data={"email": "hashuser@example.com", "password": "wrong_password"},
        )
    assert resp.status_code == 401
    assert resp.json()["error"] == "Invalid credentials"


def test_password_login_success(db_session: Session, password_client: TestClient):
    """Correct credentials — returns 200 with user info."""
    user = User(
        email="gooduser@example.com",
        name="Good User",
        role="buyer",
        password_hash=_make_password_hash("mypassword"),
    )
    db_session.add(user)
    db_session.commit()

    with patch("app.routers.auth._password_login_enabled", return_value=True):
        resp = password_client.post(
            "/auth/login",
            data={"email": "gooduser@example.com", "password": "mypassword"},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["user_email"] == "gooduser@example.com"
    assert data["user_role"] == "buyer"


# ── password_login_form endpoint tests ───────────────────────────────


def test_login_form_disabled_redirects(password_client: TestClient):
    """GET /auth/login-form when disabled should redirect to /auth/login."""
    with patch("app.routers.auth._password_login_enabled", return_value=False):
        resp = password_client.get("/auth/login-form", follow_redirects=False)
    assert resp.status_code == 307
    assert "/auth/login" in resp.headers["location"]


def test_login_form_enabled_shows_html(password_client: TestClient):
    """GET /auth/login-form when enabled should return HTML form."""
    with patch("app.routers.auth._password_login_enabled", return_value=True):
        resp = password_client.get("/auth/login-form")
    assert resp.status_code == 200
    assert "Local Password Login" in resp.text
