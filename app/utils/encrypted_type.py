"""SQLAlchemy TypeDecorator for transparent Fernet encryption of text columns."""

from sqlalchemy import TypeDecorator, Text
from cryptography.fernet import Fernet, InvalidToken
import base64
import hashlib
import logging

log = logging.getLogger(__name__)


def _get_fernet():
    """Derive a Fernet key from the app secret key."""
    from ..config import settings
    key = hashlib.pbkdf2_hmac(
        "sha256",
        settings.secret_key.encode(),
        b"availai-token-encryption-v1",
        100_000,
    )
    return Fernet(base64.urlsafe_b64encode(key))


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
        except Exception:
            log.warning("Failed to encrypt value, storing as-is")
            return value

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        try:
            f = _get_fernet()
            return f.decrypt(value.encode()).decode()
        except (InvalidToken, Exception):
            # Value may be stored in plaintext (pre-migration data)
            return value
