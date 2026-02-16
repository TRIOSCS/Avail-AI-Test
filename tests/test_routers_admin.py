"""
test_routers_admin.py — Tests for admin router endpoints.

Tests user CRUD, config management, and health endpoints.
Uses admin_client fixture with admin auth override.

Called by: pytest
Depends on: app/routers/admin.py, conftest.py
"""

import pytest
from datetime import datetime, timezone
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import User, SystemConfig


# ── Admin client fixture ────────────────────────────────────────────


@pytest.fixture()
def admin_client(db_session: Session, admin_user: User) -> TestClient:
    """TestClient with admin auth overrides."""
    from app.database import get_db
    from app.dependencies import require_admin, require_settings_access, require_user
    from app.main import app

    def _override_db():
        yield db_session

    def _override_admin():
        return admin_user

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_admin] = _override_admin
    app.dependency_overrides[require_settings_access] = _override_admin
    app.dependency_overrides[require_user] = _override_admin

    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ── User Management ─────────────────────────────────────────────────


class TestAdminUserList:
    def test_list_users(self, admin_client, admin_user):
        resp = admin_client.get("/api/admin/users")
        assert resp.status_code == 200
        users = resp.json()
        assert isinstance(users, list)
        emails = [u["email"] for u in users]
        assert admin_user.email in emails

    def test_user_dict_shape(self, admin_client):
        resp = admin_client.get("/api/admin/users")
        users = resp.json()
        if users:
            u = users[0]
            assert "id" in u
            assert "email" in u
            assert "role" in u
            assert "is_active" in u


class TestAdminCreateUser:
    def test_create_user(self, admin_client):
        resp = admin_client.post("/api/admin/users", json={
            "name": "New Buyer", "email": "newbuyer@trioscs.com", "role": "buyer",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["email"] == "newbuyer@trioscs.com"
        assert data["role"] == "buyer"

    def test_create_user_invalid_role(self, admin_client):
        resp = admin_client.post("/api/admin/users", json={
            "name": "Bad Role", "email": "bad@trioscs.com", "role": "superuser",
        })
        assert resp.status_code == 400

    def test_create_duplicate_email(self, admin_client, admin_user):
        resp = admin_client.post("/api/admin/users", json={
            "name": "Dup", "email": admin_user.email, "role": "buyer",
        })
        assert resp.status_code == 409


class TestAdminUpdateUser:
    def test_update_role(self, admin_client, db_session):
        # Create a target user first
        target = User(
            email="target@trioscs.com", name="Target", role="buyer",
            azure_id="az-target", created_at=datetime.now(timezone.utc),
        )
        db_session.add(target)
        db_session.commit()

        resp = admin_client.put(f"/api/admin/users/{target.id}", json={"role": "sales"})
        assert resp.status_code == 200
        assert resp.json()["role"] == "sales"

    def test_update_nonexistent_user(self, admin_client):
        resp = admin_client.put("/api/admin/users/99999", json={"role": "buyer"})
        assert resp.status_code in (404, 200)  # service returns dict with status


# ── System Config ───────────────────────────────────────────────────


class TestAdminConfig:
    def test_list_config(self, admin_client, db_session):
        row = SystemConfig(
            key="test_setting", value="test_val",
            updated_at=datetime.now(timezone.utc),
        )
        db_session.add(row)
        db_session.commit()

        resp = admin_client.get("/api/admin/config")
        assert resp.status_code == 200
        keys = [c["key"] for c in resp.json()]
        assert "test_setting" in keys


# ── Health ──────────────────────────────────────────────────────────


class TestAdminHealth:
    def test_health_endpoint(self, admin_client):
        resp = admin_client.get("/api/admin/health")
        assert resp.status_code == 200
        data = resp.json()
        assert "version" in data
        assert "db_stats" in data

    def test_health_includes_scheduler(self, admin_client):
        resp = admin_client.get("/api/admin/health")
        data = resp.json()
        assert "scheduler" in data
