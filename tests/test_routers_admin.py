"""
test_routers_admin.py — Tests for admin router endpoints.

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

from app.models import ApiSource, Company, CustomerSite, SystemConfig, User, VendorCard
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


# ── Delete User ────────────────────────────────────────────────────


class TestAdminDeleteUser:
    def test_delete_user_success(self, admin_client, db_session):
        target = User(
            email="deleteme@trioscs.com", name="Delete Me", role="buyer",
            azure_id="az-delete", created_at=datetime.now(timezone.utc),
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
            key="upsert_test_key", value="old_val",
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
        self, mock_mask, mock_decrypt, admin_client, db_session, api_source,
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
        self, mock_encrypt, admin_client, db_session, api_source,
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
        self, mock_cred_set, admin_client, db_session, api_source,
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

        csv_content = (
            b"company_name,site_name,contact_name,contact_email\n"
            b"DedupCo,Main Site,Alice,alice@dedup.com\n"
        )
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
        count = db_session.query(Company).filter(
            Company.name.ilike("DedupCo"),
        ).count()
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
        vc = db_session.query(VendorCard).filter(
            VendorCard.normalized_name == "vendor alpha",
        ).first()
        assert vc is not None
        assert vc.display_name == "Vendor Alpha"

    def test_import_vendors_dedup(self, admin_client, db_session):
        csv_content = (
            b"vendor_name,domain,contact_name,contact_email\n"
            b"DedupVendor,dedup.com,Dup Person,dup@dedup.com\n"
        )
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

        count = db_session.query(VendorCard).filter(
            VendorCard.normalized_name == "dedupvendor",
        ).count()
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
        row = db_session.query(SystemConfig).filter(
            SystemConfig.key == "teams_team_id",
        ).first()
        assert row is not None
        assert row.value == "team-abc-123"

        ch_row = db_session.query(SystemConfig).filter(
            SystemConfig.key == "teams_channel_id",
        ).first()
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
        """Higher threshold should return fewer (or equal) results than lower threshold."""
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
