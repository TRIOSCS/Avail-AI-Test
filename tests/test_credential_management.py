"""Tests for credential management: encryption, API endpoints, access control."""

import os
os.environ["TESTING"] = "1"

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import ApiSource, User


# ── Helpers ──────────────────────────────────────────────────────────

def _make_admin_client(db_session, admin_user):
    """Return a TestClient authenticated as admin."""
    from app.database import get_db
    from app.dependencies import require_user, require_admin, require_settings_access
    from app.main import app

    def _db():
        yield db_session

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[require_user] = lambda: admin_user
    app.dependency_overrides[require_admin] = lambda: admin_user
    app.dependency_overrides[require_settings_access] = lambda: admin_user

    client = TestClient(app)
    yield client
    app.dependency_overrides.clear()


def _seed_source(db_session) -> ApiSource:
    """Create a test API source."""
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


@pytest.fixture()
def admin_client(db_session, admin_user):
    yield from _make_admin_client(db_session, admin_user)


@pytest.fixture()
def test_source(db_session):
    return _seed_source(db_session)


# ── Encryption Tests ─────────────────────────────────────────────────

def test_encrypt_decrypt_roundtrip():
    """Encrypt then decrypt should return original value."""
    from app.services.credential_service import encrypt_value, decrypt_value
    original = "my-super-secret-api-key-12345"
    encrypted = encrypt_value(original)
    assert encrypted != original
    assert decrypt_value(encrypted) == original


def test_encrypt_produces_different_tokens():
    """Same plaintext should produce different ciphertexts (Fernet uses random IV)."""
    from app.services.credential_service import encrypt_value
    a = encrypt_value("same-key")
    b = encrypt_value("same-key")
    assert a != b


def test_mask_value_shows_last_4():
    from app.services.credential_service import mask_value
    assert mask_value("abcdefghijklmnop") == "●●●●●●●●mnop"
    assert mask_value("1234") == "****"
    assert mask_value("ab") == "****"
    assert mask_value("") == ""


# ── Credential API Tests ─────────────────────────────────────────────

def test_get_credentials_empty(admin_client, test_source):
    resp = admin_client.get(f"/api/admin/sources/{test_source.id}/credentials")
    assert resp.status_code == 200
    data = resp.json()
    assert data["source_name"] == "test_connector"
    creds = data["credentials"]
    assert creds["TEST_API_KEY"]["status"] == "empty"
    assert creds["TEST_API_SECRET"]["status"] == "empty"


def test_set_credentials(admin_client, test_source):
    resp = admin_client.put(
        f"/api/admin/sources/{test_source.id}/credentials",
        json={"TEST_API_KEY": "key123", "TEST_API_SECRET": "secret456"},
    )
    assert resp.status_code == 200
    assert set(resp.json()["updated"]) == {"TEST_API_KEY", "TEST_API_SECRET"}

    # Verify they show as set (masked)
    resp2 = admin_client.get(f"/api/admin/sources/{test_source.id}/credentials")
    creds = resp2.json()["credentials"]
    assert creds["TEST_API_KEY"]["status"] == "set"
    assert creds["TEST_API_KEY"]["source"] == "db"
    assert "y123" in creds["TEST_API_KEY"]["masked"]  # last 4 chars
    assert creds["TEST_API_SECRET"]["status"] == "set"


def test_delete_credential(admin_client, test_source):
    # Set first
    admin_client.put(
        f"/api/admin/sources/{test_source.id}/credentials",
        json={"TEST_API_KEY": "key123"},
    )
    # Delete
    resp = admin_client.delete(f"/api/admin/sources/{test_source.id}/credentials/TEST_API_KEY")
    assert resp.status_code == 200
    assert resp.json()["status"] == "removed"

    # Verify gone
    resp2 = admin_client.get(f"/api/admin/sources/{test_source.id}/credentials")
    assert resp2.json()["credentials"]["TEST_API_KEY"]["status"] == "empty"


def test_set_invalid_source_404(admin_client):
    resp = admin_client.put(
        "/api/admin/sources/99999/credentials",
        json={"FOO": "bar"},
    )
    assert resp.status_code == 404


def test_set_ignores_unknown_vars(admin_client, test_source):
    resp = admin_client.put(
        f"/api/admin/sources/{test_source.id}/credentials",
        json={"UNKNOWN_VAR": "value"},
    )
    assert resp.status_code == 200
    assert resp.json()["updated"] == []


# ── Access Control Tests ─────────────────────────────────────────────

def test_buyer_cannot_access_credentials(client, db_session):
    """Non-admin (buyer) should be denied."""
    src = _seed_source(db_session)
    resp = client.get(f"/api/admin/sources/{src.id}/credentials")
    assert resp.status_code in (401, 403)


def test_buyer_cannot_set_credentials(client, db_session):
    src = _seed_source(db_session)
    resp = client.put(
        f"/api/admin/sources/{src.id}/credentials",
        json={"TEST_API_KEY": "sneaky"},
    )
    assert resp.status_code in (401, 403)


# ── Credential Lookup Tests ──────────────────────────────────────────

def test_get_credential_from_db(db_session, test_source):
    """DB credential takes priority over env var."""
    from app.services.credential_service import encrypt_value, get_credential
    # Store encrypted credential in DB
    test_source.credentials = {"TEST_API_KEY": encrypt_value("db-value")}
    db_session.commit()

    result = get_credential(db_session, "test_connector", "TEST_API_KEY")
    assert result == "db-value"


def test_get_credential_fallback_to_env(db_session, test_source, monkeypatch):
    """Falls back to env var when DB has no credential."""
    from app.services.credential_service import get_credential
    monkeypatch.setenv("TEST_API_KEY", "env-value")
    result = get_credential(db_session, "test_connector", "TEST_API_KEY")
    assert result == "env-value"


def test_credential_is_set_db(db_session, test_source):
    from app.services.credential_service import encrypt_value, credential_is_set
    test_source.credentials = {"TEST_API_KEY": encrypt_value("something")}
    db_session.commit()
    assert credential_is_set(db_session, "test_connector", "TEST_API_KEY") is True
    assert credential_is_set(db_session, "test_connector", "TEST_API_SECRET") is False
