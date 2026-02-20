"""Tests for dev_assistant role: credential access, toggle, and security boundaries."""

import os

os.environ["TESTING"] = "1"

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import ApiSource, User

# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture()
def dev_assistant_user(db_session: Session) -> User:
    user = User(
        email="devsetup@trioscs.com",
        name="Dev Assistant",
        role="dev_assistant",
        azure_id="test-azure-id-dev",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture()
def dev_assistant_client(db_session: Session, dev_assistant_user: User):
    from fastapi import HTTPException

    from app.database import get_db
    from app.dependencies import require_admin, require_settings_access, require_user
    from app.main import app

    def _db():
        yield db_session

    def _deny_admin():
        raise HTTPException(403, "Admin access required")

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[require_user] = lambda: dev_assistant_user
    app.dependency_overrides[require_settings_access] = lambda: dev_assistant_user
    app.dependency_overrides[require_admin] = _deny_admin

    with TestClient(app) as c:
        yield c

    app.dependency_overrides.clear()


@pytest.fixture()
def test_source(db_session: Session) -> ApiSource:
    src = ApiSource(
        name="test_connector",
        display_name="Test Connector",
        category="api",
        source_type="aggregator",
        status="pending",
        env_vars=["TEST_API_KEY", "TEST_API_SECRET"],
        credentials={},
    )
    db_session.add(src)
    db_session.commit()
    db_session.refresh(src)
    return src


# ── Dev Assistant Access Tests ────────────────────────────────────────


def test_dev_assistant_can_get_credentials(dev_assistant_client, test_source):
    resp = dev_assistant_client.get(f"/api/admin/sources/{test_source.id}/credentials")
    assert resp.status_code == 200
    data = resp.json()
    assert data["source_name"] == "test_connector"
    assert "TEST_API_KEY" in data["credentials"]


def test_dev_assistant_can_set_credentials(dev_assistant_client, test_source, db_session):
    resp = dev_assistant_client.put(
        f"/api/admin/sources/{test_source.id}/credentials",
        json={"TEST_API_KEY": "dev-key-123", "TEST_API_SECRET": "dev-secret-456"},
    )
    assert resp.status_code == 200
    assert set(resp.json()["updated"]) == {"TEST_API_KEY", "TEST_API_SECRET"}

    # Verify encrypted in DB (not plaintext)
    db_session.refresh(test_source)
    stored = test_source.credentials or {}
    assert "TEST_API_KEY" in stored
    assert stored["TEST_API_KEY"] != "dev-key-123"  # encrypted, not plaintext


def test_dev_assistant_can_delete_credential(dev_assistant_client, test_source):
    # Set first
    dev_assistant_client.put(
        f"/api/admin/sources/{test_source.id}/credentials",
        json={"TEST_API_KEY": "to-be-deleted"},
    )
    # Delete
    resp = dev_assistant_client.delete(
        f"/api/admin/sources/{test_source.id}/credentials/TEST_API_KEY"
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "removed"


def test_dev_assistant_can_toggle_source(dev_assistant_client, test_source):
    resp = dev_assistant_client.put(
        f"/api/sources/{test_source.id}/toggle",
        json={"status": "disabled"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "disabled"


# ── Security Boundary Tests ──────────────────────────────────────────


def test_dev_assistant_cannot_manage_users(dev_assistant_client):
    """Dev assistant must not access user management endpoints."""
    resp = dev_assistant_client.get("/api/admin/users")
    assert resp.status_code == 403


def test_dev_assistant_cannot_write_config(dev_assistant_client):
    """Dev assistant must not write system config."""
    resp = dev_assistant_client.put(
        "/api/admin/config/some_key",
        json={"value": "sneaky"},
    )
    assert resp.status_code == 403


def test_buyer_cannot_access_credentials(db_session, test_user, test_source):
    """Buyer role should be denied credential endpoints."""
    from fastapi import HTTPException

    from app.database import get_db
    from app.dependencies import require_settings_access, require_user
    from app.main import app

    def _db():
        yield db_session

    def _deny_settings():
        raise HTTPException(403, "Settings access required")

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[require_user] = lambda: test_user
    app.dependency_overrides[require_settings_access] = _deny_settings

    with TestClient(app) as c:
        resp = c.get(f"/api/admin/sources/{test_source.id}/credentials")
        assert resp.status_code == 403

    app.dependency_overrides.clear()


def test_sales_cannot_access_credentials(db_session, sales_user, test_source):
    """Sales role should be denied credential endpoints."""
    from fastapi import HTTPException

    from app.database import get_db
    from app.dependencies import require_settings_access, require_user
    from app.main import app

    def _db():
        yield db_session

    def _deny_settings():
        raise HTTPException(403, "Settings access required")

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[require_user] = lambda: sales_user
    app.dependency_overrides[require_settings_access] = _deny_settings

    with TestClient(app) as c:
        resp = c.get(f"/api/admin/sources/{test_source.id}/credentials")
        assert resp.status_code == 403

    app.dependency_overrides.clear()
