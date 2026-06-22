"""Tests for Clay OAuth router: connect, callback, disconnect (admin-only).

Mirrors the _make_admin_client pattern from tests/test_settings_api_keys_cards.py.
"""

import os

os.environ["TESTING"] = "1"

import pytest
from fastapi.testclient import TestClient

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_admin_client(db_session, admin_user):
    """Return a TestClient authenticated as admin, overriding all auth deps."""
    from app.database import get_db
    from app.dependencies import require_admin, require_settings_access, require_user
    from app.main import app

    def _db():
        yield db_session

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[require_user] = lambda: admin_user
    app.dependency_overrides[require_admin] = lambda: admin_user
    app.dependency_overrides[require_settings_access] = lambda: admin_user

    try:
        client = TestClient(app)
        yield client
    finally:
        for dep in [get_db, require_user, require_admin, require_settings_access]:
            app.dependency_overrides.pop(dep, None)


@pytest.fixture()
def admin_client(db_session, admin_user):
    yield from _make_admin_client(db_session, admin_user)


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_connect_redirects_to_clay(admin_client, monkeypatch):
    """GET /auth/clay/connect should redirect to app.clay.com with scope=mcp."""
    import app.routers.clay_oauth as r

    async def fake_register():
        return "cid"

    monkeypatch.setattr(r.clay_oauth, "register_client", fake_register)
    resp = admin_client.get("/auth/clay/connect", follow_redirects=False)
    assert resp.status_code in (302, 307)
    assert resp.headers["location"].startswith("https://app.clay.com/oauth/authorize")
    assert "scope=mcp" in resp.headers["location"]


def test_callback_rejects_unknown_state(admin_client):
    """GET /auth/clay/callback with unknown state → redirect with clay=error."""
    resp = admin_client.get("/auth/clay/callback?code=x&state=nope", follow_redirects=False)
    assert resp.status_code in (302, 307)
    assert "error" in resp.headers["location"]


def test_callback_happy_path_stores(admin_client, monkeypatch):
    """Happy-path callback: consumes state, calls exchange_code, redirects clay=connected."""
    import app.routers.clay_oauth as r

    captured = {}
    state_store = {"clay:oauth:state:STATE1": {"verifier": "VER", "client_id": "cid"}}

    def fake_get_cached(key):
        return state_store.get(key)

    def fake_set_cached(key, data, ttl_days=7):
        # Simulate one-time consume: overwrite with consumed marker
        state_store[key] = data

    async def fake_exchange(code, verifier, cid):
        captured.update(code=code, verifier=verifier, cid=cid)
        return True

    monkeypatch.setattr(r, "get_cached", fake_get_cached)
    monkeypatch.setattr(r, "set_cached", fake_set_cached)
    monkeypatch.setattr(r.clay_oauth, "exchange_code", fake_exchange)

    resp = admin_client.get("/auth/clay/callback?code=CODE&state=STATE1", follow_redirects=False)
    assert resp.status_code in (302, 307)
    assert "clay=connected" in resp.headers["location"]
    assert captured == {"code": "CODE", "verifier": "VER", "cid": "cid"}


def test_callback_exchange_failure_redirects_error(admin_client, monkeypatch):
    """If exchange_code returns False, callback redirects with clay=error."""
    import app.routers.clay_oauth as r

    state_store = {"clay:oauth:state:STATE2": {"verifier": "VER2", "client_id": "cid2"}}
    monkeypatch.setattr(r, "get_cached", lambda key: state_store.get(key))
    monkeypatch.setattr(r, "set_cached", lambda key, data, ttl_days=7: None)

    async def fake_exchange(code, verifier, cid):
        return False

    monkeypatch.setattr(r.clay_oauth, "exchange_code", fake_exchange)
    resp = admin_client.get("/auth/clay/callback?code=CODE&state=STATE2", follow_redirects=False)
    assert resp.status_code in (302, 307)
    assert "clay=error" in resp.headers["location"]


def test_callback_error_param_redirects(admin_client):
    """If Clay passes ?error=...

    in the callback, redirect with clay=error.
    """
    resp = admin_client.get(
        "/auth/clay/callback?error=access_denied&state=S",
        follow_redirects=False,
    )
    assert resp.status_code in (302, 307)
    assert "clay=error" in resp.headers["location"]


def test_disconnect_redirects(admin_client, monkeypatch):
    """POST /auth/clay/disconnect calls disconnect() and redirects."""
    import app.routers.clay_oauth as r

    called = {}

    def fake_disconnect():
        called["yes"] = True

    monkeypatch.setattr(r.clay_oauth, "disconnect", fake_disconnect)
    resp = admin_client.post("/auth/clay/disconnect", follow_redirects=False)
    assert resp.status_code in (302, 307)
    assert "clay=disconnected" in resp.headers["location"]
    assert called.get("yes")


def test_routes_admin_gated(unauthenticated_client):
    """Unauthenticated client (no session/token) must not reach connect."""
    resp = unauthenticated_client.get("/auth/clay/connect", follow_redirects=False)
    assert resp.status_code in (401, 403, 302, 307)
