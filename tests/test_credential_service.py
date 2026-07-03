"""test_credential_service.py — Round-trip tests for credential encrypt/decrypt.

Covers encrypt_value, decrypt_value, mask_value, get_credential, and
credential_is_set from the credential service.

Called by: pytest
Depends on: app.services.credential_service, app.models.ApiSource
"""

import os

os.environ.setdefault("TESTING", "1")

import pytest
from cryptography.fernet import InvalidToken

from app.models import ApiSource
from app.services.credential_service import (
    credential_is_set,
    decrypt_value,
    encrypt_value,
    get_credential,
    mask_value,
)

# ── Helpers ───────────────────────────────────────────────────────────


def _add_api_source(db_session, name, key, plaintext):
    """Persist an ApiSource holding one encrypted credential under ``key``."""
    src = ApiSource(
        name=name,
        display_name=f"{name} display",
        category="api",
        source_type="aggregator",
        status="active",
        env_vars=[key],
        credentials={key: encrypt_value(plaintext)},
    )
    db_session.add(src)
    db_session.commit()
    return src


# ── encrypt / decrypt ────────────────────────────────────────────────


class TestEncryptDecrypt:
    """Fernet encryption round-trip tests."""

    def test_encrypt_returns_different_string(self):
        plaintext = "my-secret-api-key"
        encrypted = encrypt_value(plaintext)
        assert encrypted != plaintext

    @pytest.mark.parametrize(
        "plaintext",
        [
            pytest.param("sk-ant-api03-XXXXXX", id="ascii"),
            pytest.param("pässwörd-with-üñíçödé", id="unicode"),
            pytest.param("", id="empty_string"),
        ],
    )
    def test_round_trip(self, plaintext):
        assert decrypt_value(encrypt_value(plaintext)) == plaintext

    def test_decrypt_corrupted_token_raises(self):
        with pytest.raises((InvalidToken, Exception)):
            decrypt_value("not-a-valid-fernet-token")

    def test_decrypt_tampered_token_raises(self):
        encrypted = encrypt_value("real-secret")
        tampered = encrypted[:-4] + "XXXX"
        with pytest.raises((InvalidToken, Exception)):
            decrypt_value(tampered)


# ── mask_value ────────────────────────────────────────────────────────


class TestMaskValue:
    """Masking tests for credential display."""

    def test_long_string_shows_last_four(self):
        result = mask_value("my-secret-api-key-1234")
        assert result.endswith("1234")
        assert "my-secret" not in result

    def test_mask_contains_bullet_chars(self):
        result = mask_value("abcdefghijklmnop")
        assert "●" in result

    def test_short_string_returns_stars(self):
        assert mask_value("abc") == "****"
        assert mask_value("abcd") == "****"

    def test_empty_string_returns_empty(self):
        assert mask_value("") == ""

    def test_none_returns_empty(self):
        # mask_value checks `if not plaintext`
        assert mask_value(None) == ""

    def test_five_char_string(self):
        result = mask_value("abcde")
        assert result.endswith("bcde")
        assert len(result) == 5  # 1 bullet + last 4


# ── get_credential (DB + env fallback) ────────────────────────────────


class TestGetCredential:
    """DB-first, env-fallback credential retrieval."""

    def test_returns_decrypted_db_value(self, db_session):
        _add_api_source(db_session, "test_src", "MY_KEY", "db-secret-value")

        result = get_credential(db_session, "test_src", "MY_KEY")
        assert result == "db-secret-value"

    def test_falls_back_to_env_var(self, db_session, monkeypatch):
        monkeypatch.setenv("FALLBACK_KEY", "env-secret-value")
        result = get_credential(db_session, "nonexistent_src", "FALLBACK_KEY")
        assert result == "env-secret-value"

    def test_returns_none_when_nothing_set(self, db_session, monkeypatch):
        monkeypatch.delenv("MISSING_KEY", raising=False)
        result = get_credential(db_session, "nonexistent_src", "MISSING_KEY")
        assert result is None

    def test_db_takes_priority_over_env(self, db_session, monkeypatch):
        monkeypatch.setenv("MY_KEY", "env-value")
        _add_api_source(db_session, "priority_src", "MY_KEY", "db-value")

        result = get_credential(db_session, "priority_src", "MY_KEY")
        assert result == "db-value"


# ── credential_is_set ─────────────────────────────────────────────────


class TestCredentialIsSet:
    """Boolean check for credential existence."""

    def test_true_when_db_credential_exists(self, db_session):
        _add_api_source(db_session, "check_src", "CHECK_KEY", "some-value")

        assert credential_is_set(db_session, "check_src", "CHECK_KEY") is True

    def test_true_when_env_var_set(self, db_session, monkeypatch):
        monkeypatch.setenv("ENV_CHECK_KEY", "something")
        assert credential_is_set(db_session, "nonexistent", "ENV_CHECK_KEY") is True

    def test_false_when_nothing_set(self, db_session, monkeypatch):
        monkeypatch.delenv("NOPE_KEY", raising=False)
        assert credential_is_set(db_session, "nonexistent", "NOPE_KEY") is False


# ── _get_fernet with ENCRYPTION_SALT set ─────────────────────────────


class TestGetFernetWithSalt:
    """Exercises line 38 — the settings.encryption_salt branch inside _get_fernet()."""

    def test_encrypt_decrypt_round_trip_with_salt(self, monkeypatch):
        from types import SimpleNamespace

        import app.services.credential_service as cred_svc

        fake_settings = SimpleNamespace(
            secret_key=cred_svc.settings.secret_key,
            encryption_salt="custom-test-salt",
        )
        monkeypatch.setattr(cred_svc, "settings", fake_settings)

        # Both encrypt_value and decrypt_value call _get_fernet() which now
        # takes the salt branch (line 38); the round-trip must still hold.
        plaintext = "salted-secret-value"
        assert decrypt_value(encrypt_value(plaintext)) == plaintext


# ── _try_decrypt ──────────────────────────────────────────────────────


class TestTryDecrypt:
    """Unit tests for _try_decrypt (lines 80-86 — the except handler)."""

    def test_returns_none_for_none_input(self):
        from app.services.credential_service import _try_decrypt

        assert _try_decrypt(None, "SOME_VAR") is None

    def test_returns_plaintext_for_valid_ciphertext(self):
        from app.services.credential_service import _try_decrypt

        encrypted = encrypt_value("my-credential-value")
        assert _try_decrypt(encrypted, "SOME_VAR") == "my-credential-value"

    def test_returns_none_and_logs_on_corrupt_ciphertext(self):
        from app.services.credential_service import _try_decrypt

        # Junk input triggers the except block at lines 84-86.
        result = _try_decrypt("not-valid-fernet-data", "BAD_VAR")
        assert result is None


# ── get_credential with decrypt failure ───────────────────────────────


class TestGetCredentialDecryptFailure:
    """Exercises lines 107-114 (decrypt error path) and line 117 (env fallback warning)."""

    def _add_corrupted_source(self, db_session, name: str, key: str) -> None:
        src = ApiSource(
            name=name,
            display_name=f"{name} display",
            category="api",
            source_type="aggregator",
            status="active",
            env_vars=[key],
            credentials={key: "corrupted-not-valid-fernet-data"},
        )
        db_session.add(src)
        db_session.commit()

    def test_returns_none_when_decrypt_fails_and_no_env_var(self, db_session, monkeypatch):
        monkeypatch.delenv("CORRUPT_KEY_A", raising=False)
        self._add_corrupted_source(db_session, "corrupt_src_a", "CORRUPT_KEY_A")
        result = get_credential(db_session, "corrupt_src_a", "CORRUPT_KEY_A")
        assert result is None

    def test_returns_env_var_when_decrypt_fails_with_fallback(self, db_session, monkeypatch):
        # Covers line 117 — logger.warning for env var fallback after decrypt failure.
        monkeypatch.setenv("CORRUPT_KEY_B", "env-fallback-value")
        self._add_corrupted_source(db_session, "corrupt_src_b", "CORRUPT_KEY_B")
        result = get_credential(db_session, "corrupt_src_b", "CORRUPT_KEY_B")
        assert result == "env-fallback-value"


# ── get_all_credentials_for_source ────────────────────────────────────


class TestGetAllCredentialsForSource:
    """Tests for lines 126-137 — the loop over env_vars decrypting each one."""

    def test_returns_empty_dict_when_source_not_found(self, db_session):
        from app.services.credential_service import get_all_credentials_for_source

        assert get_all_credentials_for_source(db_session, "ghost_source_xyz") == {}

    def test_returns_all_decrypted_db_credentials(self, db_session):
        from app.services.credential_service import get_all_credentials_for_source

        src = ApiSource(
            name="all_creds_src_1",
            display_name="All Creds 1",
            category="api",
            source_type="aggregator",
            status="active",
            env_vars=["KEY_ALPHA", "KEY_BETA"],
            credentials={
                "KEY_ALPHA": encrypt_value("alpha-val"),
                "KEY_BETA": encrypt_value("beta-val"),
            },
        )
        db_session.add(src)
        db_session.commit()

        result = get_all_credentials_for_source(db_session, "all_creds_src_1")
        assert result == {"KEY_ALPHA": "alpha-val", "KEY_BETA": "beta-val"}

    def test_falls_back_to_env_var_for_missing_db_credential(self, db_session, monkeypatch):
        from app.services.credential_service import get_all_credentials_for_source

        monkeypatch.setenv("ENV_ONLY_KEY_X", "env-only-val")
        src = ApiSource(
            name="all_creds_src_2",
            display_name="All Creds 2",
            category="api",
            source_type="aggregator",
            status="active",
            env_vars=["ENV_ONLY_KEY_X"],
            credentials={},
        )
        db_session.add(src)
        db_session.commit()

        result = get_all_credentials_for_source(db_session, "all_creds_src_2")
        assert result == {"ENV_ONLY_KEY_X": "env-only-val"}

    def test_omits_var_when_no_db_cred_and_no_env_var(self, db_session, monkeypatch):
        from app.services.credential_service import get_all_credentials_for_source

        monkeypatch.delenv("NO_VAL_KEY", raising=False)
        src = ApiSource(
            name="all_creds_src_3",
            display_name="All Creds 3",
            category="api",
            source_type="aggregator",
            status="active",
            env_vars=["NO_VAL_KEY"],
            credentials={},
        )
        db_session.add(src)
        db_session.commit()

        result = get_all_credentials_for_source(db_session, "all_creds_src_3")
        assert result == {}


# ── get_credentials_batch ─────────────────────────────────────────────


class TestGetCredentialsBatch:
    """Tests for lines 157-167 — the batch loop."""

    def test_returns_db_credential(self, db_session):
        from app.services.credential_service import get_credentials_batch

        src = ApiSource(
            name="batch_src_1",
            display_name="Batch 1",
            category="api",
            source_type="aggregator",
            status="active",
            env_vars=["BATCH_KEY_A"],
            credentials={"BATCH_KEY_A": encrypt_value("batch-val-a")},
        )
        db_session.add(src)
        db_session.commit()

        result = get_credentials_batch(db_session, [("batch_src_1", "BATCH_KEY_A")])
        assert result == {("batch_src_1", "BATCH_KEY_A"): "batch-val-a"}

    def test_falls_back_to_env_var(self, db_session, monkeypatch):
        from app.services.credential_service import get_credentials_batch

        monkeypatch.setenv("BATCH_ENV_VAR", "env-batch-val")
        result = get_credentials_batch(db_session, [("no_src_batch", "BATCH_ENV_VAR")])
        assert result == {("no_src_batch", "BATCH_ENV_VAR"): "env-batch-val"}

    def test_returns_none_when_no_value_anywhere(self, db_session, monkeypatch):
        from app.services.credential_service import get_credentials_batch

        monkeypatch.delenv("MISSING_BATCH_VAR", raising=False)
        result = get_credentials_batch(db_session, [("no_src_batch2", "MISSING_BATCH_VAR")])
        assert result == {("no_src_batch2", "MISSING_BATCH_VAR"): None}

    def test_handles_multiple_sources_and_env_mix(self, db_session, monkeypatch):
        from app.services.credential_service import get_credentials_batch

        monkeypatch.setenv("MULTI_ENV_VAR", "env-multi-val")
        src = ApiSource(
            name="batch_src_2",
            display_name="Batch 2",
            category="api",
            source_type="aggregator",
            status="active",
            env_vars=["MULTI_DB_KEY"],
            credentials={"MULTI_DB_KEY": encrypt_value("db-multi-val")},
        )
        db_session.add(src)
        db_session.commit()

        requests_list = [
            ("batch_src_2", "MULTI_DB_KEY"),
            ("no_src_multi", "MULTI_ENV_VAR"),
        ]
        result = get_credentials_batch(db_session, requests_list)
        assert result[("batch_src_2", "MULTI_DB_KEY")] == "db-multi-val"
        assert result[("no_src_multi", "MULTI_ENV_VAR")] == "env-multi-val"


# ── get_credential_cached ─────────────────────────────────────────────


class TestGetCredentialCached:
    """Tests for lines 181-195 — in-process TTL cache + lazy SessionLocal."""

    def setup_method(self):
        import app.services.credential_service as cred_svc

        cred_svc._cred_cache.clear()

    def teardown_method(self):
        import app.services.credential_service as cred_svc

        cred_svc._cred_cache.clear()

    def test_cache_miss_calls_db_and_stores_result(self):
        from unittest.mock import MagicMock, patch

        from app.services.credential_service import _cred_cache, get_credential_cached

        mock_db = MagicMock()

        with patch("app.database.SessionLocal", return_value=mock_db):
            with patch(
                "app.services.credential_service.get_credential",
                return_value="fetched-val",
            ) as mock_get:
                result = get_credential_cached("cac_src1", "CAC_VAR1")

        assert result == "fetched-val"
        mock_get.assert_called_once_with(mock_db, "cac_src1", "CAC_VAR1")
        mock_db.close.assert_called_once()
        assert ("cac_src1", "CAC_VAR1") in _cred_cache

    def test_cache_hit_skips_db_entirely(self):
        import time
        from unittest.mock import patch

        from app.services.credential_service import _cred_cache, get_credential_cached

        _cred_cache[("cac_src2", "CAC_VAR2")] = ("cached-hit-val", time.time())

        with patch("app.database.SessionLocal") as mock_sl:
            result = get_credential_cached("cac_src2", "CAC_VAR2")

        assert result == "cached-hit-val"
        mock_sl.assert_not_called()

    def test_expired_cache_entry_triggers_refetch(self):
        import time
        from unittest.mock import MagicMock, patch

        import app.services.credential_service as cred_svc

        old_time = time.time() - (cred_svc._CACHE_TTL + 5)
        cred_svc._cred_cache[("cac_src3", "CAC_VAR3")] = ("stale-val", old_time)

        mock_db = MagicMock()

        with patch("app.database.SessionLocal", return_value=mock_db):
            with patch(
                "app.services.credential_service.get_credential",
                return_value="fresh-val",
            ) as mock_get:
                result = cred_svc.get_credential_cached("cac_src3", "CAC_VAR3")

        assert result == "fresh-val"
        mock_get.assert_called_once()
        mock_db.close.assert_called_once()
