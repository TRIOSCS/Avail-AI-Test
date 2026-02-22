"""Tests for admin settings: roles, permissions, config, health."""

import os

os.environ["TESTING"] = "1"

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.models import SystemConfig, User

# ── Helper: create client with specific user role ────────────────────

def _make_client(db_session, user):
    """Return a TestClient authenticated as the given user."""
    from app.database import get_db
    from app.dependencies import require_admin, require_buyer, require_settings_access, require_user
    from app.main import app

    def _override_db():
        yield db_session

    def _override_user():
        return user

    def _override_buyer():
        if user.role in ("sales", "dev_assistant"):
            from fastapi import HTTPException
            raise HTTPException(403, "Buyer role required")
        return user

    def _override_admin():
        if user.role != "admin":
            from fastapi import HTTPException
            raise HTTPException(403, "Admin access required")
        return user

    def _override_settings():
        if user.role not in ("admin", "dev_assistant"):
            from fastapi import HTTPException
            raise HTTPException(403, "Settings access required")
        return user

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_user] = _override_user
    app.dependency_overrides[require_buyer] = _override_buyer
    app.dependency_overrides[require_admin] = _override_admin
    app.dependency_overrides[require_settings_access] = _override_settings

    client = TestClient(app)
    yield client
    app.dependency_overrides.clear()


@pytest.fixture()
def admin_client(db_session, admin_user):
    yield from _make_client(db_session, admin_user)


@pytest.fixture()
def buyer_client(db_session, test_user):
    yield from _make_client(db_session, test_user)


@pytest.fixture()
def dev_assistant_user(db_session):
    user = User(
        email="devbot@trioscs.com",
        name="Dev Bot",
        role="dev_assistant",
        azure_id="test-azure-dev",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture()
def dev_client(db_session, dev_assistant_user):
    yield from _make_client(db_session, dev_assistant_user)


@pytest.fixture()
def seed_config(db_session):
    """Seed system_config with test data."""
    configs = [
        SystemConfig(key="email_mining_enabled", value="false", description="Test toggle"),
        SystemConfig(key="inbox_scan_interval_min", value="30", description="Test interval"),
    ]
    for c in configs:
        db_session.add(c)
    db_session.commit()


# ── Access Control Tests ──────────────────────────────────────────────

def test_require_admin_rejects_buyer(buyer_client):
    resp = buyer_client.get("/api/admin/users")
    assert resp.status_code == 403


def test_require_admin_rejects_dev_assistant(dev_client):
    resp = dev_client.get("/api/admin/users")
    assert resp.status_code == 403


def test_require_admin_allows_admin(admin_client, admin_user):
    resp = admin_client.get("/api/admin/users")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert any(u["email"] == admin_user.email for u in data)


def test_require_settings_allows_dev_assistant(dev_client, seed_config):
    # /api/admin/health should work for dev_assistant
    resp = dev_client.get("/api/admin/health")
    assert resp.status_code == 200
    data = resp.json()
    assert "version" in data

    # /api/admin/config GET should also work
    resp = dev_client.get("/api/admin/config")
    assert resp.status_code == 200


def test_dev_assistant_cannot_write_config(dev_client, seed_config):
    resp = dev_client.put("/api/admin/config/email_mining_enabled",
                          json={"value": "true"})
    assert resp.status_code == 403


# ── User Management Tests ────────────────────────────────────────────

def test_list_users(admin_client, admin_user, test_user):
    resp = admin_client.get("/api/admin/users")
    assert resp.status_code == 200
    data = resp.json()
    emails = [u["email"] for u in data]
    assert admin_user.email in emails
    assert test_user.email in emails


def test_update_user_role(admin_client, db_session, test_user):
    resp = admin_client.put(f"/api/admin/users/{test_user.id}",
                            json={"role": "sales"})
    assert resp.status_code == 200
    assert resp.json()["role"] == "sales"
    db_session.refresh(test_user)
    assert test_user.role == "sales"


def test_cannot_deactivate_self(admin_client, admin_user):
    resp = admin_client.put(f"/api/admin/users/{admin_user.id}",
                            json={"is_active": False})
    assert resp.status_code == 400
    assert "yourself" in resp.json()["error"].lower()


def test_cannot_change_own_role(admin_client, admin_user):
    resp = admin_client.put(f"/api/admin/users/{admin_user.id}",
                            json={"role": "buyer"})
    assert resp.status_code == 400
    assert "own role" in resp.json()["error"].lower()


def test_deactivate_other_user(admin_client, db_session, test_user):
    resp = admin_client.put(f"/api/admin/users/{test_user.id}",
                            json={"is_active": False})
    assert resp.status_code == 200
    assert resp.json()["is_active"] is False


# ── Config Tests ─────────────────────────────────────────────────────

def test_get_config(admin_client, seed_config):
    resp = admin_client.get("/api/admin/config")
    assert resp.status_code == 200
    data = resp.json()
    keys = [c["key"] for c in data]
    assert "email_mining_enabled" in keys
    assert "inbox_scan_interval_min" in keys


def test_set_config(admin_client, db_session, seed_config, admin_user):
    resp = admin_client.put("/api/admin/config/inbox_scan_interval_min",
                            json={"value": "15"})
    assert resp.status_code == 200
    assert resp.json()["value"] == "15"
    assert resp.json()["updated_by"] == admin_user.email


# ── Health Endpoint Test ─────────────────────────────────────────────

def test_health_endpoint(admin_client):
    resp = admin_client.get("/api/admin/health")
    assert resp.status_code == 200
    data = resp.json()
    assert "version" in data
    assert "db_stats" in data
    assert "scheduler" in data
    assert "connectors" in data
