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

# ── encrypt / decrypt ────────────────────────────────────────────────


class TestEncryptDecrypt:
    """Fernet encryption round-trip tests."""

    def test_encrypt_returns_different_string(self):
        plaintext = "my-secret-api-key"
        encrypted = encrypt_value(plaintext)
        assert encrypted != plaintext

    def test_round_trip(self):
        plaintext = "sk-ant-api03-XXXXXX"
        assert decrypt_value(encrypt_value(plaintext)) == plaintext

    def test_round_trip_unicode(self):
        plaintext = "pässwörd-with-üñíçödé"
        assert decrypt_value(encrypt_value(plaintext)) == plaintext

    def test_round_trip_empty_string(self):
        assert decrypt_value(encrypt_value("")) == ""

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
        encrypted = encrypt_value("db-secret-value")
        src = ApiSource(
            name="test_src",
            display_name="Test Source",
            category="api",
            source_type="aggregator",
            status="active",
            env_vars=["MY_KEY"],
            credentials={"MY_KEY": encrypted},
        )
        db_session.add(src)
        db_session.commit()

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
        encrypted = encrypt_value("db-value")
        src = ApiSource(
            name="priority_src",
            display_name="Priority Source",
            category="api",
            source_type="aggregator",
            status="active",
            env_vars=["MY_KEY"],
            credentials={"MY_KEY": encrypted},
        )
        db_session.add(src)
        db_session.commit()

        result = get_credential(db_session, "priority_src", "MY_KEY")
        assert result == "db-value"


# ── credential_is_set ─────────────────────────────────────────────────


class TestCredentialIsSet:
    """Boolean check for credential existence."""

    def test_true_when_db_credential_exists(self, db_session):
        encrypted = encrypt_value("some-value")
        src = ApiSource(
            name="check_src",
            display_name="Check Source",
            category="api",
            source_type="aggregator",
            status="active",
            env_vars=["CHECK_KEY"],
            credentials={"CHECK_KEY": encrypted},
        )
        db_session.add(src)
        db_session.commit()

        assert credential_is_set(db_session, "check_src", "CHECK_KEY") is True

    def test_true_when_env_var_set(self, db_session, monkeypatch):
        monkeypatch.setenv("ENV_CHECK_KEY", "something")
        assert credential_is_set(db_session, "nonexistent", "ENV_CHECK_KEY") is True

    def test_false_when_nothing_set(self, db_session, monkeypatch):
        monkeypatch.delenv("NOPE_KEY", raising=False)
        assert credential_is_set(db_session, "nonexistent", "NOPE_KEY") is False
