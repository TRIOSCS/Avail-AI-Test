"""Tests for app/utils/encrypted_type.py -- EncryptedText TypeDecorator."""

from unittest.mock import MagicMock, patch

from app.utils.encrypted_type import EncryptedText, _get_fernet


class TestGetFernet:
    @patch("app.config.settings", MagicMock(secret_key="test-secret-key-12345"))
    def test_returns_fernet_instance(self):
        from cryptography.fernet import Fernet
        f = _get_fernet()
        assert isinstance(f, Fernet)

    @patch("app.config.settings", MagicMock(secret_key="test-secret-key-12345"))
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
    def test_process_bind_param_error_returns_raw(self, mock_fernet):
        mock_fernet.side_effect = Exception("key error")

        et = EncryptedText()
        result = et.process_bind_param("fallback-value", MagicMock())
        assert result == "fallback-value"

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
        assert result == "plaintext-legacy"

    @patch("app.utils.encrypted_type._get_fernet")
    def test_process_result_value_other_error_returns_raw(self, mock_fernet):
        mock_f = MagicMock()
        mock_f.decrypt.side_effect = RuntimeError("unexpected")
        mock_fernet.return_value = mock_f

        et = EncryptedText()
        result = et.process_result_value("raw-value", MagicMock())
        assert result == "raw-value"
