"""tests/test_utils_encrypted_type.py — Tests for app/utils/encrypted_type.py."""

import os

os.environ["TESTING"] = "1"

from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet

from app.utils.encrypted_type import EncryptedText, build_fernet


class TestBuildFernet:
    def test_builds_valid_fernet(self):
        f = build_fernet("test_secret_key_for_testing_1234", None)
        assert isinstance(f, Fernet)

    def test_with_explicit_salt(self):
        f = build_fernet("test_secret_key_for_testing_1234", "my_salt")
        assert isinstance(f, Fernet)

    def test_different_salts_different_keys(self):
        f1 = build_fernet("same_key", "salt1")
        f2 = build_fernet("same_key", "salt2")
        # Encrypt with one, try to decrypt with the other — should fail
        token = f1.encrypt(b"test")
        with pytest.raises(Exception):
            f2.decrypt(token)

    def test_same_salt_same_key(self):
        f1 = build_fernet("same_key", "same_salt")
        f2 = build_fernet("same_key", "same_salt")
        token = f1.encrypt(b"test")
        result = f2.decrypt(token)
        assert result == b"test"

    def test_none_salt_uses_legacy(self):
        f = build_fernet("test_key", None)
        assert isinstance(f, Fernet)


class TestEncryptedText:
    def setup_method(self):
        """Reset the cached Fernet instance and set a test secret key."""
        import app.utils.encrypted_type as et

        et._fernet_instance = None
        self.type_decorator = EncryptedText()

    def _mock_settings(self):
        """Patch settings with a fixed test key."""
        mock_settings = type(
            "Settings",
            (),
            {
                "secret_key": "test-secret-key-for-testing-1234",
                "encryption_salt": "test-salt",
            },
        )()
        return mock_settings

    def test_none_input_returns_none_bind(self):
        result = self.type_decorator.process_bind_param(None, None)
        assert result is None

    def test_none_input_returns_none_result(self):
        result = self.type_decorator.process_result_value(None, None)
        assert result is None

    def test_encrypt_decrypt_roundtrip(self):
        with patch("app.utils.encrypted_type._get_fernet") as mock_fernet:
            f = build_fernet("test_key_1234567890123456", "test_salt")
            mock_fernet.return_value = f

            plaintext = "sensitive-token-value"
            encrypted = self.type_decorator.process_bind_param(plaintext, None)
            assert encrypted != plaintext
            assert isinstance(encrypted, str)

            decrypted = self.type_decorator.process_result_value(encrypted, None)
            assert decrypted == plaintext

    def test_invalid_token_returns_none(self):
        with patch("app.utils.encrypted_type._get_fernet") as mock_fernet:
            f = build_fernet("test_key_1234567890123456", "test_salt")
            mock_fernet.return_value = f

            result = self.type_decorator.process_result_value("not_valid_fernet_data", None)
            assert result is None

    def test_encrypt_empty_string(self):
        with patch("app.utils.encrypted_type._get_fernet") as mock_fernet:
            f = build_fernet("test_key_1234567890123456", "test_salt")
            mock_fernet.return_value = f

            encrypted = self.type_decorator.process_bind_param("", None)
            assert encrypted is not None
            assert encrypted != ""

    def test_encrypt_raises_on_failure(self):
        with patch("app.utils.encrypted_type._get_fernet") as mock_fernet:
            mock_fernet.return_value.encrypt.side_effect = Exception("fernet error")

            with pytest.raises(ValueError, match="Encryption failed"):
                self.type_decorator.process_bind_param("secret", None)
