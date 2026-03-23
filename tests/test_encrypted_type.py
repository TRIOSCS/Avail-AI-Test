"""Tests for app/utils/encrypted_type.py -- EncryptedText TypeDecorator.

Covers: Fernet key derivation, salt configuration, EncryptedText TypeDecorator,
and credential_service salt behavior.

Called by: pytest
Depends on: app.utils.encrypted_type, app.services.credential_service, app.config
"""

from unittest.mock import MagicMock, patch

import pytest
from cryptography.fernet import InvalidToken

from app.config import Settings
from app.utils.encrypted_type import EncryptedText, _get_fernet


@pytest.fixture(autouse=True)
def _reset_fernet_cache():
    """Reset the cached Fernet instance before and after each test."""
    import app.utils.encrypted_type as et_mod

    et_mod._fernet_instance = None
    yield
    et_mod._fernet_instance = None


def _make_settings(**overrides):
    """Helper to create a Settings instance with test defaults."""
    defaults = dict(
        secret_key="test-secret",
        encryption_salt="",
        database_url="sqlite://",
    )
    defaults.update(overrides)
    return Settings(**defaults)


class TestGetFernet:
    @patch("app.config.settings", MagicMock(secret_key="test-secret-key-12345", encryption_salt=""))
    def test_returns_fernet_instance(self):
        from cryptography.fernet import Fernet

        f = _get_fernet()
        assert isinstance(f, Fernet)

    @patch("app.config.settings", MagicMock(secret_key="test-secret-key-12345", encryption_salt=""))
    def test_roundtrip(self):
        f = _get_fernet()
        original = "sensitive-token-value"
        encrypted = f.encrypt(original.encode())
        decrypted = f.decrypt(encrypted).decode()
        assert decrypted == original


class TestEncryptedText:
    def test_impl_is_text(self):
        from sqlalchemy import Text

        et = EncryptedText()
        assert et.impl is Text or et.impl_instance.__class__.__name__ == "Text"

    def test_cache_ok(self):
        assert EncryptedText.cache_ok is True

    @patch("app.utils.encrypted_type._get_fernet")
    def test_process_bind_param_none(self, mock_fernet):
        et = EncryptedText()
        result = et.process_bind_param(None, MagicMock())
        assert result is None
        mock_fernet.assert_not_called()

    @patch("app.utils.encrypted_type._get_fernet")
    def test_process_bind_param_encrypts(self, mock_fernet):
        mock_f = MagicMock()
        mock_f.encrypt.return_value = b"encrypted-data"
        mock_fernet.return_value = mock_f

        et = EncryptedText()
        result = et.process_bind_param("secret-value", MagicMock())
        assert result == "encrypted-data"
        mock_f.encrypt.assert_called_once_with(b"secret-value")

    @patch("app.utils.encrypted_type._get_fernet")
    def test_process_bind_param_error_raises(self, mock_fernet):
        """Encryption failure raises ValueError (fail-closed, no plaintext stored)."""
        mock_fernet.side_effect = Exception("key error")

        et = EncryptedText()
        with pytest.raises(ValueError, match="Encryption failed"):
            et.process_bind_param("secret-value", MagicMock())

    @patch("app.utils.encrypted_type._get_fernet")
    def test_process_result_value_none(self, mock_fernet):
        et = EncryptedText()
        result = et.process_result_value(None, MagicMock())
        assert result is None
        mock_fernet.assert_not_called()

    @patch("app.utils.encrypted_type._get_fernet")
    def test_process_result_value_decrypts(self, mock_fernet):
        mock_f = MagicMock()
        mock_f.decrypt.return_value = b"decrypted-value"
        mock_fernet.return_value = mock_f

        et = EncryptedText()
        result = et.process_result_value("encrypted-data", MagicMock())
        assert result == "decrypted-value"
        mock_f.decrypt.assert_called_once_with(b"encrypted-data")

    @patch("app.utils.encrypted_type._get_fernet")
    def test_process_result_value_invalid_token_returns_raw(self, mock_fernet):
        from cryptography.fernet import InvalidToken

        mock_f = MagicMock()
        mock_f.decrypt.side_effect = InvalidToken()
        mock_fernet.return_value = mock_f

        et = EncryptedText()
        result = et.process_result_value("plaintext-legacy", MagicMock())
        assert result is None

    @patch("app.utils.encrypted_type._get_fernet")
    def test_process_result_value_other_error_returns_raw(self, mock_fernet):
        mock_f = MagicMock()
        mock_f.decrypt.side_effect = RuntimeError("unexpected")
        mock_fernet.return_value = mock_f

        et = EncryptedText()
        result = et.process_result_value("raw-value", MagicMock())
        assert result is None


class TestEncryptedTypeSalt:
    """Tests for encryption_salt in encrypted_type._get_fernet()."""

    def test_custom_salt_produces_different_key(self, monkeypatch):
        """Custom salt produces a different Fernet key than legacy static salt."""
        import app.utils.encrypted_type as et_mod

        # Encrypt with legacy (empty) salt
        monkeypatch.setattr("app.config.settings", _make_settings(encryption_salt=""))
        et_mod._fernet_instance = None
        ciphertext = et_mod._get_fernet().encrypt(b"sensitive-data")

        # Switch to custom salt — should NOT decrypt legacy ciphertext
        monkeypatch.setattr("app.config.settings", _make_settings(encryption_salt="my-unique-deployment-salt"))
        et_mod._fernet_instance = None
        with pytest.raises(InvalidToken):
            et_mod._get_fernet().decrypt(ciphertext)

    def test_different_salts_cannot_cross_decrypt(self, monkeypatch):
        """Two different non-empty salts produce incompatible Fernet keys."""
        import app.utils.encrypted_type as et_mod

        monkeypatch.setattr("app.config.settings", _make_settings(encryption_salt="salt-alpha"))
        et_mod._fernet_instance = None
        ciphertext_a = et_mod._get_fernet().encrypt(b"hello")

        monkeypatch.setattr("app.config.settings", _make_settings(encryption_salt="salt-beta"))
        et_mod._fernet_instance = None
        with pytest.raises(InvalidToken):
            et_mod._get_fernet().decrypt(ciphertext_a)

    def test_empty_salt_falls_back_to_legacy(self, monkeypatch):
        """Empty encryption_salt uses legacy static salt; round-trip works."""
        import app.utils.encrypted_type as et_mod

        monkeypatch.setattr("app.config.settings", _make_settings(encryption_salt=""))
        et_mod._fernet_instance = None
        fernet = et_mod._get_fernet()
        assert fernet.decrypt(fernet.encrypt(b"round-trip")) == b"round-trip"

    def test_same_salt_produces_same_key(self, monkeypatch):
        """Same salt + same secret_key produces the same Fernet key across resets."""
        import app.utils.encrypted_type as et_mod

        monkeypatch.setattr("app.config.settings", _make_settings(encryption_salt="stable-salt"))

        et_mod._fernet_instance = None
        ciphertext = et_mod._get_fernet().encrypt(b"data")

        et_mod._fernet_instance = None
        assert et_mod._get_fernet().decrypt(ciphertext) == b"data"

    def test_empty_salt_logs_warning(self, monkeypatch):
        """Empty encryption_salt emits a warning about legacy salt."""
        import io

        import app.utils.encrypted_type as et_mod

        monkeypatch.setattr("app.config.settings", _make_settings(encryption_salt=""))
        et_mod._fernet_instance = None

        # Capture loguru output by adding a temporary sink
        log_capture = io.StringIO()
        from loguru import logger

        handler_id = logger.add(log_capture, format="{message}", level="WARNING")
        try:
            et_mod._get_fernet()
            log_output = log_capture.getvalue()
            assert "ENCRYPTION_SALT not set" in log_output
        finally:
            logger.remove(handler_id)


class TestCredentialServiceSalt:
    """Tests for encryption_salt in credential_service._get_fernet()."""

    def test_custom_salt_produces_different_key(self, monkeypatch):
        """Credential service uses custom salt when set."""
        import app.services.credential_service as cs_mod

        monkeypatch.setattr("app.services.credential_service.settings", _make_settings(encryption_salt=""))
        ciphertext = cs_mod._get_fernet().encrypt(b"api-key-value")

        monkeypatch.setattr(
            "app.services.credential_service.settings",
            _make_settings(encryption_salt="deploy-unique-salt"),
        )
        with pytest.raises(InvalidToken):
            cs_mod._get_fernet().decrypt(ciphertext)

    def test_empty_salt_falls_back_to_legacy(self, monkeypatch):
        """Credential service falls back to legacy salt when empty."""
        import app.services.credential_service as cs_mod

        monkeypatch.setattr("app.services.credential_service.settings", _make_settings(encryption_salt=""))
        fernet = cs_mod._get_fernet()
        assert fernet.decrypt(fernet.encrypt(b"credential-round-trip")) == b"credential-round-trip"

    def test_encrypt_decrypt_roundtrip_with_salt(self, monkeypatch):
        """encrypt_value/decrypt_value round-trip with custom salt."""
        import app.services.credential_service as cs_mod

        monkeypatch.setattr(
            "app.services.credential_service.settings",
            _make_settings(encryption_salt="my-salt"),
        )
        encrypted = cs_mod.encrypt_value("super-secret-api-key")
        assert encrypted != "super-secret-api-key"
        assert cs_mod.decrypt_value(encrypted) == "super-secret-api-key"
