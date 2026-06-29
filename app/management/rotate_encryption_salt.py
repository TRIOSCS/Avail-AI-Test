"""rotate_encryption_salt.py — Rotate the Fernet salt for EncryptedText columns.

Re-encrypts the three at-rest ``EncryptedText`` columns on the ``users`` table
(refresh_token, access_token, password_hash) from an OLD ``ENCRYPTION_SALT`` to a
NEW one without orphaning existing ciphertext.

Why this exists: ``app/utils/encrypted_type.py`` derives a Fernet key from
``SECRET_KEY`` + ``ENCRYPTION_SALT``. Changing ``ENCRYPTION_SALT`` makes every
existing ciphertext undecryptable. This command bridges the gap — it decrypts each
value with the OLD salt's key and re-encrypts with the NEW salt's key in a single
transaction (PRE_ROLLOUT Gate 4).

The crypto is keyed off the values passed in (old/new salt + secret key), NOT the
live settings, so the command is independent of the running app's configuration and
can be run before OR after ``ENCRYPTION_SALT`` is changed in ``.env``.

Idempotent + resumable: a value already encrypted under the NEW salt is detected (it
decrypts with the new key, not the old) and left untouched, so re-running after a
partial/failed run — or running twice — is a safe no-op for already-rotated rows. A
value that decrypts with *neither* salt is reported and left intact (never discarded),
so a wrong-OLD-salt run can't destroy data.

NOTE — blast radius: ``ENCRYPTION_SALT`` also keys
``app/services/credential_service.py`` (supplier API keys in ``api_sources.credentials``).
That path degrades gracefully (falls back to env vars on a decrypt miss), so it does not
block rotation, but re-enter any DB-stored supplier credentials afterward. See
``docs/PRE_ROLLOUT_CHECKLIST.md`` Gate 4.

Usage:
    # Dry-run from the current (live) salt to a freshly generated one:
    python -m app.management.rotate_encryption_salt --new-salt "$(openssl rand -base64 32)" --dry-run
    # Then, with the SAME new salt, rotate for real:
    python -m app.management.rotate_encryption_salt --new-salt "<same value>"

    # Explicit OLD salt (e.g. .env already edited to the new value):
    python -m app.management.rotate_encryption_salt --old-salt "<old>" --new-salt "<new>"

    # NEW salt may also come from the NEW_ENCRYPTION_SALT env var.

Called by: operator, manually, during a salt rotation (PRE_ROLLOUT Gate 4).
Depends on: app.utils.encrypted_type.build_fernet, app.database.SessionLocal,
            app.config.settings (default OLD salt + secret key).
"""

import argparse
import hashlib
import os
from dataclasses import dataclass, field

from cryptography.fernet import Fernet, InvalidToken
from loguru import logger
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..utils.encrypted_type import build_fernet

# The EncryptedText columns this command rotates. All live on the ``users`` table.
TABLE = "users"
COLUMNS: tuple[str, ...] = ("refresh_token", "access_token", "password_hash")

# rotate_value() status codes.
ROTATED = "rotated"
ALREADY = "already"
UNDECRYPTABLE = "undecryptable"


@dataclass
class RotationStats:
    """Per-run, per-column tally of the rotation outcome."""

    users_scanned: int = 0
    rows_updated: int = 0
    rotated: dict[str, int] = field(default_factory=lambda: {c: 0 for c in COLUMNS})
    already: dict[str, int] = field(default_factory=lambda: {c: 0 for c in COLUMNS})
    undecryptable: dict[str, int] = field(default_factory=lambda: {c: 0 for c in COLUMNS})

    @property
    def total_rotated(self) -> int:
        return sum(self.rotated.values())

    @property
    def total_undecryptable(self) -> int:
        return sum(self.undecryptable.values())


def _salt_fingerprint(salt: str | None) -> str:
    """A short, non-reversible fingerprint of a salt for auditable logging.

    Never logs the salt itself. Empty/None salt is the legacy fallback.
    """
    if not salt:
        return "(legacy fallback salt)"
    return f"sha256:{hashlib.sha256(salt.encode()).hexdigest()[:12]}"


def rotate_value(raw: str | None, old_fernet: Fernet, new_fernet: Fernet) -> tuple[str | None, str | None]:
    """Rotate one stored ciphertext value from old_fernet to new_fernet.

    Returns ``(new_raw, status)``:
      - decrypts with OLD  -> re-encrypt with NEW -> ``(new_ciphertext, ROTATED)``
      - decrypts with NEW  -> already rotated      -> ``(raw,            ALREADY)``
      - decrypts with neither                      -> ``(raw,            UNDECRYPTABLE)``
      - raw is None/empty                          -> ``(raw,            None)``

    Never returns plaintext and never discards an undecryptable value, so a
    wrong-salt run can't destroy data — it just reports and leaves it intact.
    """
    if not raw:
        return raw, None
    raw_bytes = raw.encode()
    try:
        plaintext = old_fernet.decrypt(raw_bytes)
    except InvalidToken:
        # Not decryptable with OLD. Already rotated to NEW? Then it's done.
        try:
            new_fernet.decrypt(raw_bytes)
            return raw, ALREADY
        except InvalidToken:
            return raw, UNDECRYPTABLE
    return new_fernet.encrypt(plaintext).decode(), ROTATED


def rotate_salt(
    db: Session,
    *,
    old_salt: str | None,
    new_salt: str | None,
    secret_key: str,
    dry_run: bool = False,
) -> RotationStats:
    """Re-encrypt all EncryptedText user columns from old_salt to new_salt.

    Builds OLD and NEW Fernet keys from ``(secret_key, salt)`` — independent of the
    live app settings — and runs in a single transaction: it commits once at the end,
    or (in dry-run) rolls back so nothing is written.

    Reads/writes the columns as RAW text via Core SQL, deliberately bypassing the
    ``EncryptedText`` TypeDecorator (which would auto-decrypt/encrypt with the *live*
    key) so the rotation is driven solely by the supplied OLD/NEW salts.

    Raises ``ValueError`` if OLD and NEW resolve to the same salt (nothing to rotate).
    """
    if old_salt == new_salt or (not old_salt and not new_salt):
        raise ValueError("OLD and NEW salts resolve to the same value — nothing to rotate.")

    old_fernet = build_fernet(secret_key, old_salt)
    new_fernet = build_fernet(secret_key, new_salt)

    stats = RotationStats()
    select_cols = ", ".join(COLUMNS)
    rows = db.execute(text(f"SELECT id, {select_cols} FROM {TABLE}")).mappings().all()

    for row in rows:
        stats.users_scanned += 1
        updates: dict[str, str] = {}
        for col in COLUMNS:
            new_raw, status = rotate_value(row[col], old_fernet, new_fernet)
            if status == ROTATED:
                stats.rotated[col] += 1
                # ROTATED always yields a non-None ciphertext str (see rotate_value).
                updates[col] = new_raw
            elif status == ALREADY:
                stats.already[col] += 1
            elif status == UNDECRYPTABLE:
                stats.undecryptable[col] += 1
                logger.warning(
                    "{}.id={} {}: ciphertext decrypts with neither OLD nor NEW salt — left intact",
                    TABLE,
                    row["id"],
                    col,
                )
        if updates:
            stats.rows_updated += 1
            if not dry_run:
                set_clause = ", ".join(f"{c} = :{c}" for c in updates)
                db.execute(
                    text(f"UPDATE {TABLE} SET {set_clause} WHERE id = :_id"),
                    {**updates, "_id": row["id"]},
                )

    if dry_run:
        db.rollback()
    else:
        db.commit()
    return stats


def _log_summary(
    stats: RotationStats, old_salt: str | None, new_salt: str | None, secret_key: str, dry_run: bool
) -> None:
    """Print a clear before/after summary (salts fingerprinted, never logged raw)."""
    mode = "DRY RUN — no changes written" if dry_run else "LIVE — changes committed"
    secret_fp = hashlib.sha256(secret_key.encode()).hexdigest()[:12]
    logger.info("-" * 64)
    logger.info("ENCRYPTION SALT ROTATION  [{}]", mode)
    logger.info("  secret_key fingerprint : sha256:{} (unchanged on both sides)", secret_fp)
    logger.info("  OLD salt               : {}", _salt_fingerprint(old_salt))
    logger.info("  NEW salt               : {}", _salt_fingerprint(new_salt))
    logger.info("  users scanned          : {}", stats.users_scanned)
    logger.info("  rows {:<17}: {}", "to update" if dry_run else "updated", stats.rows_updated)
    for col in COLUMNS:
        logger.info(
            "    {:<14} rotated={} already={} undecryptable={}",
            col,
            stats.rotated[col],
            stats.already[col],
            stats.undecryptable[col],
        )
    if stats.total_undecryptable:
        logger.warning(
            "  {} value(s) decrypt with NEITHER salt — verify the OLD salt + SECRET_KEY before proceeding.",
            stats.total_undecryptable,
        )
    logger.info("-" * 64)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Rotate ENCRYPTION_SALT for the at-rest EncryptedText user columns "
        "(refresh_token, access_token, password_hash)."
    )
    parser.add_argument(
        "--old-salt",
        default=None,
        help="Current salt. Defaults to settings.encryption_salt (the live value). "
        "Empty means the legacy hard-coded salt.",
    )
    parser.add_argument(
        "--new-salt",
        default=None,
        help="New salt. Falls back to the NEW_ENCRYPTION_SALT env var. Generate one with: openssl rand -base64 32",
    )
    parser.add_argument("--dry-run", action="store_true", help="Report what would change; write nothing.")
    args = parser.parse_args()

    from ..config import settings
    from ..database import SessionLocal

    old_salt = args.old_salt if args.old_salt is not None else settings.encryption_salt
    new_salt = args.new_salt if args.new_salt is not None else os.environ.get("NEW_ENCRYPTION_SALT")

    if not new_salt:
        parser.error(
            "NEW salt is required: pass --new-salt or set NEW_ENCRYPTION_SALT "
            "(generate one with: openssl rand -base64 32)."
        )

    db = SessionLocal()
    try:
        stats = rotate_salt(
            db,
            old_salt=old_salt,
            new_salt=new_salt,
            secret_key=settings.secret_key,
            dry_run=args.dry_run,
        )
        _log_summary(stats, old_salt, new_salt, settings.secret_key, args.dry_run)
    finally:
        db.close()


if __name__ == "__main__":
    main()
