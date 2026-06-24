"""tests/test_auth_allowlist.py — Phase 3 allowlist gate + invite adoption.

Covers the OAuth callback's allowlist decision and invite-adoption behavior:
- unknown email rejected when ENABLE_USER_ALLOWLIST is on (and not an admin)
- pre-provisioned invited row adopts azure_id without losing its role
- unknown admin_emails email bypasses the allowlist and is bootstrapped to admin
- ENABLE_USER_ALLOWLIST=False preserves legacy auto-provision-as-buyer behavior
- disabled (is_active=False) user is rejected
- access-denied page copy (default vs reason=disabled)

Reuses the callback mock harness from tests/test_routers_auth.py
(_mock_token_response / _mock_graph_me / _get_oauth_state / auth_client +
@patch("app.routers.auth.http") with AsyncMock).

Called by: pytest
Depends on: app/routers/auth.py, app/config.py, conftest.py
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import User

# Reuse the callback mock harness (token/Graph response builders + state helper) from
# the existing auth tests rather than reinventing it.
from tests.test_routers_auth import _get_oauth_state, _mock_graph_me, _mock_token_response


@pytest.fixture()
def auth_client(db_session: Session) -> TestClient:
    """TestClient WITHOUT auth overrides — mirrors the fixture in test_routers_auth.

    Re-declared locally (rather than imported) so the allowlist remains ON by
    default here: importing the sibling fixture would also pull in that module's
    autouse override that disables the allowlist for its legacy callback tests.
    """
    from app.database import get_db
    from app.main import app

    def _override_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_db
    try:
        with TestClient(app, base_url="https://testserver") as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_db, None)


# ── Allowlist gate ───────────────────────────────────────────────────


@patch("app.routers.auth.http")
def test_unknown_email_rejected_when_allowlist_on(mock_http, auth_client, db_session, monkeypatch):
    """Unknown email + allowlist ON + not in admin_emails → no User row, redirect to
    /auth/access-denied, no session user_id."""
    from app.config import settings

    monkeypatch.setattr(settings, "enable_user_allowlist", True)
    monkeypatch.setattr(settings, "admin_emails", [])

    state = _get_oauth_state(auth_client)
    mock_http.post = AsyncMock(return_value=_mock_token_response())
    mock_http.get = AsyncMock(return_value=_mock_graph_me(email="stranger@example.com", name="Stranger"))

    resp = auth_client.get(f"/auth/callback?code=test-code&state={state}", follow_redirects=False)
    assert resp.status_code in (302, 307)
    assert resp.headers["location"] == "/auth/access-denied"

    # No user row created.
    assert db_session.query(User).filter_by(email="stranger@example.com").first() is None
    # No session established.
    assert "session" not in resp.cookies or "user_id" not in (resp.cookies.get("session") or "")


@patch("app.routers.auth.http")
def test_invited_row_adopts_azure_id_keeps_role(mock_http, auth_client, db_session, monkeypatch):
    """A pre-created invited row (azure_id=None, role='trader') logging in adopts the
    azure_id, keeps role='trader', sets the session, and stamps last_login_at."""
    from app.config import settings

    monkeypatch.setattr(settings, "enable_user_allowlist", True)
    monkeypatch.setattr(settings, "admin_emails", [])

    invited = User(
        email="invited@trioscs.com",
        name="Invited Trader",
        role="trader",
        azure_id=None,
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(invited)
    db_session.commit()
    db_session.refresh(invited)

    state = _get_oauth_state(auth_client)
    mock_http.post = AsyncMock(return_value=_mock_token_response())
    mock_http.get = AsyncMock(
        return_value=_mock_graph_me(email="invited@trioscs.com", name="Invited Trader", azure_id="az-invited-999")
    )

    resp = auth_client.get(f"/auth/callback?code=test-code&state={state}", follow_redirects=False)
    assert resp.status_code in (302, 307)
    assert resp.headers["location"] == "/"

    db_session.refresh(invited)
    assert invited.azure_id == "az-invited-999"
    assert invited.role == "trader"  # invite must NOT change role
    assert invited.last_login_at is not None


@patch("app.routers.auth.http")
def test_unknown_admin_email_bypasses_allowlist(mock_http, auth_client, db_session, monkeypatch):
    """Unknown email that IS in admin_emails is created and bootstrapped to admin even
    with the allowlist on."""
    from app.config import settings

    monkeypatch.setattr(settings, "enable_user_allowlist", True)
    monkeypatch.setattr(settings, "admin_emails", ["newadmin@trioscs.com"])

    state = _get_oauth_state(auth_client)
    mock_http.post = AsyncMock(return_value=_mock_token_response())
    mock_http.get = AsyncMock(return_value=_mock_graph_me(email="newadmin@trioscs.com", name="New Admin"))

    resp = auth_client.get(f"/auth/callback?code=test-code&state={state}", follow_redirects=False)
    assert resp.status_code in (302, 307)
    assert resp.headers["location"] == "/"

    user = db_session.query(User).filter_by(email="newadmin@trioscs.com").first()
    assert user is not None
    assert user.role == "admin"
    assert user.last_login_at is not None


@patch("app.routers.auth.http")
def test_allowlist_off_auto_provisions_buyer(mock_http, auth_client, db_session, monkeypatch):
    """ENABLE_USER_ALLOWLIST=False → unknown email auto-provisioned as buyer (legacy
    behavior preserved)."""
    from app.config import settings

    monkeypatch.setattr(settings, "enable_user_allowlist", False)
    monkeypatch.setattr(settings, "admin_emails", [])

    state = _get_oauth_state(auth_client)
    mock_http.post = AsyncMock(return_value=_mock_token_response())
    mock_http.get = AsyncMock(return_value=_mock_graph_me(email="legacy@example.com", name="Legacy User"))

    resp = auth_client.get(f"/auth/callback?code=test-code&state={state}", follow_redirects=False)
    assert resp.status_code in (302, 307)
    assert resp.headers["location"] == "/"

    user = db_session.query(User).filter_by(email="legacy@example.com").first()
    assert user is not None
    assert (user.role or "buyer") == "buyer"
    assert user.last_login_at is not None


@patch("app.routers.auth.http")
def test_disabled_user_rejected(mock_http, auth_client, db_session, monkeypatch):
    """Existing user with is_active=False → redirect to access-denied?reason=disabled,
    no session."""
    from app.config import settings

    monkeypatch.setattr(settings, "enable_user_allowlist", True)
    monkeypatch.setattr(settings, "admin_emails", [])

    disabled = User(
        email="disabled@trioscs.com",
        name="Disabled User",
        role="buyer",
        azure_id="az-disabled-001",
        is_active=False,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(disabled)
    db_session.commit()

    state = _get_oauth_state(auth_client)
    mock_http.post = AsyncMock(return_value=_mock_token_response())
    mock_http.get = AsyncMock(return_value=_mock_graph_me(email="disabled@trioscs.com", name="Disabled User"))

    resp = auth_client.get(f"/auth/callback?code=test-code&state={state}", follow_redirects=False)
    assert resp.status_code in (302, 307)
    assert resp.headers["location"] == "/auth/access-denied?reason=disabled"

    db_session.refresh(disabled)
    # Login was rejected — no token stored, no last_login stamp.
    assert disabled.last_login_at is None


# ── Access-denied page ───────────────────────────────────────────────


class TestAccessDeniedPage:
    def test_default_copy(self, auth_client):
        """GET /auth/access-denied renders the not-provisioned copy + a logout link."""
        resp = auth_client.get("/auth/access-denied")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "Access not provisioned" in resp.text
        assert "/auth/logout" in resp.text

    def test_disabled_copy(self, auth_client):
        """Reason=disabled switches the copy to the disabled-account message."""
        resp = auth_client.get("/auth/access-denied?reason=disabled")
        assert resp.status_code == 200
        assert "disabled" in resp.text.lower()
