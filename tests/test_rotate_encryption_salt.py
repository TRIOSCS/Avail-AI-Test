"""Tests for app/management/rotate_encryption_salt.py and the build_fernet refactor.

Covers: arbitrary-salt Fernet construction, the per-value rotation state machine
(rotated / already / undecryptable), the full DB rotation (dry-run + live),
idempotency/resumability, and the encrypt-old -> rotate -> decrypt-new round trip
through the ORM EncryptedText type.

Called by: pytest
Depends on: app.utils.encrypted_type, app.management.rotate_encryption_salt,
            app.models.auth.User, app.config
"""

import pytest
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import text

import app.utils.encrypted_type as et_mod
from app.config import Settings
from app.management.rotate_encryption_salt import (
    ALREADY,
    ROTATED,
    UNDECRYPTABLE,
    rotate_salt,
    rotate_value,
)
from app.models.auth import User
from app.utils.encrypted_type import build_fernet

SECRET = "test-secret-key"


@pytest.fixture(autouse=True)
def _reset_fernet_cache():
    """Reset the cached Fernet instance before and after each test."""
    et_mod._fernet_instance = None
    yield
    et_mod._fernet_instance = None


def _make_settings(**overrides):
    defaults = dict(secret_key=SECRET, encryption_salt="", database_url="sqlite://")
    defaults.update(overrides)
    return Settings(**defaults)


# ── build_fernet refactor ────────────────────────────────────────────


class TestBuildFernet:
    def test_returns_fernet(self):
        assert isinstance(build_fernet(SECRET, "salt-a"), Fernet)

    def test_same_inputs_same_key(self):
        a = build_fernet(SECRET, "salt-a")
        b = build_fernet(SECRET, "salt-a")
        assert b.decrypt(a.encrypt(b"hi")) == b"hi"

    def test_different_salts_cannot_cross_decrypt(self):
        a = build_fernet(SECRET, "salt-a")
        b = build_fernet(SECRET, "salt-b")
        with pytest.raises(InvalidToken):
            b.decrypt(a.encrypt(b"secret"))

    def test_empty_salt_is_legacy_fallback(self):
        """Empty and None salt both resolve to the legacy static salt."""
        empty = build_fernet(SECRET, "")
        none_ = build_fernet(SECRET, None)
        assert none_.decrypt(empty.encrypt(b"x")) == b"x"

    def test_get_fernet_matches_build_fernet(self, monkeypatch):
        """The SQLAlchemy normal path derives the same key as build_fernet."""
        monkeypatch.setattr("app.config.settings", _make_settings(encryption_salt="live-salt"))
        et_mod._fernet_instance = None
        live = et_mod._get_fernet()
        manual = build_fernet(SECRET, "live-salt")
        assert manual.decrypt(live.encrypt(b"payload")) == b"payload"


# ── rotate_value state machine ───────────────────────────────────────


class TestRotateValue:
    def setup_method(self):
        self.old = build_fernet(SECRET, "old-salt")
        self.new = build_fernet(SECRET, "new-salt")

    def test_none_passthrough(self):
        assert rotate_value(None, self.old, self.new) == (None, None)

    def test_empty_passthrough(self):
        assert rotate_value("", self.old, self.new) == ("", None)

    def test_rotates_old_to_new(self):
        raw = self.old.encrypt(b"my-token").decode()
        new_raw, status = rotate_value(raw, self.old, self.new)
        assert status == ROTATED
        # New ciphertext decrypts with the NEW key to the original plaintext...
        assert self.new.decrypt(new_raw.encode()) == b"my-token"
        # ...and the OLD key can no longer decrypt it.
        with pytest.raises(InvalidToken):
            self.old.decrypt(new_raw.encode())

    def test_already_rotated_is_noop(self):
        raw = self.new.encrypt(b"my-token").decode()
        new_raw, status = rotate_value(raw, self.old, self.new)
        assert status == ALREADY
        assert new_raw == raw

    def test_undecryptable_left_intact(self):
        new_raw, status = rotate_value("not-a-fernet-token", self.old, self.new)
        assert status == UNDECRYPTABLE
        assert new_raw == "not-a-fernet-token"


# ── full DB rotation ─────────────────────────────────────────────────


def _insert_user_under_salt(db, monkeypatch, salt, **enc):
    """Insert a User whose EncryptedText columns are written under ``salt``.

    Returns the new user id and detaches all ORM objects so later reads reload fresh
    from the DB under whatever salt the live settings then carry.
    """
    monkeypatch.setattr("app.config.settings", _make_settings(encryption_salt=salt))
    et_mod._fernet_instance = None
    user = User(email="rot@trioscs.com", **enc)
    db.add(user)
    db.commit()
    db.refresh(user)
    uid = user.id
    db.expunge_all()
    return uid


class TestRotateSaltDB:
    def test_full_rotation_roundtrip(self, db_session, monkeypatch):
        uid = _insert_user_under_salt(
            db_session,
            monkeypatch,
            "old-salt",
            refresh_token="refresh-abc",
            access_token="access-xyz",
            password_hash="pw$hash",
        )

        stats = rotate_salt(db_session, old_salt="old-salt", new_salt="new-salt", secret_key=SECRET)
        assert stats.users_scanned == 1
        assert stats.rows_updated == 1
        assert stats.rotated["refresh_token"] == 1
        assert stats.rotated["access_token"] == 1
        assert stats.rotated["password_hash"] == 1
        assert stats.total_undecryptable == 0

        # Under the NEW salt, the ORM transparently decrypts the rotated values.
        monkeypatch.setattr("app.config.settings", _make_settings(encryption_salt="new-salt"))
        et_mod._fernet_instance = None
        u = db_session.get(User, uid)
        db_session.expire(u)
        assert u.refresh_token == "refresh-abc"
        assert u.access_token == "access-xyz"
        assert u.password_hash == "pw$hash"

        # Under the OLD salt, the rotated ciphertext no longer decrypts (-> None).
        monkeypatch.setattr("app.config.settings", _make_settings(encryption_salt="old-salt"))
        et_mod._fernet_instance = None
        db_session.expire(u)
        assert u.refresh_token is None

    def test_idempotent_second_run(self, db_session, monkeypatch):
        _insert_user_under_salt(db_session, monkeypatch, "old-salt", refresh_token="tok")
        rotate_salt(db_session, old_salt="old-salt", new_salt="new-salt", secret_key=SECRET)

        # Second run: every value is already on the NEW salt -> detected, skipped.
        stats = rotate_salt(db_session, old_salt="old-salt", new_salt="new-salt", secret_key=SECRET)
        assert stats.total_rotated == 0
        assert stats.already["refresh_token"] == 1
        assert stats.rows_updated == 0

    def test_dry_run_writes_nothing(self, db_session, monkeypatch):
        _insert_user_under_salt(db_session, monkeypatch, "old-salt", refresh_token="tok")
        stats = rotate_salt(db_session, old_salt="old-salt", new_salt="new-salt", secret_key=SECRET, dry_run=True)
        assert stats.rotated["refresh_token"] == 1
        assert stats.rows_updated == 1  # would-update count

        # The DB still holds OLD-salt ciphertext: decrypts under OLD, not NEW.
        raw = db_session.execute(text("SELECT refresh_token FROM users")).scalar_one()
        assert build_fernet(SECRET, "old-salt").decrypt(raw.encode()) == b"tok"
        with pytest.raises(InvalidToken):
            build_fernet(SECRET, "new-salt").decrypt(raw.encode())

    def test_undecryptable_row_left_intact(self, db_session, monkeypatch):
        # Encrypt under a salt that is neither OLD nor NEW.
        _insert_user_under_salt(db_session, monkeypatch, "mystery-salt", refresh_token="tok")
        stats = rotate_salt(db_session, old_salt="old-salt", new_salt="new-salt", secret_key=SECRET)
        assert stats.undecryptable["refresh_token"] == 1
        assert stats.total_rotated == 0
        assert stats.rows_updated == 0

        # Row is untouched: still decrypts under the original mystery salt.
        raw = db_session.execute(text("SELECT refresh_token FROM users")).scalar_one()
        assert build_fernet(SECRET, "mystery-salt").decrypt(raw.encode()) == b"tok"

    def test_equal_salts_refused(self, db_session):
        with pytest.raises(ValueError, match="nothing to rotate"):
            rotate_salt(db_session, old_salt="same", new_salt="same", secret_key=SECRET)

    def test_both_empty_salts_refused(self, db_session):
        with pytest.raises(ValueError, match="nothing to rotate"):
            rotate_salt(db_session, old_salt=None, new_salt="", secret_key=SECRET)
