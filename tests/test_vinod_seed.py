"""Tests for _seed_admin_user_if_env_set() in startup.py.

Verifies idempotent creation of the admin user.
"""

from app.models.auth import User
from app.startup import _seed_admin_user_if_env_set


def test_seed_vinod_creates_user(db_session):
    """_seed_admin_user_if_env_set creates an admin user with the correct fields."""
    _seed_admin_user_if_env_set(db=db_session)

    user = db_session.query(User).filter_by(email="vinod@trioscs.com").first()
    assert user is not None
    assert user.name == "Vinod"
    assert user.role == "admin"
    assert user.password_hash is None


def test_seed_vinod_idempotent(db_session):
    """Calling _seed_admin_user_if_env_set twice creates only one user."""
    _seed_admin_user_if_env_set(db=db_session)
    _seed_admin_user_if_env_set(db=db_session)

    count = db_session.query(User).filter_by(email="vinod@trioscs.com").count()
    assert count == 1
