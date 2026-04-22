"""test_credential_service_extra.py — Additional coverage for credential_service.

Covers: get_all_credentials_for_source, get_credentials_batch, get_credential_cached,
and decrypt-failure fallback paths.

Called by: pytest
Depends on: app/services/credential_service, app/models/ApiSource
"""

import os
import time

os.environ["TESTING"] = "1"

from unittest.mock import patch

from app.models import ApiSource
from app.services.credential_service import (
    encrypt_value,
    get_all_credentials_for_source,
    get_credential,
    get_credential_cached,
    get_credentials_batch,
)


def _make_source(db, name, env_vars, credentials=None):
    src = ApiSource(
        name=name,
        display_name=name.title(),
        category="api",
        source_type="aggregator",
        status="active",
        env_vars=env_vars,
        credentials=credentials or {},
    )
    db.add(src)
    db.commit()
    return src


# ── get_all_credentials_for_source ────────────────────────────────────


class TestGetAllCredentialsForSource:
    def test_returns_empty_for_unknown_source(self, db_session):
        result = get_all_credentials_for_source(db_session, "nonexistent_src")
        assert result == {}

    def test_returns_decrypted_db_values(self, db_session):
        enc1 = encrypt_value("val-one")
        enc2 = encrypt_value("val-two")
        _make_source(
            db_session,
            "all_creds_src",
            ["KEY1", "KEY2"],
            {"KEY1": enc1, "KEY2": enc2},
        )
        result = get_all_credentials_for_source(db_session, "all_creds_src")
        assert result["KEY1"] == "val-one"
        assert result["KEY2"] == "val-two"

    def test_env_var_fallback_for_missing_db_key(self, db_session, monkeypatch):
        monkeypatch.setenv("ENV_FALLBACK_KEY", "env-only-value")
        _make_source(db_session, "env_fallback_src", ["ENV_FALLBACK_KEY"], {})
        result = get_all_credentials_for_source(db_session, "env_fallback_src")
        assert result.get("ENV_FALLBACK_KEY") == "env-only-value"

    def test_skips_env_vars_with_no_value(self, db_session, monkeypatch):
        monkeypatch.delenv("EMPTY_KEY", raising=False)
        _make_source(db_session, "no_val_src", ["EMPTY_KEY"], {})
        result = get_all_credentials_for_source(db_session, "no_val_src")
        assert "EMPTY_KEY" not in result

    def test_handles_decrypt_error_with_fallback(self, db_session, monkeypatch):
        monkeypatch.setenv("BAD_KEY", "env-value-for-bad-key")
        _make_source(
            db_session,
            "bad_decrypt_src",
            ["BAD_KEY"],
            {"BAD_KEY": "not-a-valid-fernet-token"},
        )
        result = get_all_credentials_for_source(db_session, "bad_decrypt_src")
        assert result.get("BAD_KEY") == "env-value-for-bad-key"

    def test_no_credentials_field(self, db_session, monkeypatch):
        monkeypatch.setenv("NC_KEY", "nc-value")
        src = ApiSource(
            name="null_creds_src",
            display_name="Null Creds",
            category="api",
            source_type="aggregator",
            status="active",
            env_vars=["NC_KEY"],
            credentials=None,
        )
        db_session.add(src)
        db_session.commit()
        result = get_all_credentials_for_source(db_session, "null_creds_src")
        assert result.get("NC_KEY") == "nc-value"


# ── get_credentials_batch ─────────────────────────────────────────────


class TestGetCredentialsBatch:
    def test_returns_all_requested_keys(self, db_session):
        enc = encrypt_value("batch-val")
        _make_source(db_session, "batch_src", ["BATCH_KEY"], {"BATCH_KEY": enc})
        result = get_credentials_batch(db_session, [("batch_src", "BATCH_KEY")])
        assert result[("batch_src", "BATCH_KEY")] == "batch-val"

    def test_env_var_fallback(self, db_session, monkeypatch):
        monkeypatch.setenv("BATCH_ENV_KEY", "env-batch")
        result = get_credentials_batch(db_session, [("unknown_src", "BATCH_ENV_KEY")])
        assert result[("unknown_src", "BATCH_ENV_KEY")] == "env-batch"

    def test_returns_none_for_missing(self, db_session, monkeypatch):
        monkeypatch.delenv("NO_KEY_HERE", raising=False)
        result = get_credentials_batch(db_session, [("nonexistent", "NO_KEY_HERE")])
        assert result[("nonexistent", "NO_KEY_HERE")] is None

    def test_handles_empty_requests(self, db_session):
        result = get_credentials_batch(db_session, [])
        assert result == {}

    def test_multiple_sources_in_one_call(self, db_session, monkeypatch):
        enc1 = encrypt_value("src1-val")
        enc2 = encrypt_value("src2-val")
        _make_source(db_session, "multi_src1", ["K1"], {"K1": enc1})
        _make_source(db_session, "multi_src2", ["K2"], {"K2": enc2})
        result = get_credentials_batch(
            db_session,
            [("multi_src1", "K1"), ("multi_src2", "K2")],
        )
        assert result[("multi_src1", "K1")] == "src1-val"
        assert result[("multi_src2", "K2")] == "src2-val"

    def test_decrypt_error_falls_back_to_env(self, db_session, monkeypatch):
        monkeypatch.setenv("BATCH_BAD_KEY", "fallback-value")
        _make_source(
            db_session,
            "batch_bad_src",
            ["BATCH_BAD_KEY"],
            {"BATCH_BAD_KEY": "not-valid-fernet"},
        )
        result = get_credentials_batch(db_session, [("batch_bad_src", "BATCH_BAD_KEY")])
        assert result[("batch_bad_src", "BATCH_BAD_KEY")] == "fallback-value"


# ── get_credential_cached ─────────────────────────────────────────────


class TestGetCredentialCached:
    def test_returns_credential_value(self, db_session):
        enc = encrypt_value("cached-val")
        _make_source(db_session, "cached_src", ["CACHED_KEY"], {"CACHED_KEY": enc})

        from app.services import credential_service

        credential_service._cred_cache.clear()

        with patch("app.database.SessionLocal") as mock_sl:
            mock_sl.return_value = db_session
            # Prevent close from breaking the session
            db_session.close = lambda: None
            result = get_credential_cached("cached_src", "CACHED_KEY")
        assert result == "cached-val"

    def test_uses_cache_on_second_call(self):
        from app.services import credential_service

        credential_service._cred_cache.clear()
        test_key = ("cache_hit_src", "CACHE_HIT_KEY")
        credential_service._cred_cache[test_key] = ("cached-result", time.time())

        result = get_credential_cached("cache_hit_src", "CACHE_HIT_KEY")
        assert result == "cached-result"

    def test_expired_cache_refreshes(self, db_session, monkeypatch):
        monkeypatch.setenv("STALE_KEY", "fresh-env-val")
        from app.services import credential_service

        # Set an expired cache entry
        stale_key = ("stale_src", "STALE_KEY")
        credential_service._cred_cache[stale_key] = ("old-val", time.time() - 120)

        with patch("app.database.SessionLocal") as mock_sl:
            mock_sl.return_value = db_session
            db_session.close = lambda: None
            result = get_credential_cached("stale_src", "STALE_KEY")
        # Should have fetched fresh value (env var fallback since DB has no entry)
        assert result == "fresh-env-val"


# ── decrypt failure fallback in get_credential ────────────────────────


class TestGetCredentialDecryptFallback:
    def test_decrypt_failure_falls_back_to_env_var(self, db_session, monkeypatch):
        monkeypatch.setenv("DECRYPT_FAIL_KEY", "env-fallback")
        _make_source(
            db_session,
            "decrypt_fail_src",
            ["DECRYPT_FAIL_KEY"],
            {"DECRYPT_FAIL_KEY": "totally-not-fernet-encrypted"},
        )
        result = get_credential(db_session, "decrypt_fail_src", "DECRYPT_FAIL_KEY")
        assert result == "env-fallback"

    def test_decrypt_failure_with_no_env_returns_none(self, db_session, monkeypatch):
        monkeypatch.delenv("DECRYPT_NO_ENV_KEY", raising=False)
        _make_source(
            db_session,
            "decrypt_no_env_src",
            ["DECRYPT_NO_ENV_KEY"],
            {"DECRYPT_NO_ENV_KEY": "invalid-ciphertext"},
        )
        result = get_credential(db_session, "decrypt_no_env_src", "DECRYPT_NO_ENV_KEY")
        assert result is None
