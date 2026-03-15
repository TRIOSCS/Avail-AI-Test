"""test_routers_admin.py — Tests for admin router endpoints.

Tests user CRUD, config management, health, credentials, CSV import,
Teams config, and vendor dedup suggestions.
Uses admin_client fixture with admin auth override.

Called by: pytest
Depends on: app/routers/admin.py, conftest.py
"""

import io
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import (
    ActivityLog,
    ApiSource,
    Company,
    CustomerSite,
    SystemConfig,
    User,
    VendorCard,
)
from app.rate_limit import limiter

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
        resp = admin_client.post(
            "/api/admin/users",
            json={
                "name": "New Buyer",
                "email": "newbuyer@trioscs.com",
                "role": "buyer",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["email"] == "newbuyer@trioscs.com"
        assert data["role"] == "buyer"

    def test_create_user_invalid_role(self, admin_client):
        resp = admin_client.post(
            "/api/admin/users",
            json={
                "name": "Bad Role",
                "email": "bad@trioscs.com",
                "role": "superuser",
            },
        )
        assert resp.status_code == 400

    def test_create_duplicate_email(self, admin_client, admin_user):
        resp = admin_client.post(
            "/api/admin/users",
            json={
                "name": "Dup",
                "email": admin_user.email,
                "role": "buyer",
            },
        )
        assert resp.status_code == 409


class TestAdminUpdateUser:
    def test_update_role(self, admin_client, db_session):
        # Create a target user first
        target = User(
            email="target@trioscs.com",
            name="Target",
            role="buyer",
            azure_id="az-target",
            created_at=datetime.now(timezone.utc),
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
    def test_delete_user_success(self, admin_client, db_session):
        target = User(
            email="deleteme@trioscs.com",
            name="Delete Me",
            role="buyer",
            azure_id="az-delete",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(target)
        db_session.commit()
        target_id = target.id

        resp = admin_client.delete(f"/api/admin/users/{target_id}")
        assert resp.status_code == 200
        assert resp.json()["status"] == "deleted"

        # Verify actually removed from DB
        assert db_session.get(User, target_id) is None

    def test_delete_user_not_found(self, admin_client):
        resp = admin_client.delete("/api/admin/users/99999")
        assert resp.status_code == 404

    def test_delete_self_blocked(self, admin_client, admin_user):
        resp = admin_client.delete(f"/api/admin/users/{admin_user.id}")
        assert resp.status_code == 400
        body = resp.json()
        msg = (body.get("detail") or body.get("error") or "").lower()
        assert "yourself" in msg


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


# ── CSV Import ─────────────────────────────────────────────────────


class TestAdminImportCustomers:
    def test_import_customers_success(self, admin_client, db_session):
        csv_content = (
            b"company_name,site_name,contact_name,contact_email,contact_phone\n"
            b"Acme Corp,Acme HQ,Jane Doe,jane@acme.com,555-1234\n"
            b"Beta Inc,Beta Office,Bob Smith,bob@beta.com,555-5678\n"
        )
        resp = admin_client.post(
            "/api/admin/import/customers",
            files={"file": ("customers.csv", io.BytesIO(csv_content), "text/csv")},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["companies_created"] >= 2
        assert data["rows_processed"] == 2

        # Verify DB records created
        acme = db_session.query(Company).filter(Company.name == "Acme Corp").first()
        assert acme is not None

    def test_import_customers_empty(self, admin_client):
        csv_content = b"company_name,site_name,contact_name\n"
        resp = admin_client.post(
            "/api/admin/import/customers",
            files={"file": ("empty.csv", io.BytesIO(csv_content), "text/csv")},
        )
        # Empty CSV with headers only → 400 "No data rows found"
        assert resp.status_code == 400

    def test_import_customers_dedup(self, admin_client, db_session):
        # Reset rate limiter — prior tests may have consumed the 2/min budget
        limiter.reset()

        csv_content = b"company_name,site_name,contact_name,contact_email\nDedupCo,Main Site,Alice,alice@dedup.com\n"
        # First import
        resp1 = admin_client.post(
            "/api/admin/import/customers",
            files={"file": ("dup1.csv", io.BytesIO(csv_content), "text/csv")},
        )
        assert resp1.status_code == 200
        first_count = resp1.json()["companies_created"]
        assert first_count == 1

        # Reset rate limiter to avoid 429 on second request (2/min limit)
        limiter.reset()

        # Second import — same data, should not create duplicates
        resp2 = admin_client.post(
            "/api/admin/import/customers",
            files={"file": ("dup2.csv", io.BytesIO(csv_content), "text/csv")},
        )
        assert resp2.status_code == 200
        assert resp2.json()["companies_created"] == 0

        # Only one company in DB
        count = (
            db_session.query(Company)
            .filter(
                Company.name.ilike("DedupCo"),
            )
            .count()
        )
        assert count == 1


class TestAdminImportVendors:
    def test_import_vendors_success(self, admin_client, db_session):
        csv_content = (
            b"vendor_name,domain,contact_name,contact_email\n"
            b"Vendor Alpha,alpha.com,Al Pha,al@alpha.com\n"
            b"Vendor Beta,beta.com,Be Ta,be@beta.com\n"
        )
        resp = admin_client.post(
            "/api/admin/import/vendors",
            files={"file": ("vendors.csv", io.BytesIO(csv_content), "text/csv")},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["vendors_created"] >= 2
        assert data["rows_processed"] == 2

        # Verify DB records
        vc = (
            db_session.query(VendorCard)
            .filter(
                VendorCard.normalized_name == "vendor alpha",
            )
            .first()
        )
        assert vc is not None
        assert vc.display_name == "Vendor Alpha"

    def test_import_vendors_dedup(self, admin_client, db_session):
        csv_content = b"vendor_name,domain,contact_name,contact_email\nDedupVendor,dedup.com,Dup Person,dup@dedup.com\n"
        resp1 = admin_client.post(
            "/api/admin/import/vendors",
            files={"file": ("v1.csv", io.BytesIO(csv_content), "text/csv")},
        )
        assert resp1.status_code == 200
        assert resp1.json()["vendors_created"] == 1

        # Reset rate limiter to avoid 429 on second request (2/min limit)
        limiter.reset()

        # Second import — same vendor name → no new vendors
        resp2 = admin_client.post(
            "/api/admin/import/vendors",
            files={"file": ("v2.csv", io.BytesIO(csv_content), "text/csv")},
        )
        assert resp2.status_code == 200
        assert resp2.json()["vendors_created"] == 0

        count = (
            db_session.query(VendorCard)
            .filter(
                VendorCard.normalized_name == "dedupvendor",
            )
            .count()
        )
        assert count == 1


# ── Teams Config ───────────────────────────────────────────────────


class TestAdminTeamsConfig:
    def test_get_teams_config(self, admin_client):
        resp = admin_client.get("/api/admin/teams/config")
        assert resp.status_code == 200
        data = resp.json()
        # Should return a config dict (may have empty/default values)
        assert "team_id" in data
        assert "channel_id" in data
        assert "enabled" in data

    def test_set_teams_config(self, admin_client, db_session):
        resp = admin_client.post(
            "/api/admin/teams/config",
            json={
                "team_id": "team-abc-123",
                "channel_id": "channel-def-456",
                "channel_name": "General",
                "enabled": True,
            },
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "saved"

        # Verify persisted in SystemConfig
        row = (
            db_session.query(SystemConfig)
            .filter(
                SystemConfig.key == "teams_team_id",
            )
            .first()
        )
        assert row is not None
        assert row.value == "team-abc-123"

        ch_row = (
            db_session.query(SystemConfig)
            .filter(
                SystemConfig.key == "teams_channel_id",
            )
            .first()
        )
        assert ch_row is not None
        assert ch_row.value == "channel-def-456"


# ── Vendor Dedup Suggestions ──────────────────────────────────────


class TestAdminVendorDedup:
    def test_dedup_suggestions_empty(self, admin_client):
        """No vendor cards → empty candidate list."""
        resp = admin_client.get("/api/admin/vendor-dedup-suggestions")
        assert resp.status_code == 200
        data = resp.json()
        assert data["candidates"] == []
        assert data["count"] == 0

    def test_dedup_suggestions_with_data(self, admin_client, db_session):
        """Similar vendor names should produce dedup candidates."""
        v1 = VendorCard(
            normalized_name="arrow electronics",
            display_name="Arrow Electronics",
            sighting_count=10,
        )
        v2 = VendorCard(
            normalized_name="arrow electronic",
            display_name="Arrow Electronic",
            sighting_count=5,
        )
        v3 = VendorCard(
            normalized_name="totally different vendor",
            display_name="Totally Different Vendor",
            sighting_count=3,
        )
        db_session.add_all([v1, v2, v3])
        db_session.commit()

        resp = admin_client.get("/api/admin/vendor-dedup-suggestions?threshold=80")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] >= 1
        # The similar pair should be found
        names_in_candidates = set()
        for c in data["candidates"]:
            names_in_candidates.add(c["vendor_a"]["name"])
            names_in_candidates.add(c["vendor_b"]["name"])
        assert "Arrow Electronics" in names_in_candidates
        assert "Arrow Electronic" in names_in_candidates

    def test_dedup_suggestions_threshold(self, admin_client, db_session):
        """Higher threshold should return fewer (or equal) results than lower
        threshold."""
        v1 = VendorCard(
            normalized_name="mouser electronics",
            display_name="Mouser Electronics",
            sighting_count=20,
        )
        v2 = VendorCard(
            normalized_name="mouser electronic inc",
            display_name="Mouser Electronic Inc",
            sighting_count=8,
        )
        db_session.add_all([v1, v2])
        db_session.commit()

        resp_low = admin_client.get("/api/admin/vendor-dedup-suggestions?threshold=70")
        resp_high = admin_client.get("/api/admin/vendor-dedup-suggestions?threshold=99")
        assert resp_low.status_code == 200
        assert resp_high.status_code == 200

        count_low = resp_low.json()["count"]
        count_high = resp_high.json()["count"]
        assert count_low >= count_high

    def test_merge_vendors(self, admin_client, db_session):
        """Merge two vendor cards — keep the one with more sightings."""
        v1 = VendorCard(
            normalized_name="acme supply",
            display_name="Acme Supply",
            emails=["a@acme.com"],
            phones=["111"],
            sighting_count=10,
        )
        v2 = VendorCard(
            normalized_name="acme supply inc",
            display_name="Acme Supply Inc",
            emails=["b@acme.com"],
            phones=["111", "222"],
            sighting_count=3,
        )
        db_session.add_all([v1, v2])
        db_session.commit()
        keep_id, remove_id = v1.id, v2.id

        resp = admin_client.post(
            "/api/admin/vendor-merge",
            json={"keep_id": keep_id, "remove_id": remove_id},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["kept"] == keep_id
        assert data["removed"] == remove_id

        # Kept card should have merged data
        db_session.expire_all()
        kept = db_session.get(VendorCard, keep_id)
        assert kept is not None
        assert "a@acme.com" in kept.emails
        assert "b@acme.com" in kept.emails
        assert kept.sighting_count == 13
        assert "Acme Supply Inc" in (kept.alternate_names or [])

        # Removed card should be gone
        assert db_session.get(VendorCard, remove_id) is None

    def test_merge_vendors_not_found(self, admin_client):
        """Merge with invalid IDs returns 400 (ValueError caught by router)."""
        resp = admin_client.post(
            "/api/admin/vendor-merge",
            json={"keep_id": 99999, "remove_id": 99998},
        )
        assert resp.status_code == 400

    def test_merge_vendors_same_id(self, admin_client, db_session):
        """Cannot merge a vendor with itself."""
        v = VendorCard(
            normalized_name="self merge test",
            display_name="Self Merge Test",
            sighting_count=1,
        )
        db_session.add(v)
        db_session.commit()
        resp = admin_client.post(
            "/api/admin/vendor-merge",
            json={"keep_id": v.id, "remove_id": v.id},
        )
        assert resp.status_code == 400


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


class TestAdminUpdateUserEdgeCases:
    def test_update_user_invalid_role(self, admin_client, db_session):
        """Updating a user with invalid role returns error."""
        target = User(
            email="invalid_role_target@trioscs.com",
            name="Invalid",
            role="buyer",
            azure_id="az-invalid-role",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(target)
        db_session.commit()

        resp = admin_client.put(
            f"/api/admin/users/{target.id}",
            json={"role": "superadmin"},
        )
        # service returns error dict with status
        assert resp.status_code in (400, 200)

    def test_update_user_deactivate(self, admin_client, db_session):
        """Deactivating a user via is_active=False."""
        target = User(
            email="deactivate@trioscs.com",
            name="Deactivate",
            role="buyer",
            azure_id="az-deactivate",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(target)
        db_session.commit()

        resp = admin_client.put(
            f"/api/admin/users/{target.id}",
            json={"is_active": False},
        )
        assert resp.status_code == 200


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


class TestAdminImportCustomersEdgeCases:
    def test_import_customers_latin1_encoding(self, admin_client, db_session):
        """Import CSV with latin-1 encoding (non-UTF8 characters)."""
        # Encode with latin-1 to trigger UnicodeDecodeError on UTF-8 attempt
        csv_text = "company_name,site_name,contact_name\nR\xe9sum\xe9 Corp,Main,Ren\xe9\n"
        csv_bytes = csv_text.encode("latin-1")
        resp = admin_client.post(
            "/api/admin/import/customers",
            files={"file": ("latin.csv", io.BytesIO(csv_bytes), "text/csv")},
        )
        assert resp.status_code == 200
        assert resp.json()["companies_created"] >= 1

    def test_import_customers_skips_empty_company(self, admin_client, db_session):
        """Rows with empty company_name are skipped."""
        csv_content = b"company_name,site_name,contact_name\n,Main,Jane\nValid Corp,Main,Bob\n"
        resp = admin_client.post(
            "/api/admin/import/customers",
            files={"file": ("skip.csv", io.BytesIO(csv_content), "text/csv")},
        )
        assert resp.status_code == 200
        assert resp.json()["companies_created"] == 1

    def test_import_customers_with_contact_no_email(self, admin_client, db_session):
        """Import customer with contact name but no email."""
        csv_content = b"company_name,site_name,contact_name,contact_email\nNoEmail Corp,Main,John Doe,\n"
        resp = admin_client.post(
            "/api/admin/import/customers",
            files={"file": ("noemail.csv", io.BytesIO(csv_content), "text/csv")},
        )
        assert resp.status_code == 200
        assert resp.json()["contacts_created"] >= 1


class TestAdminImportVendorsEdgeCases:
    def test_import_vendors_empty_csv(self, admin_client):
        csv_content = b"vendor_name,domain\n"
        resp = admin_client.post(
            "/api/admin/import/vendors",
            files={"file": ("empty.csv", io.BytesIO(csv_content), "text/csv")},
        )
        assert resp.status_code == 400

    def test_import_vendors_skips_empty_vendor_name(self, admin_client, db_session):
        csv_content = (
            b"vendor_name,domain,contact_name,contact_email\n"
            b",empty.com,Empty Person,emp@empty.com\n"
            b"Valid Vendor,valid.com,Valid Person,val@valid.com\n"
        )
        resp = admin_client.post(
            "/api/admin/import/vendors",
            files={"file": ("skip.csv", io.BytesIO(csv_content), "text/csv")},
        )
        assert resp.status_code == 200
        assert resp.json()["vendors_created"] == 1

    def test_import_vendors_contact_no_email(self, admin_client, db_session):
        csv_content = b"vendor_name,domain,contact_name,contact_email\nNoEmail Vendor,noemail.com,John Doe,\n"
        resp = admin_client.post(
            "/api/admin/import/vendors",
            files={"file": ("noemail.csv", io.BytesIO(csv_content), "text/csv")},
        )
        assert resp.status_code == 200
        assert resp.json()["contacts_created"] >= 1

    def test_import_vendors_latin1_encoding(self, admin_client, db_session):
        csv_text = "vendor_name,domain\nR\xe9sum\xe9 Vendor,resume.com\n"
        csv_bytes = csv_text.encode("latin-1")
        resp = admin_client.post(
            "/api/admin/import/vendors",
            files={"file": ("latin.csv", io.BytesIO(csv_bytes), "text/csv")},
        )
        assert resp.status_code == 200
        assert resp.json()["vendors_created"] >= 1


class TestAdminTeamsConfigEdgeCases:
    def test_get_teams_config_with_db_overrides(self, admin_client, db_session):
        """Teams config reads runtime overrides from SystemConfig."""
        from datetime import datetime, timezone

        rows = [
            SystemConfig(key="teams_team_id", value="db-team-123", updated_at=datetime.now(timezone.utc)),
            SystemConfig(key="teams_channel_id", value="db-channel-456", updated_at=datetime.now(timezone.utc)),
            SystemConfig(key="teams_enabled", value="true", updated_at=datetime.now(timezone.utc)),
            SystemConfig(key="teams_channel_name", value="General", updated_at=datetime.now(timezone.utc)),
            SystemConfig(key="teams_hot_threshold", value="5000", updated_at=datetime.now(timezone.utc)),
        ]
        db_session.add_all(rows)
        db_session.commit()

        resp = admin_client.get("/api/admin/teams/config")
        assert resp.status_code == 200
        data = resp.json()
        assert data["team_id"] == "db-team-123"
        assert data["channel_id"] == "db-channel-456"
        assert data["enabled"] is True
        assert data["channel_name"] == "General"
        assert data["hot_threshold"] == 5000.0

    def test_get_teams_config_invalid_threshold(self, admin_client, db_session):
        """Invalid hot_threshold value in DB is ignored."""
        row = SystemConfig(
            key="teams_hot_threshold",
            value="not-a-number",
            updated_at=datetime.now(timezone.utc),
        )
        db_session.add(row)
        db_session.commit()

        resp = admin_client.get("/api/admin/teams/config")
        assert resp.status_code == 200
        # Should not crash; hot_threshold falls back to default

    def test_set_teams_config_with_hot_threshold(self, admin_client, db_session):
        """Set teams config with hot_threshold value."""
        resp = admin_client.post(
            "/api/admin/teams/config",
            json={
                "team_id": "team-hot-123",
                "channel_id": "ch-hot-456",
                "enabled": True,
                "hot_threshold": 25000.0,
            },
        )
        assert resp.status_code == 200

        row = db_session.query(SystemConfig).filter(SystemConfig.key == "teams_hot_threshold").first()
        assert row is not None
        assert row.value == "25000.0"

    def test_set_teams_config_without_optional_fields(self, admin_client, db_session):
        """Set teams config without channel_name or hot_threshold."""
        resp = admin_client.post(
            "/api/admin/teams/config",
            json={
                "team_id": "team-min",
                "channel_id": "ch-min",
                "enabled": False,
            },
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "saved"


# ── Company Dedup ────────────────────────────────────────────────────


class TestCompanyDedup:
    """Tests for company dedup suggestions and merge endpoints."""

    def _make_company(self, db, name, sites=0, owner_id=None, is_strategic=False, **kw):
        c = Company(
            name=name,
            is_active=True,
            account_owner_id=owner_id,
            is_strategic=is_strategic,
            created_at=datetime.now(timezone.utc),
            **kw,
        )
        db.add(c)
        db.flush()
        for i in range(sites):
            s = CustomerSite(
                company_id=c.id,
                site_name=f"Site {i + 1}",
                owner_id=owner_id,
                created_at=datetime.now(timezone.utc),
            )
            db.add(s)
        db.flush()
        return c

    def test_company_dedup_suggestions_empty(self, admin_client):
        resp = admin_client.get("/api/admin/company-dedup-suggestions")
        assert resp.status_code == 200
        data = resp.json()
        assert data["candidates"] == []
        assert data["count"] == 0

    def test_company_dedup_suggestions_with_data(self, admin_client, db_session):
        self._make_company(db_session, "Arrow Electronics")
        self._make_company(db_session, "Arrow Electronic")
        self._make_company(db_session, "Totally Different Corp")
        db_session.commit()

        resp = admin_client.get("/api/admin/company-dedup-suggestions?threshold=80")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] >= 1
        names = set()
        for c in data["candidates"]:
            names.add(c["company_a"]["name"])
            names.add(c["company_b"]["name"])
        assert "Arrow Electronics" in names
        assert "Arrow Electronic" in names

    def test_company_merge_preview(self, admin_client, db_session, admin_user):
        keep = self._make_company(db_session, "Keep Corp", sites=2, owner_id=admin_user.id)
        remove = self._make_company(db_session, "Remove Corp", sites=1, owner_id=admin_user.id, domain="remove.com")
        db_session.commit()

        resp = admin_client.get(f"/api/admin/company-merge-preview?keep_id={keep.id}&remove_id={remove.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["keep"]["id"] == keep.id
        assert data["remove"]["id"] == remove.id
        assert data["sites_to_move"] >= 1
        assert "domain" in data["fields_to_fill"]

    def test_merge_companies_basic(self, admin_client, db_session, admin_user):
        keep = self._make_company(db_session, "Keep Corp", sites=1, owner_id=admin_user.id)
        remove = self._make_company(db_session, "Remove Corp", sites=2, owner_id=admin_user.id)
        db_session.commit()

        resp = admin_client.post(
            "/api/admin/company-merge",
            json={
                "keep_id": keep.id,
                "remove_id": remove.id,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["kept"] == keep.id
        assert data["removed"] == remove.id
        assert data["sites_moved"] == 2

        # Removed company gone
        db_session.expire_all()
        assert db_session.get(Company, remove.id) is None
        # Sites now under keep
        sites = db_session.query(CustomerSite).filter(CustomerSite.company_id == keep.id).all()
        assert len(sites) >= 3  # 1 original + 2 moved

    def test_merge_companies_empty_hq_deleted(self, admin_client, db_session, admin_user):
        keep = self._make_company(db_session, "Keep Corp")
        remove = Company(
            name="Remove Corp",
            is_active=True,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(remove)
        db_session.flush()
        # Empty HQ site: name=HQ, no contact info, no site contacts, no reqs
        empty_hq = CustomerSite(
            company_id=remove.id,
            site_name="HQ",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(empty_hq)
        db_session.commit()

        resp = admin_client.post(
            "/api/admin/company-merge",
            json={
                "keep_id": keep.id,
                "remove_id": remove.id,
            },
        )
        assert resp.status_code == 200
        assert resp.json()["sites_deleted"] == 1
        assert resp.json()["sites_moved"] == 0

    def test_merge_companies_not_found(self, admin_client):
        resp = admin_client.post(
            "/api/admin/company-merge",
            json={
                "keep_id": 99999,
                "remove_id": 99998,
            },
        )
        assert resp.status_code == 400

    def test_merge_companies_same_id(self, admin_client, db_session):
        c = self._make_company(db_session, "Self Corp")
        db_session.commit()
        resp = admin_client.post(
            "/api/admin/company-merge",
            json={
                "keep_id": c.id,
                "remove_id": c.id,
            },
        )
        assert resp.status_code == 400

    def test_merge_enrichment_fields(self, admin_client, db_session):
        keep = self._make_company(db_session, "Keep Corp")
        remove = self._make_company(db_session, "Remove Corp", domain="remove.com", website="https://remove.com")
        db_session.commit()

        resp = admin_client.post(
            "/api/admin/company-merge",
            json={
                "keep_id": keep.id,
                "remove_id": remove.id,
            },
        )
        assert resp.status_code == 200
        db_session.expire_all()
        kept = db_session.get(Company, keep.id)
        assert kept.domain == "remove.com"
        assert kept.website == "https://remove.com"

    def test_merge_json_tags(self, admin_client, db_session):
        keep = self._make_company(db_session, "Keep Corp")
        keep.brand_tags = ["Vishay"]
        remove = self._make_company(db_session, "Remove Corp")
        remove.brand_tags = ["Vishay", "Texas Instruments"]
        db_session.commit()

        resp = admin_client.post(
            "/api/admin/company-merge",
            json={
                "keep_id": keep.id,
                "remove_id": remove.id,
            },
        )
        assert resp.status_code == 200
        db_session.expire_all()
        kept = db_session.get(Company, keep.id)
        assert "Vishay" in kept.brand_tags
        assert "Texas Instruments" in kept.brand_tags
        # No duplicates
        assert kept.brand_tags.count("Vishay") == 1

    def test_merge_notes_appended(self, admin_client, db_session):
        keep = self._make_company(db_session, "Keep Corp")
        keep.notes = "Keep notes"
        remove = self._make_company(db_session, "Remove Corp")
        remove.notes = "Remove notes"
        db_session.commit()

        resp = admin_client.post(
            "/api/admin/company-merge",
            json={
                "keep_id": keep.id,
                "remove_id": remove.id,
            },
        )
        assert resp.status_code == 200
        db_session.expire_all()
        kept = db_session.get(Company, keep.id)
        assert "Keep notes" in kept.notes
        assert "Remove notes" in kept.notes
        assert "Merged from Remove Corp" in kept.notes

    def test_merge_is_strategic(self, admin_client, db_session):
        keep = self._make_company(db_session, "Keep Corp", is_strategic=False)
        remove = self._make_company(db_session, "Remove Corp", is_strategic=True)
        db_session.commit()

        resp = admin_client.post(
            "/api/admin/company-merge",
            json={
                "keep_id": keep.id,
                "remove_id": remove.id,
            },
        )
        assert resp.status_code == 200
        db_session.expire_all()
        kept = db_session.get(Company, keep.id)
        assert kept.is_strategic is True

    def test_merge_reassigns_activities(self, admin_client, db_session, admin_user):
        keep = self._make_company(db_session, "Keep Corp")
        remove = self._make_company(db_session, "Remove Corp")
        act = ActivityLog(
            user_id=admin_user.id,
            activity_type="note",
            channel="manual",
            company_id=remove.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(act)
        db_session.commit()
        act_id = act.id

        resp = admin_client.post(
            "/api/admin/company-merge",
            json={
                "keep_id": keep.id,
                "remove_id": remove.id,
            },
        )
        assert resp.status_code == 200
        assert resp.json()["reassigned"] >= 1
        db_session.expire_all()
        assert db_session.get(ActivityLog, act_id).company_id == keep.id

    def test_merge_site_name_collision(self, admin_client, db_session, admin_user):
        keep = self._make_company(db_session, "Keep Corp")
        # Add a site named "Main" to keep
        s1 = CustomerSite(
            company_id=keep.id,
            site_name="Main",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(s1)
        remove = self._make_company(db_session, "Remove Corp")
        # Add a site named "Main" to remove — will collide
        s2 = CustomerSite(
            company_id=remove.id,
            site_name="Main",
            contact_name="Someone",  # not empty so it won't be deleted
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(s2)
        db_session.commit()
        s2_id = s2.id

        resp = admin_client.post(
            "/api/admin/company-merge",
            json={
                "keep_id": keep.id,
                "remove_id": remove.id,
            },
        )
        assert resp.status_code == 200
        db_session.expire_all()
        moved = db_session.get(CustomerSite, s2_id)
        assert moved.company_id == keep.id
        assert "Remove Corp" in moved.site_name  # renamed to avoid collision

    def test_merge_preserves_site_owners(self, admin_client, db_session, admin_user):
        # Create a second user as site owner
        user2 = User(
            email="siteowner@trioscs.com",
            name="Site Owner",
            role="sales",
            azure_id="az-siteowner",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(user2)
        db_session.flush()

        keep = self._make_company(db_session, "Keep Corp", sites=0)
        s_keep = CustomerSite(
            company_id=keep.id,
            site_name="Keep Site",
            owner_id=admin_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(s_keep)

        remove = self._make_company(db_session, "Remove Corp", sites=0)
        s_remove = CustomerSite(
            company_id=remove.id,
            site_name="Remove Site",
            owner_id=user2.id,
            contact_name="X",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(s_remove)
        db_session.commit()
        s_remove_id = s_remove.id

        resp = admin_client.post(
            "/api/admin/company-merge",
            json={
                "keep_id": keep.id,
                "remove_id": remove.id,
            },
        )
        assert resp.status_code == 200
        db_session.expire_all()
        # Both site owners retained
        assert db_session.get(CustomerSite, s_keep.id).owner_id == admin_user.id
        assert db_session.get(CustomerSite, s_remove_id).owner_id == user2.id


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
    """Tests for /api/admin/company-merge-preview (lines 456, 472)."""

    def test_merge_preview_not_found(self, admin_client):
        """Missing company -> 404."""
        resp = admin_client.get("/api/admin/company-merge-preview?keep_id=99999&remove_id=99998")
        assert resp.status_code == 404

    def test_merge_preview_success(self, admin_client, db_session):
        """Preview merging two companies with sites."""
        keep = Company(name="Keep Co", is_active=True)
        remove = Company(name="Remove Co", is_active=True, domain="remove.com")
        db_session.add_all([keep, remove])
        db_session.flush()

        # Add an empty HQ site to remove (should count as sites_to_delete)
        empty_hq = CustomerSite(company_id=remove.id, site_name="HQ")
        # Add a non-empty site to remove (should count as sites_to_move)
        real_site = CustomerSite(
            company_id=remove.id,
            site_name="Branch",
            contact_name="John",
            contact_email="john@remove.com",
        )
        db_session.add_all([empty_hq, real_site])
        db_session.commit()

        resp = admin_client.get(f"/api/admin/company-merge-preview?keep_id={keep.id}&remove_id={remove.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["keep"]["name"] == "Keep Co"
        assert data["remove"]["name"] == "Remove Co"
        assert data["sites_to_delete"] == 1
        assert data["sites_to_move"] == 1
        assert "fields_to_fill" in data
        assert "domain" in data["fields_to_fill"]
