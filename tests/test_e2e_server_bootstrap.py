"""Tests for scripts/e2e_server.py — the Playwright e2e launcher's bootstrap.

Verifies that ``bootstrap()`` creates the schema on the process-global engine
and seeds the DEFAULT_USER_* admin through the production helper
(``app.startup._create_default_user_if_env_set``), with a password hash the
real login path (``app.routers.auth._verify_password``) accepts, and that a
second call is a no-op (idempotent).

Called by: pytest autodiscovery
Depends on: scripts/e2e_server.py, app.database (global SessionLocal),
            app.models.auth.User, app.routers.auth._verify_password
"""

import pytest

from app.database import SessionLocal
from app.models.auth import User
from app.routers.auth import _verify_password
from scripts.e2e_server import bootstrap

_EMAIL = "e2e-admin@availai.test"
_PASSWORD = "e2e-local-only-pw"


@pytest.fixture
def _seed_env(monkeypatch):
    """Point bootstrap() at throwaway credentials; delete the row afterwards.

    Teardown is mandatory (plan F9): bootstrap() commits through the GLOBAL
    ``app.database.SessionLocal`` — under pytest that engine is this worker's
    shared in-memory sqlite, so a leaked ``e2e-admin@availai.test`` row would
    bleed into every later test in the same xdist worker.
    """
    monkeypatch.setenv("DEFAULT_USER_EMAIL", _EMAIL)
    monkeypatch.setenv("DEFAULT_USER_PASSWORD", _PASSWORD)
    monkeypatch.setenv("DEFAULT_USER_ROLE", "admin")
    yield
    db = SessionLocal()
    try:
        db.query(User).filter_by(email=_EMAIL).delete()
        db.commit()
    finally:
        db.close()


def test_bootstrap_seeds_admin_with_working_password(_seed_env):
    bootstrap()
    db = SessionLocal()
    try:
        user = db.query(User).filter_by(email=_EMAIL).first()
        assert user is not None, "bootstrap() did not seed the DEFAULT_USER_* row"
        assert user.role == "admin"
        # The real login path must accept the seeded password (PBKDF2 round-trip
        # through EncryptedText in the same process).
        assert _verify_password(user.password_hash, _PASSWORD)
        # ORM seed leaves column defaults intact (plan §1/F11) —
        # auth.spec.ts asserts connected === false against this.
        assert user.m365_connected is False
    finally:
        db.close()


def test_bootstrap_is_idempotent(_seed_env):
    bootstrap()
    bootstrap()  # second call must be a no-op, not a duplicate row or an error
    db = SessionLocal()
    try:
        assert db.query(User).filter_by(email=_EMAIL).count() == 1
    finally:
        db.close()
