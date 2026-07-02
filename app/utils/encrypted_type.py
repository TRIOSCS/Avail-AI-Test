"""SQLAlchemy TypeDecorator for transparent Fernet encryption of text columns."""

import base64

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from loguru import logger
from sqlalchemy import Text, TypeDecorator

_fernet_instance = None


_LEGACY_SALT = b"availai-token-encryption-v1"


def build_fernet(secret_key: str, salt: str | None) -> Fernet:
    """Build a Fernet from an explicit secret key + salt string.

    An empty/None ``salt`` falls back to the legacy static salt (backward
    compatibility). This is the single key-derivation point: the SQLAlchemy type's
    normal path reaches it via :func:`_get_fernet`, and the salt-rotation management
    command (``app.management.rotate_encryption_salt``) calls it directly to build a
    Fernet for an *arbitrary* OLD/NEW salt, independent of the live settings.
    """
    salt_bytes = salt.encode() if salt else _LEGACY_SALT
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt_bytes,
        iterations=100_000,
    )
    key = base64.urlsafe_b64encode(kdf.derive(secret_key.encode()))
    return Fernet(key)


def _get_fernet():
    """Derive a Fernet key from the app secret key (cached after first call).

    Uses settings.encryption_salt if set (defense-in-depth), otherwise falls back to the
    legacy static salt for backward compatibility.
    """
    global _fernet_instance
    if _fernet_instance is not None:
        return _fernet_instance
    from ..config import settings

    if not settings.encryption_salt:
        logger.warning("ENCRYPTION_SALT not set — using legacy static salt. Set ENCRYPTION_SALT for defense-in-depth.")
    _fernet_instance = build_fernet(settings.secret_key, settings.encryption_salt)
    return _fernet_instance


_CANARY_KEY = "encryption_canary"
_CANARY_SENTINEL = "AVAIL-ENC-CANARY-v1"


def verify_encryption_canary(db) -> None:
    """Boot-time self-test: prove the live key can decrypt data encrypted under itself.

    A wrong ENCRYPTION_SALT/SECRET_KEY derives a valid-but-DIFFERENT Fernet key, so every
    EncryptedText column silently decrypts to None (InvalidToken) — the app's stored
    credentials (M365 tokens, API keys, password hashes) all read as empty with only a
    per-field warning, indistinguishable from genuinely-empty data. The per-field decrypt
    cannot tell "wrong key" from "legit pre-migration plaintext", so detection must live
    here, once, at startup. First boot under a given key bootstraps the canary; later boots
    verify it and FAIL LOUD (raise) on mismatch instead of silently emptying every secret.

    Called from app/startup.py after the system_config table exists. No-op-safe if the
    table is missing (pre-migration) — it just skips.
    """
    from sqlalchemy.exc import DatabaseError, SQLAlchemyError

    from ..models.config import SystemConfig

    f = _get_fernet()
    try:
        row = db.query(SystemConfig).filter(SystemConfig.key == _CANARY_KEY).first()
    except (SQLAlchemyError, DatabaseError):
        db.rollback()
        return  # table not yet created — nothing to verify against
    if row is None:
        token = f.encrypt(_CANARY_SENTINEL.encode()).decode()
        db.add(SystemConfig(key=_CANARY_KEY, value=token, description="Encryption self-test canary"))
        db.commit()
        logger.info("Encryption canary bootstrapped under the current key.")
        return
    try:
        decrypted = f.decrypt(row.value.encode()).decode()
    except InvalidToken as e:
        raise RuntimeError(
            "ENCRYPTION MISCONFIG: the encryption canary failed to decrypt — the live "
            "ENCRYPTION_SALT/SECRET_KEY does not match the key that encrypted stored data. "
            "Refusing to boot silently, since every encrypted credential (M365 tokens, API "
            "keys, password hashes) would otherwise read as empty. Restore the correct "
            "ENCRYPTION_SALT or run the salt-rotation migration."
        ) from e
    if decrypted != _CANARY_SENTINEL:
        raise RuntimeError(f"ENCRYPTION MISCONFIG: canary decrypted to an unexpected value ({decrypted!r}).")
    logger.debug("Encryption canary verified.")


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
