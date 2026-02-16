"""
test_dependencies.py — Tests for shared FastAPI dependencies.

Tests auth functions, role-based access, query helpers.
Uses in-memory SQLite via conftest fixtures.

Called by: pytest
Depends on: app/dependencies.py, conftest.py
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock

from app.dependencies import (
    get_user,
    require_user,
    require_buyer,
    require_admin,
    require_settings_access,
    is_admin,
    user_reqs_query,
    get_req_for_user,
)
from app.models import Requisition, User
import pytest
from fastapi import HTTPException


# ── Helpers ─────────────────────────────────────────────────────────


def _mock_request(session_data=None):
    req = MagicMock()
    req.session = session_data or {}
    return req


# ── get_user ────────────────────────────────────────────────────────


class TestGetUser:
    def test_returns_user_when_session_has_id(self, db_session, test_user):
        request = _mock_request({"user_id": test_user.id})
        user = get_user(request, db_session)
        assert user is not None
        assert user.id == test_user.id

    def test_returns_none_when_no_session(self, db_session):
        request = _mock_request({})
        user = get_user(request, db_session)
        assert user is None

    def test_returns_none_when_user_not_found(self, db_session):
        request = _mock_request({"user_id": 99999})
        user = get_user(request, db_session)
        assert user is None


# ── is_admin ────────────────────────────────────────────────────────


class TestIsAdmin:
    def test_admin_role(self, admin_user):
        assert is_admin(admin_user) is True

    def test_buyer_role(self, test_user):
        assert is_admin(test_user) is False


# ── Role-based queries ──────────────────────────────────────────────


class TestUserReqsQuery:
    def test_buyer_sees_all(self, db_session, test_user, test_requisition):
        query = user_reqs_query(db_session, test_user)
        results = query.all()
        assert len(results) >= 1

    def test_sales_sees_own_only(self, db_session, sales_user, test_user, test_requisition):
        """Sales user should only see reqs they created."""
        query = user_reqs_query(db_session, sales_user)
        results = query.all()
        # sales_user didn't create test_requisition, so should see 0
        assert len(results) == 0

    def test_dev_assistant_sees_none(self, db_session, test_requisition):
        dev = User(
            email="dev@trioscs.com", name="Dev", role="dev_assistant",
            azure_id="az-dev", created_at=datetime.now(timezone.utc),
        )
        db_session.add(dev)
        db_session.commit()

        query = user_reqs_query(db_session, dev)
        results = query.all()
        assert len(results) == 0


class TestGetReqForUser:
    def test_buyer_can_get_any(self, db_session, test_user, test_requisition):
        req = get_req_for_user(db_session, test_user, test_requisition.id)
        assert req is not None
        assert req.id == test_requisition.id

    def test_dev_assistant_gets_none(self, db_session, test_requisition):
        dev = User(
            email="dev2@trioscs.com", name="Dev", role="dev_assistant",
            azure_id="az-dev2", created_at=datetime.now(timezone.utc),
        )
        db_session.add(dev)
        db_session.commit()

        req = get_req_for_user(db_session, dev, test_requisition.id)
        assert req is None

    def test_nonexistent_req(self, db_session, test_user):
        req = get_req_for_user(db_session, test_user, 99999)
        assert req is None
