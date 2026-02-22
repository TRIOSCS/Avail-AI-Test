"""
test_services_admin.py — Tests for admin_service.

Tests user management, config CRUD, scoring weights, and system health.
Uses in-memory SQLite via conftest fixtures.

Called by: pytest
Depends on: app/services/admin_service.py, conftest.py
"""

from datetime import datetime, timezone

from app.models import SystemConfig
from app.services.admin_service import (
    VALID_ROLES,
    get_all_config,
    get_system_health,
    list_users,
    set_config_value,
    update_user,
)

# ── Helpers ─────────────────────────────────────────────────────────


def _make_config(db, key, value, desc=""):
    row = SystemConfig(
        key=key, value=value, description=desc,
        updated_at=datetime.now(timezone.utc),
    )
    db.add(row)
    db.flush()
    return row


# ── User Management ─────────────────────────────────────────────────


class TestListUsers:
    def test_returns_all_users(self, db_session, test_user, admin_user):
        result = list_users(db_session)
        emails = [u["email"] for u in result]
        assert test_user.email in emails
        assert admin_user.email in emails

    def test_user_fields(self, db_session, test_user):
        result = list_users(db_session)
        user_dict = result[0]
        assert "id" in user_dict
        assert "name" in user_dict
        assert "email" in user_dict
        assert "role" in user_dict
        assert "is_active" in user_dict


class TestUpdateUser:
    def test_change_role(self, db_session, test_user, admin_user):
        result = update_user(db_session, test_user.id, {"role": "sales"}, admin_user)
        assert result["role"] == "sales"

    def test_deactivate_user(self, db_session, test_user, admin_user):
        result = update_user(db_session, test_user.id, {"is_active": False}, admin_user)
        assert result["is_active"] is False

    def test_cannot_self_deactivate(self, db_session, admin_user):
        result = update_user(db_session, admin_user.id, {"is_active": False}, admin_user)
        assert "error" in result
        assert "yourself" in result["error"]

    def test_cannot_change_own_role(self, db_session, admin_user):
        result = update_user(db_session, admin_user.id, {"role": "buyer"}, admin_user)
        assert "error" in result
        assert "own role" in result["error"]

    def test_invalid_role_rejected(self, db_session, test_user, admin_user):
        result = update_user(db_session, test_user.id, {"role": "superuser"}, admin_user)
        assert "error" in result
        assert "Invalid" in result["error"]

    def test_user_not_found(self, db_session, admin_user):
        result = update_user(db_session, 99999, {"role": "buyer"}, admin_user)
        assert result["status"] == 404

    def test_update_name(self, db_session, test_user, admin_user):
        result = update_user(db_session, test_user.id, {"name": "New Name"}, admin_user)
        assert result["name"] == "New Name"

    def test_valid_roles_list(self):
        assert "buyer" in VALID_ROLES
        assert "sales" in VALID_ROLES
        assert "admin" in VALID_ROLES
        assert "manager" in VALID_ROLES
        assert "dev_assistant" in VALID_ROLES


# ── System Config ───────────────────────────────────────────────────


class TestConfig:
    def test_get_all_config(self, db_session):
        _make_config(db_session, "test_key", "test_value", "A test setting")
        db_session.commit()

        result = get_all_config(db_session)
        assert len(result) >= 1
        found = [r for r in result if r["key"] == "test_key"]
        assert len(found) == 1
        assert found[0]["value"] == "test_value"

    def test_set_config_value(self, db_session, admin_user):
        _make_config(db_session, "my_key", "old_value")
        db_session.commit()

        result = set_config_value(db_session, "my_key", "new_value", admin_user.email)
        assert result["value"] == "new_value"
        assert result["updated_by"] == admin_user.email

    def test_set_nonexistent_key(self, db_session, admin_user):
        result = set_config_value(db_session, "nonexistent", "val", admin_user.email)
        assert "error" in result
        assert result["status"] == 404


# ── Scoring Weights ─────────────────────────────────────────────────


# ── System Health ───────────────────────────────────────────────────


class TestSystemHealth:
    def test_returns_version(self, db_session):
        result = get_system_health(db_session)
        assert "version" in result
        assert isinstance(result["version"], str)

    def test_returns_db_stats(self, db_session):
        result = get_system_health(db_session)
        assert "db_stats" in result
        assert "users" in result["db_stats"]
        assert "requisitions" in result["db_stats"]

    def test_counts_match(self, db_session, test_user, test_company):
        result = get_system_health(db_session)
        assert result["db_stats"]["users"] >= 1
        assert result["db_stats"]["companies"] >= 1

    def test_scheduler_status(self, db_session, test_user):
        result = get_system_health(db_session)
        assert "scheduler" in result
        assert len(result["scheduler"]) >= 1
        s = result["scheduler"][0]
        assert "email" in s
        assert "m365_connected" in s
