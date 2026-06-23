"""test_trading_capabilities.py — Role-derived capability checks for the Trading module.

Covers ``can_post`` (sales + traders, plus admin/manager) and ``can_offer``
(buyers + traders, plus admin/manager), derived from ``User.role`` — the spec
models the two powers as capabilities, not scattered ``role == 'trader'`` checks.

Called by: pytest
Depends on: app.services.excess_service (can_post / can_offer), tests.conftest
"""

import pytest
from sqlalchemy.orm import Session

from app.models import User
from app.services.excess_service import can_offer, can_post
from tests.conftest import engine

_ = engine  # Ensure test DB tables are created


def _make_user(db: Session, role: str) -> User:
    user = User(email=f"{role}-cap@trioscs.com", name=f"{role.title()} User", role=role, azure_id=f"cap-{role}")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


# ── can_post: sell-side intake (sales + trader, plus admin/manager) ──


@pytest.mark.parametrize("role", ["sales", "trader", "admin", "manager"])
def test_can_post_allowed_roles(db_session: Session, role: str):
    user = _make_user(db_session, role)
    assert can_post(user) is True


@pytest.mark.parametrize("role", ["buyer", "agent"])
def test_can_post_denied_roles(db_session: Session, role: str):
    user = _make_user(db_session, role)
    assert can_post(user) is False


# ── can_offer: buy-side offers (buyer + trader, plus admin/manager) ──


@pytest.mark.parametrize("role", ["buyer", "trader", "admin", "manager"])
def test_can_offer_allowed_roles(db_session: Session, role: str):
    user = _make_user(db_session, role)
    assert can_offer(user) is True


@pytest.mark.parametrize("role", ["sales", "agent"])
def test_can_offer_denied_roles(db_session: Session, role: str):
    user = _make_user(db_session, role)
    assert can_offer(user) is False


def test_trader_holds_both_capabilities(db_session: Session):
    """Traders are on both sides — the primary users of this module."""
    user = _make_user(db_session, "trader")
    assert can_post(user) is True
    assert can_offer(user) is True


def test_capabilities_handle_none_user():
    """Capability checks are total — None is denied, never raises."""
    assert can_post(None) is False
    assert can_offer(None) is False
