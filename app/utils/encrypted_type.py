"""SQLAlchemy TypeDecorator for transparent Fernet encryption of text columns."""

import base64

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from loguru import logger
from sqlalchemy import Text, TypeDecorator

_fernet_instance = None


_LEGACY_SALT = b"availai-token-encryption-v1"


def _get_fernet():
    """Derive a Fernet key from the app secret key (cached after first call).

    Uses settings.encryption_salt if set (defense-in-depth), otherwise falls back to the
    legacy static salt for backward compatibility.
    """
    global _fernet_instance
    if _fernet_instance is not None:
        return _fernet_instance
    from ..config import settings

    if settings.encryption_salt:
        salt = settings.encryption_salt.encode()
    else:
        logger.warning("ENCRYPTION_SALT not set — using legacy static salt. Set ENCRYPTION_SALT for defense-in-depth.")
        salt = _LEGACY_SALT

    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=100_000,
    )
    key = base64.urlsafe_b64encode(kdf.derive(settings.secret_key.encode()))
    _fernet_instance = Fernet(key)
    return _fernet_instance


class EncryptedText(TypeDecorator):
    """Transparently encrypts/decrypts text values stored in the database."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        try:
            f = _get_fernet()
            return f.encrypt(value.encode()).decode()
        except Exception as e:
            logger.error(f"Encryption failed — refusing to store plaintext: {e}")
            raise ValueError("Encryption failed — cannot store sensitive data as plaintext") from e

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        try:
            f = _get_fernet()
            return f.decrypt(value.encode()).decode()
        except InvalidToken:
            logger.warning(
                "Fernet decryption failed (possible pre-migration plaintext data) — "
                "returning None instead of raw ciphertext"
            )
            return None
        except Exception:
            logger.warning("Unexpected decryption error — returning None for safety")
            return None
