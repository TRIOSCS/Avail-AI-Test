"""Tests for Teams bot service — _resolve_user and shared utilities.

Called by: pytest
Depends on: app/services/teams_bot_service.py, conftest db_session
"""

from app.models.auth import User
from app.services.teams_bot_service import _resolve_user


def test_resolve_user_no_match_returns_none(db_session):
    """_resolve_user must NOT fall back to random active users."""
    u = User(
        email="other@test.com",
        name="Other",
        is_active=True,
        azure_id="different-aad-id",
    )
    db_session.add(u)
    db_session.commit()

    result = _resolve_user("nonexistent-aad-id", db_session)
    assert result is None, "Must return None, not fall back to arbitrary user"


def test_resolve_user_returns_user_when_match(db_session):
    """_resolve_user returns the user when AAD ID matches."""
    u = User(
        email="match@test.com",
        name="Match",
        is_active=True,
        azure_id="correct-aad-id",
    )
    db_session.add(u)
    db_session.commit()

    result = _resolve_user("correct-aad-id", db_session)
    assert result is not None
    assert result.email == "match@test.com"


def test_resolve_user_empty_id_returns_none(db_session):
    """_resolve_user returns None for empty AAD ID."""
    assert _resolve_user("", db_session) is None
