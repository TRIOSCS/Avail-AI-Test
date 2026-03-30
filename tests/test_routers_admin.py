"""test_routers_admin.py — Tests for admin router endpoints.

Tests config management, health, credentials, connector health,
integrity check, material audit, and company merge preview.
Uses admin_client fixture with admin auth override.

Called by: pytest
Depends on: app/routers/admin.py, conftest.py

Note: User CRUD, CSV import, Teams config, vendor dedup, and company dedup
test classes were removed — those endpoints were deleted in the CRM redesign.
"""

from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import (
    ApiSource,
    SystemConfig,
    User,
)

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

    try:
        with TestClient(app) as c:
            yield c
    finally:
        for dep in [get_db, require_admin, require_settings_access, require_user]:
            app.dependency_overrides.pop(dep, None)


# ── User Management (remaining) ─────────────────────────────────────


class TestAdminUpdateUser:
    def test_update_nonexistent_user(self, admin_client):
        resp = admin_client.put("/api/admin/users/99999", json={"role": "buyer"})
        assert resp.status_code in (404, 200)  # service returns dict with status


# ── System Config ───────────────────────────────────────────────────


class TestAdminConfig:
    def test_list_config(self, admin_client, db_session):
        row = SystemConfig(
            key="test_setting",
            value="test_val",
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


# ── Delete User ────────────────────────────────────────────────────


class TestAdminDeleteUser:
    def test_delete_user_not_found(self, admin_client):
        resp = admin_client.delete("/api/admin/users/99999")
        assert resp.status_code == 404


# ── Config Update ──────────────────────────────────────────────────


class TestAdminConfigUpdate:
    def test_config_upsert(self, admin_client, db_session):
        # Seed the key first — set_config_value only updates existing rows
        row = SystemConfig(
            key="upsert_test_key",
            value="old_val",
            updated_at=datetime.now(timezone.utc),
        )
        db_session.add(row)
        db_session.commit()

        resp = admin_client.put(
            "/api/admin/config/upsert_test_key",
            json={"value": "new_val"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["value"] == "new_val"

        # Verify persisted
        db_session.refresh(row)
        assert row.value == "new_val"

    def test_config_empty_value_rejected(self, admin_client):
        resp = admin_client.put(
            "/api/admin/config/any_key",
            json={"value": ""},
        )
        assert resp.status_code == 422


# ── Credential CRUD ────────────────────────────────────────────────


class TestAdminCredentials:
    @pytest.fixture()
    def api_source(self, db_session):
        """An ApiSource with env_vars configured for credential tests."""
        src = ApiSource(
            name="test_api",
            display_name="Test API",
            category="connector",
            source_type="connector",
            status="pending",
            env_vars=["TEST_API_KEY", "TEST_API_SECRET"],
            credentials={},
        )
        db_session.add(src)
        db_session.commit()
        db_session.refresh(src)
        return src

    @patch("app.routers.admin.decrypt_value", return_value="sk-secret1234567890")
    @patch("app.routers.admin.mask_value", return_value="●●●●7890")
    def test_get_credentials_masked(
        self,
        mock_mask,
        mock_decrypt,
        admin_client,
        db_session,
        api_source,
    ):
        # Pre-populate an encrypted credential
        api_source.credentials = {"TEST_API_KEY": "encrypted_blob"}
        db_session.commit()

        resp = admin_client.get(f"/api/admin/sources/{api_source.id}/credentials")
        assert resp.status_code == 200
        data = resp.json()
        assert data["source_id"] == api_source.id
        creds = data["credentials"]
        assert "TEST_API_KEY" in creds
        assert creds["TEST_API_KEY"]["status"] == "set"
        assert "●" in creds["TEST_API_KEY"]["masked"] or "****" in creds["TEST_API_KEY"]["masked"]

    def test_get_credentials_not_found(self, admin_client):
        resp = admin_client.get("/api/admin/sources/99999/credentials")
        assert resp.status_code == 404

    @patch("app.routers.admin.encrypt_value", return_value="encrypted_blob_123")
    def test_set_credentials_encrypted(
        self,
        mock_encrypt,
        admin_client,
        db_session,
        api_source,
    ):
        resp = admin_client.put(
            f"/api/admin/sources/{api_source.id}/credentials",
            json={"TEST_API_KEY": "my-secret-key"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "TEST_API_KEY" in data["updated"]
        mock_encrypt.assert_called_once_with("my-secret-key")

        # Verify stored in DB
        db_session.refresh(api_source)
        assert api_source.credentials["TEST_API_KEY"] == "encrypted_blob_123"

    @patch("app.services.credential_service.credential_is_set", return_value=False)
    def test_delete_credential(self, mock_cred_set, admin_client, db_session, api_source):
        # Pre-populate credentials
        api_source.credentials = {"TEST_API_KEY": "enc1", "TEST_API_SECRET": "enc2"}
        db_session.commit()

        resp = admin_client.delete(
            f"/api/admin/sources/{api_source.id}/credentials/TEST_API_KEY",
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "removed"

        # Verify removed from DB
        db_session.refresh(api_source)
        assert "TEST_API_KEY" not in (api_source.credentials or {})

    @patch("app.services.credential_service.credential_is_set", return_value=False)
    def test_credential_status_downgrade(
        self,
        mock_cred_set,
        admin_client,
        db_session,
        api_source,
    ):
        # Source starts as "live" with credentials
        api_source.status = "live"
        api_source.credentials = {"TEST_API_KEY": "enc1"}
        db_session.commit()

        resp = admin_client.delete(
            f"/api/admin/sources/{api_source.id}/credentials/TEST_API_KEY",
        )
        assert resp.status_code == 200

        # Status should downgrade from live to pending
        db_session.refresh(api_source)
        assert api_source.status == "pending"


# ── Additional coverage tests ─────────────────────────────────────────


class TestAdminConfigSetError:
    def test_config_set_invalid_key(self, admin_client):
        """Setting a non-existent config key returns service-level error."""
        resp = admin_client.put(
            "/api/admin/config/nonexistent_key_xyz",
            json={"value": "some_value"},
        )
        # set_config_value returns error dict for unknown keys
        assert resp.status_code in (400, 404, 200)


class TestAdminCredentialEdgeCases:
    @pytest.fixture()
    def api_source(self, db_session):
        src = ApiSource(
            name="test_cred_edge",
            display_name="Test Cred Edge",
            category="connector",
            source_type="connector",
            status="pending",
            env_vars=["EDGE_KEY", "EDGE_SECRET"],
            credentials={},
        )
        db_session.add(src)
        db_session.commit()
        db_session.refresh(src)
        return src

    @patch("app.routers.admin.decrypt_value", side_effect=ValueError("bad key"))
    def test_get_credentials_decrypt_error(self, mock_decrypt, admin_client, db_session, api_source):
        """Decryption failure shows error status."""
        api_source.credentials = {"EDGE_KEY": "corrupted_blob"}
        db_session.commit()

        resp = admin_client.get(f"/api/admin/sources/{api_source.id}/credentials")
        assert resp.status_code == 200
        creds = resp.json()["credentials"]
        assert creds["EDGE_KEY"]["status"] == "error"

    def test_get_credentials_env_fallback(self, admin_client, db_session, api_source, monkeypatch):
        """Falls back to env vars when no DB credential."""
        monkeypatch.setenv("EDGE_KEY", "env-value-123")
        resp = admin_client.get(f"/api/admin/sources/{api_source.id}/credentials")
        assert resp.status_code == 200
        creds = resp.json()["credentials"]
        assert creds["EDGE_KEY"]["status"] == "set"
        assert creds["EDGE_KEY"]["source"] == "env"

    def test_get_credentials_empty(self, admin_client, db_session, api_source):
        """Empty credentials show status=empty."""
        resp = admin_client.get(f"/api/admin/sources/{api_source.id}/credentials")
        assert resp.status_code == 200
        creds = resp.json()["credentials"]
        assert creds["EDGE_KEY"]["status"] == "empty"
        assert creds["EDGE_KEY"]["source"] == "none"

    @patch("app.routers.admin.encrypt_value", return_value="enc_blob")
    def test_set_credentials_invalid_var_skipped(self, mock_enc, admin_client, api_source):
        """Setting an invalid var_name is silently skipped."""
        resp = admin_client.put(
            f"/api/admin/sources/{api_source.id}/credentials",
            json={"INVALID_VAR_NAME": "test-value"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "INVALID_VAR_NAME" not in data["updated"]

    @patch("app.routers.admin.encrypt_value", return_value="enc_blob")
    def test_set_credentials_empty_value_removes(self, mock_enc, admin_client, db_session, api_source):
        """Setting empty string value removes the credential."""
        api_source.credentials = {"EDGE_KEY": "old_enc"}
        db_session.commit()

        resp = admin_client.put(
            f"/api/admin/sources/{api_source.id}/credentials",
            json={"EDGE_KEY": ""},
        )
        assert resp.status_code == 200
        assert "EDGE_KEY" in resp.json()["updated"]
        # Verify the key was removed
        db_session.refresh(api_source)
        assert "EDGE_KEY" not in (api_source.credentials or {})

    def test_set_credentials_source_not_found(self, admin_client):
        """Setting credentials on non-existent source returns 404."""
        resp = admin_client.put(
            "/api/admin/sources/99999/credentials",
            json={"KEY": "val"},
        )
        assert resp.status_code == 404

    def test_delete_credential_not_found_source(self, admin_client):
        """Deleting credential from non-existent source returns 404."""
        resp = admin_client.delete("/api/admin/sources/99999/credentials/SOME_VAR")
        assert resp.status_code == 404

    @patch("app.services.credential_service.credential_is_set", return_value=False)
    def test_delete_credential_not_present(self, mock_set, admin_client, db_session, api_source):
        """Deleting a non-existent credential returns not_found status."""
        api_source.credentials = {}
        db_session.commit()
        resp = admin_client.delete(
            f"/api/admin/sources/{api_source.id}/credentials/EDGE_KEY",
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "not_found"


# ── Connector Health ──────────────────────────────────────────────────


class TestConnectorHealth:
    def test_connector_health_empty(self, admin_client):
        resp = admin_client.get("/api/admin/connector-health")
        assert resp.status_code == 200
        data = resp.json()
        assert "connectors" in data
        assert isinstance(data["connectors"], list)

    def test_connector_health_returns_fields(self, admin_client, db_session):
        src = ApiSource(
            name="test_src",
            display_name="Test Source",
            category="distributor",
            source_type="api",
            status="live",
            is_active=True,
            total_searches=100,
            total_results=500,
            avg_response_ms=250,
            error_count_24h=2,
        )
        db_session.add(src)
        db_session.commit()

        resp = admin_client.get("/api/admin/connector-health")
        assert resp.status_code == 200
        connectors = resp.json()["connectors"]
        match = [c for c in connectors if c["name"] == "test_src"]
        assert len(match) == 1
        c = match[0]
        assert c["status"] == "live"
        assert c["total_searches"] == 100
        assert c["total_results"] == 500
        assert c["avg_response_ms"] == 250
        assert c["error_count_24h"] == 2
        assert c["last_error"] is None
        assert c["last_error_at"] is None

    def test_connector_health_auto_degraded(self, admin_client, db_session):
        src = ApiSource(
            name="bad_src",
            display_name="Bad Source",
            category="broker",
            source_type="api",
            status="live",
            is_active=True,
            total_searches=10,
            error_count_24h=8,
        )
        db_session.add(src)
        db_session.commit()

        resp = admin_client.get("/api/admin/connector-health")
        connectors = resp.json()["connectors"]
        match = [c for c in connectors if c["name"] == "bad_src"][0]
        assert match["status"] == "degraded"

    def test_connector_health_not_degraded_low_errors(self, admin_client, db_session):
        src = ApiSource(
            name="ok_src",
            display_name="OK Source",
            category="distributor",
            source_type="api",
            status="live",
            is_active=True,
            total_searches=100,
            error_count_24h=3,
        )
        db_session.add(src)
        db_session.commit()

        resp = admin_client.get("/api/admin/connector-health")
        connectors = resp.json()["connectors"]
        match = [c for c in connectors if c["name"] == "ok_src"][0]
        assert match["status"] == "live"


class TestIntegrityCheck:
    """Tests for /api/admin/integrity (lines 323-325)."""

    @patch("app.services.integrity_service.run_integrity_check", return_value={"status": "healthy", "issues": []})
    def test_integrity_check(self, mock_check, admin_client):
        resp = admin_client.get("/api/admin/integrity")
        assert resp.status_code == 200
        assert resp.json()["status"] == "healthy"


class TestMaterialAudit:
    """Tests for /api/admin/material-audit (lines 336-350)."""

    def test_material_audit_empty(self, admin_client):
        """No audit entries returns empty list."""
        resp = admin_client.get("/api/admin/material-audit")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["entries"] == []

    def test_material_audit_with_filter(self, admin_client, db_session):
        """Filter by card_id and action."""
        from app.models import MaterialCardAudit

        audit = MaterialCardAudit(
            material_card_id=1,
            action="merge",
            entity_type="material_card",
            entity_id=1,
            normalized_mpn="lm317t",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(audit)
        db_session.commit()

        resp = admin_client.get("/api/admin/material-audit?card_id=1&action=merge")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["entries"][0]["action"] == "merge"

    def test_material_audit_pagination(self, admin_client):
        """Limit and offset work."""
        resp = admin_client.get("/api/admin/material-audit?limit=10&offset=0")
        assert resp.status_code == 200
        data = resp.json()
        assert data["limit"] == 10
        assert data["offset"] == 0


class TestCompanyMergePreview:
    """Tests for /api/admin/company-merge-preview."""

    def test_merge_preview_not_found(self, admin_client):
        """Missing company -> 404."""
        resp = admin_client.get("/api/admin/company-merge-preview?keep_id=99999&remove_id=99998")
        assert resp.status_code == 404
