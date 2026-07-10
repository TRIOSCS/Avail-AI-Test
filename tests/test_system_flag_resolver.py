"""test_system_flag_resolver.py — Tests for the DB-overrides-env flag resolver.

Covers app/services/admin_service.py resolver helpers that make the system_config
DB row authoritative over the env-backed Pydantic setting:
  - get_effective_flag(db, key, env_default) -> bool
  - get_effective_int(db, key, env_default) -> int
and the cache-invalidation contract on set_config_value (a write must be visible on
the next read without waiting for the 5-min TTL to expire).

Called by: pytest
Depends on: app/services/admin_service.py, app/models/config.py, conftest.py
"""

import time
from datetime import UTC, datetime

from app.models import SystemConfig
from app.services import admin_service
from app.services.admin_service import (
    get_effective_flag,
    get_effective_int,
    set_config_value,
)

# ── Helpers ─────────────────────────────────────────────────────────────


def _seed(db, key, value):
    """Seed a system_config row directly (set_config_value 404s on unknown keys)."""
    row = SystemConfig(key=key, value=value, updated_at=datetime.now(UTC))
    db.add(row)
    db.flush()
    return row


# ── get_effective_flag ──────────────────────────────────────────────────


class TestEffectiveFlag:
    def test_effective_flag_db_overrides_env(self, db_session, admin_user):
        _seed(db_session, "email_mining_enabled", "false")
        set_config_value(db_session, "email_mining_enabled", "true", admin_user.email)
        assert get_effective_flag(db_session, "email_mining_enabled", env_default=False) is True

    def test_effective_flag_db_false_overrides_env_true(self, db_session, admin_user):
        _seed(db_session, "proactive_matching_enabled", "true")
        set_config_value(db_session, "proactive_matching_enabled", "false", admin_user.email)
        assert get_effective_flag(db_session, "proactive_matching_enabled", env_default=True) is False

    def test_effective_flag_case_insensitive(self, db_session):
        _seed(db_session, "activity_tracking_enabled", "TRUE")
        assert get_effective_flag(db_session, "activity_tracking_enabled", env_default=False) is True

    def test_effective_flag_falls_back_to_env_when_missing(self, db_session):
        assert get_effective_flag(db_session, "no_such_key", env_default=True) is True
        assert get_effective_flag(db_session, "no_such_key", env_default=False) is False

    def test_effective_flag_malformed_db_falls_back_to_env(self, db_session):
        _seed(db_session, "email_mining_enabled", "banana")
        # Malformed bool string must not crash and must fall back to the env default.
        assert get_effective_flag(db_session, "email_mining_enabled", env_default=True) is True

    def test_effective_flag_none_when_db_none(self):
        # No session in scope (e.g. _get_connector_for_source with db=None) → env default.
        assert get_effective_flag(None, "email_mining_enabled", env_default=True) is True
        assert get_effective_flag(None, "email_mining_enabled", env_default=False) is False


# ── get_effective_int ───────────────────────────────────────────────────


class TestEffectiveInt:
    def test_effective_int_db_overrides_env(self, db_session, admin_user):
        _seed(db_session, "inbox_scan_interval_min", "30")
        set_config_value(db_session, "inbox_scan_interval_min", "45", admin_user.email)
        assert get_effective_int(db_session, "inbox_scan_interval_min", env_default=30) == 45

    def test_effective_int_falls_back_to_env_when_missing(self, db_session):
        assert get_effective_int(db_session, "no_such_key", env_default=30) == 30

    def test_effective_int_malformed_db_falls_back_to_env(self, db_session):
        _seed(db_session, "inbox_scan_interval_min", "not-a-number")
        assert get_effective_int(db_session, "inbox_scan_interval_min", env_default=30) == 30

    def test_effective_int_none_when_db_none(self):
        assert get_effective_int(None, "inbox_scan_interval_min", env_default=30) == 30


# ── Cache invalidation on write ─────────────────────────────────────────


class TestCacheInvalidation:
    def test_write_visible_on_next_read_without_ttl_wait(self, db_session, admin_user):
        """A set_config_value write must be visible on the very next resolver read.

        Forces a 'fresh' (non-expired) cache holding the OLD value, then writes. If
        set_config_value did not invalidate the cache, the resolver would keep serving
        the stale cached value until the 5-min TTL lapsed.
        """
        _seed(db_session, "email_mining_enabled", "false")
        db_session.commit()

        # Prime the cache with the old value and a far-future timestamp so the TTL
        # check (_ensure_config_cache_fresh) would NOT trigger a reload on its own.
        admin_service._config_cache = {"email_mining_enabled": "false"}
        admin_service._config_cache_ts = time.time() + 10_000

        set_config_value(db_session, "email_mining_enabled", "true", admin_user.email)

        # Must reflect the write immediately — proving the cache was invalidated.
        assert get_effective_flag(db_session, "email_mining_enabled", env_default=False) is True


# ── No-surprise startup reconcile ───────────────────────────────────────


class TestStartupReconcile:
    """_reconcile_system_config mirrors env into never-admin-edited rows only."""

    def _settings(self, **kw):
        from types import SimpleNamespace

        base = dict(
            inbox_scan_interval_min=45,
            email_mining_enabled=True,
            proactive_matching_enabled=False,
            activity_tracking_enabled=True,
        )
        base.update(kw)
        return SimpleNamespace(**base)

    def test_reconcile_updates_unedited_rows_to_env(self, db_session, monkeypatch):
        from app import startup
        from app.services import admin_service

        # Seed rows as the seed would (updated_by NULL) with the OLD seed defaults.
        _seed(db_session, "inbox_scan_interval_min", "30")
        _seed(db_session, "email_mining_enabled", "false")
        _seed(db_session, "proactive_matching_enabled", "true")
        _seed(db_session, "activity_tracking_enabled", "true")
        db_session.commit()

        monkeypatch.setattr("app.config.settings", self._settings(), raising=False)
        startup._reconcile_system_config(db_session)
        db_session.commit()
        admin_service._invalidate_config_cache()

        # Each row now mirrors the current env value so the resolver returns it.
        assert get_effective_int(db_session, "inbox_scan_interval_min", env_default=0) == 45
        assert get_effective_flag(db_session, "email_mining_enabled", env_default=False) is True
        assert get_effective_flag(db_session, "proactive_matching_enabled", env_default=True) is False
        assert get_effective_flag(db_session, "activity_tracking_enabled", env_default=False) is True

    def test_reconcile_preserves_admin_edited_rows(self, db_session, monkeypatch, admin_user):
        from app import startup
        from app.services import admin_service

        _seed(db_session, "email_mining_enabled", "false")
        db_session.commit()
        # An admin deliberately flipped it on — sets updated_by.
        set_config_value(db_session, "email_mining_enabled", "true", admin_user.email)

        # Env says False, but reconcile must NOT clobber an admin-edited row.
        monkeypatch.setattr("app.config.settings", self._settings(email_mining_enabled=False), raising=False)
        startup._reconcile_system_config(db_session)
        db_session.commit()
        admin_service._invalidate_config_cache()

        assert get_effective_flag(db_session, "email_mining_enabled", env_default=False) is True

    def test_reconcile_is_idempotent(self, db_session, monkeypatch):
        from app import startup
        from app.services import admin_service

        _seed(db_session, "inbox_scan_interval_min", "30")
        db_session.commit()

        monkeypatch.setattr("app.config.settings", self._settings(inbox_scan_interval_min=45), raising=False)
        startup._reconcile_system_config(db_session)
        db_session.commit()
        startup._reconcile_system_config(db_session)
        db_session.commit()
        admin_service._invalidate_config_cache()

        assert get_effective_int(db_session, "inbox_scan_interval_min", env_default=0) == 45
