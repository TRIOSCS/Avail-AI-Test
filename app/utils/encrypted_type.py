"""SQLAlchemy TypeDecorator for transparent Fernet encryption of text columns."""

from sqlalchemy import TypeDecorator, Text
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
import base64
import logging

log = logging.getLogger(__name__)


def _get_fernet():
    """Derive a Fernet key from the app secret key."""
    from ..config import settings
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"availai-token-encryption-v1",
        iterations=100_000,
    )
    key = base64.urlsafe_b64encode(kdf.derive(settings.secret_key.encode()))
    return Fernet(key)


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
