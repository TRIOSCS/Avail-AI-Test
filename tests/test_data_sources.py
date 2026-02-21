"""
tests/test_data_sources.py — Tests for the Data Sources settings tab

Covers: source listing, credential CRUD, status toggling, test endpoint,
and planned vs configurable source separation.

Called by: pytest
Depends on: app/routers/sources.py, app/routers/admin.py, conftest.py
"""

import os

os.environ["TESTING"] = "1"

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.models import ApiSource, User


# ── Helper: create client with admin + settings access ────────────────


def _make_admin_client(db_session, user):
    from app.database import get_db
    from app.dependencies import require_admin, require_settings_access, require_user
    from app.main import app

    def _override_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_user] = lambda: user
    app.dependency_overrides[require_settings_access] = lambda: user
    app.dependency_overrides[require_admin] = lambda: user

    client = TestClient(app)
    yield client
    app.dependency_overrides.clear()


@pytest.fixture()
def admin_source_client(db_session, admin_user):
    yield from _make_admin_client(db_session, admin_user)


@pytest.fixture()
def seed_sources(db_session):
    """Seed a mix of configurable and planned sources."""
    sources = [
        ApiSource(
            name="nexar",
            display_name="Octopart (Nexar)",
            category="api",
            source_type="aggregator",
            status="live",
            description="GraphQL API for parts search",
            env_vars=["NEXAR_CLIENT_ID", "NEXAR_CLIENT_SECRET"],
            credentials={},
            total_searches=150,
            total_results=4200,
            avg_response_ms=320,
            created_at=datetime.now(timezone.utc),
        ),
        ApiSource(
            name="mouser",
            display_name="Mouser",
            category="api",
            source_type="authorized",
            status="pending",
            description="Search API v2",
            env_vars=["MOUSER_API_KEY"],
            credentials={},
            created_at=datetime.now(timezone.utc),
        ),
        ApiSource(
            name="arrow",
            display_name="Arrow Electronics",
            category="api",
            source_type="authorized",
            status="pending",
            description="Major authorized distributor",
            env_vars=[],
            credentials={},
            created_at=datetime.now(timezone.utc),
        ),
        ApiSource(
            name="netcomponents",
            display_name="NetComponents",
            category="scraper",
            source_type="broker",
            status="pending",
            description="60M+ line items from suppliers",
            env_vars=[],
            credentials={},
            created_at=datetime.now(timezone.utc),
        ),
    ]
    for s in sources:
        db_session.add(s)
    db_session.commit()
    return sources


# ── GET /api/sources ─────────────────────────────────────────────────


def test_list_sources(client, seed_sources):
    """GET /api/sources returns all sources with correct structure."""
    resp = client.get("/api/sources")
    assert resp.status_code == 200
    data = resp.json()
    assert "sources" in data
    sources = data["sources"]
    assert len(sources) == 4

    # Check structure of a configurable source
    nexar = next(s for s in sources if s["name"] == "nexar")
    assert nexar["display_name"] == "Octopart (Nexar)"
    assert nexar["category"] == "api"
    assert nexar["env_vars"] == ["NEXAR_CLIENT_ID", "NEXAR_CLIENT_SECRET"]
    assert "env_status" in nexar
    assert "credentials_masked" in nexar
    assert nexar["total_searches"] == 150


def test_list_sources_planned_have_no_env_vars(client, seed_sources):
    """Planned sources (Arrow, NetComponents) have empty env_vars."""
    resp = client.get("/api/sources")
    sources = resp.json()["sources"]
    arrow = next(s for s in sources if s["name"] == "arrow")
    assert arrow["env_vars"] == []
    assert arrow["credentials_masked"] == {}


def test_list_sources_credential_masking(client, db_session, seed_sources):
    """Credentials stored in DB show masked values."""
    from app.services.credential_service import encrypt_value

    mouser = db_session.query(ApiSource).filter_by(name="mouser").first()
    mouser.credentials = {"MOUSER_API_KEY": encrypt_value("sk-test-12345678")}
    db_session.commit()

    resp = client.get("/api/sources")
    sources = resp.json()["sources"]
    mouser_data = next(s for s in sources if s["name"] == "mouser")
    assert mouser_data["env_status"]["MOUSER_API_KEY"] is True
    assert mouser_data["credentials_masked"]["MOUSER_API_KEY"] != ""
    # Masked value should end with last 4 chars
    assert mouser_data["credentials_masked"]["MOUSER_API_KEY"].endswith("5678")


def test_list_sources_auto_status_live(client, db_session, seed_sources):
    """Source with all credentials set auto-promotes from pending to live."""
    from app.services.credential_service import encrypt_value

    mouser = db_session.query(ApiSource).filter_by(name="mouser").first()
    assert mouser.status == "pending"
    mouser.credentials = {"MOUSER_API_KEY": encrypt_value("test-key")}
    db_session.commit()

    resp = client.get("/api/sources")
    sources = resp.json()["sources"]
    mouser_data = next(s for s in sources if s["name"] == "mouser")
    assert mouser_data["status"] == "live"


# ── PUT /api/sources/{id}/toggle ─────────────────────────────────────


def test_toggle_disable_source(admin_source_client, db_session, seed_sources):
    """Disabling a source sets status to disabled."""
    nexar = db_session.query(ApiSource).filter_by(name="nexar").first()
    resp = admin_source_client.put(f"/api/sources/{nexar.id}/toggle", json={"status": "disabled"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "disabled"


def test_toggle_enable_with_creds_goes_live(admin_source_client, db_session, seed_sources):
    """Enabling a source with credentials set goes to live, not pending."""
    from app.services.credential_service import encrypt_value

    mouser = db_session.query(ApiSource).filter_by(name="mouser").first()
    mouser.credentials = {"MOUSER_API_KEY": encrypt_value("test-key")}
    mouser.status = "disabled"
    db_session.commit()

    resp = admin_source_client.put(f"/api/sources/{mouser.id}/toggle", json={"status": "live"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "live"


def test_toggle_enable_without_creds_goes_pending(admin_source_client, db_session, seed_sources):
    """Enabling a source without credentials set goes to pending."""
    mouser = db_session.query(ApiSource).filter_by(name="mouser").first()
    mouser.status = "disabled"
    db_session.commit()

    resp = admin_source_client.put(f"/api/sources/{mouser.id}/toggle", json={"status": "live"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "pending"


# ── POST /api/sources/{id}/test ──────────────────────────────────────


def test_source_test_no_connector(client, db_session, seed_sources):
    """Testing a source with no connector returns error."""
    arrow = db_session.query(ApiSource).filter_by(name="arrow").first()
    resp = client.post(f"/api/sources/{arrow.id}/test")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "error"
    assert "No connector" in (data["error"] or "")


def test_source_test_not_found(client):
    """Testing a nonexistent source returns 404."""
    resp = client.post("/api/sources/99999/test")
    assert resp.status_code == 404


# ── Credential CRUD (admin) ──────────────────────────────────────────


def test_save_credential(admin_source_client, db_session, seed_sources):
    """PUT /api/admin/sources/{id}/credentials saves encrypted credential."""
    mouser = db_session.query(ApiSource).filter_by(name="mouser").first()
    resp = admin_source_client.put(
        f"/api/admin/sources/{mouser.id}/credentials",
        json={"MOUSER_API_KEY": "my-secret-key"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert "MOUSER_API_KEY" in resp.json()["updated"]

    # Verify it's encrypted in DB
    db_session.refresh(mouser)
    assert mouser.credentials.get("MOUSER_API_KEY") != "my-secret-key"
    assert len(mouser.credentials["MOUSER_API_KEY"]) > 20  # encrypted


def test_delete_credential(admin_source_client, db_session, seed_sources):
    """DELETE /api/admin/sources/{id}/credentials/{var} removes credential."""
    from app.services.credential_service import encrypt_value

    mouser = db_session.query(ApiSource).filter_by(name="mouser").first()
    mouser.credentials = {"MOUSER_API_KEY": encrypt_value("test-key")}
    db_session.commit()

    resp = admin_source_client.delete(
        f"/api/admin/sources/{mouser.id}/credentials/MOUSER_API_KEY"
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "removed"

    db_session.refresh(mouser)
    assert "MOUSER_API_KEY" not in (mouser.credentials or {})


def test_delete_credential_updates_status(admin_source_client, db_session, seed_sources):
    """Deleting a credential downgrades live source to pending."""
    from app.services.credential_service import encrypt_value

    mouser = db_session.query(ApiSource).filter_by(name="mouser").first()
    mouser.credentials = {"MOUSER_API_KEY": encrypt_value("test-key")}
    mouser.status = "live"
    db_session.commit()

    admin_source_client.delete(
        f"/api/admin/sources/{mouser.id}/credentials/MOUSER_API_KEY"
    )
    db_session.refresh(mouser)
    assert mouser.status == "pending"


def test_get_credentials_masked(admin_source_client, db_session, seed_sources):
    """GET /api/admin/sources/{id}/credentials returns masked values."""
    from app.services.credential_service import encrypt_value

    mouser = db_session.query(ApiSource).filter_by(name="mouser").first()
    mouser.credentials = {"MOUSER_API_KEY": encrypt_value("sk-test-secret-12345678")}
    db_session.commit()

    resp = admin_source_client.get(f"/api/admin/sources/{mouser.id}/credentials")
    assert resp.status_code == 200
    data = resp.json()
    assert data["source_name"] == "mouser"
    cred = data["credentials"]["MOUSER_API_KEY"]
    assert cred["status"] == "set"
    assert cred["source"] == "db"
    assert cred["masked"].endswith("5678")
    assert "sk-test" not in cred["masked"]
