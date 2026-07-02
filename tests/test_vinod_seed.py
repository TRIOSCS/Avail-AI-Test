"""Tests for _seed_admin_user_if_env_set() in startup.py.

Verifies the seed is env-driven (no hard-coded default admin) and idempotent.
"""

from unittest.mock import patch

from app.models.auth import User
from app.startup import _seed_admin_user_if_env_set


def test_env_unset_seeds_nothing(db_session):
    """Without SEED_ADMIN_EMAIL, nothing is seeded (CFG-8).

    The old hard-coded default silently created an admin on every fresh install.
    """
    with patch.dict("os.environ", {}, clear=False):
        import os

        os.environ.pop("SEED_ADMIN_EMAIL", None)
        _seed_admin_user_if_env_set(db=db_session)

    assert db_session.query(User).filter_by(role="admin").count() == 0


def test_seed_creates_user_from_env(db_session):
    """_seed_admin_user_if_env_set creates an admin user from the env vars."""
    with patch.dict("os.environ", {"SEED_ADMIN_EMAIL": "ops@example.com", "SEED_ADMIN_NAME": "Ops"}):
        _seed_admin_user_if_env_set(db=db_session)

    user = db_session.query(User).filter_by(email="ops@example.com").first()
    assert user is not None
    assert user.name == "Ops"
    assert user.role == "admin"
    assert user.password_hash is None


def test_seed_name_defaults_to_email_local_part(db_session):
    with patch.dict("os.environ", {"SEED_ADMIN_EMAIL": "vinod@trioscs.com"}):
        import os

        os.environ.pop("SEED_ADMIN_NAME", None)
        _seed_admin_user_if_env_set(db=db_session)

    user = db_session.query(User).filter_by(email="vinod@trioscs.com").first()
    assert user is not None
    assert user.name == "vinod"


def test_seed_idempotent(db_session):
    """Calling _seed_admin_user_if_env_set twice creates only one user."""
    with patch.dict("os.environ", {"SEED_ADMIN_EMAIL": "ops@example.com", "SEED_ADMIN_NAME": "Ops"}):
        _seed_admin_user_if_env_set(db=db_session)
        _seed_admin_user_if_env_set(db=db_session)

    assert db_session.query(User).filter_by(email="ops@example.com").count() == 1
