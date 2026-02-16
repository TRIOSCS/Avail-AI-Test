"""
credential_service.py — Encrypted credential storage for API data sources.

Credentials are encrypted at rest using Fernet (AES-128-CBC + HMAC-SHA256).
The encryption key is derived from the app's SECRET_KEY via PBKDF2.

Business Rules:
- DB credentials take priority over env vars (fallback chain: DB → os.getenv)
- Only admins can read (masked) or write credentials
- Plaintext is never exposed via API — only masked values

Called by: routers/admin.py, routers/sources.py, search_service.py
Depends on: config.py (secret_key), models.py (ApiSource)
"""

import base64
import os

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from sqlalchemy.orm import Session

from ..config import settings
from ..models import ApiSource


def _get_fernet() -> Fernet:
    """Derive a Fernet key from the app secret and return a Fernet instance."""
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"availai-credential-salt-v1",
        iterations=100_000,
    )
    key = base64.urlsafe_b64encode(kdf.derive(settings.secret_key.encode()))
    return Fernet(key)


def encrypt_value(plaintext: str) -> str:
    """Encrypt a credential value. Returns a base64 Fernet token string."""
    f = _get_fernet()
    return f.encrypt(plaintext.encode()).decode()


def decrypt_value(ciphertext: str) -> str:
    """Decrypt a credential value. Returns the original plaintext."""
    f = _get_fernet()
    return f.decrypt(ciphertext.encode()).decode()


def mask_value(plaintext: str) -> str:
    """Mask a credential value for display: show last 4 chars only."""
    if not plaintext:
        return ""
    if len(plaintext) <= 4:
        return "****"
    return "●" * min(8, len(plaintext) - 4) + plaintext[-4:]


def get_credential(db: Session, source_name: str, env_var_name: str) -> str | None:
    """Get a credential value: DB first, then env var fallback."""
    src = db.query(ApiSource).filter_by(name=source_name).first()
    if src and src.credentials:
        encrypted = src.credentials.get(env_var_name)
        if encrypted:
            try:
                return decrypt_value(encrypted)
            except Exception:
                pass
    return os.getenv(env_var_name) or None


def get_all_credentials_for_source(db: Session, source_name: str) -> dict[str, str]:
    """Get all decrypted credentials for a source. DB first, env var fallback."""
    src = db.query(ApiSource).filter_by(name=source_name).first()
    if not src:
        return {}

    result = {}
    for var_name in (src.env_vars or []):
        val = None
        if src.credentials:
            encrypted = src.credentials.get(var_name)
            if encrypted:
                try:
                    val = decrypt_value(encrypted)
                except Exception:
                    pass
        if not val:
            val = os.getenv(var_name) or ""
        if val:
            result[var_name] = val
    return result


def credential_is_set(db: Session, source_name: str, env_var_name: str) -> bool:
    """Check if a credential has a value (DB or env var)."""
    src = db.query(ApiSource).filter_by(name=source_name).first()
    if src and src.credentials and src.credentials.get(env_var_name):
        return True
    return bool(os.getenv(env_var_name))
