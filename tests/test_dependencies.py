"""test_dependencies.py — Tests for shared FastAPI dependencies.

Tests auth functions, role-based access, query helpers.
Uses in-memory SQLite via conftest fixtures.

Called by: pytest
Depends on: app/dependencies.py, conftest.py
"""

from unittest.mock import MagicMock

from app.dependencies import (
    get_req_for_user,
    get_user,
    is_admin,
    user_reqs_query,
)

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


class TestGetReqForUser:
    def test_buyer_can_get_any(self, db_session, test_user, test_requisition):
        req = get_req_for_user(db_session, test_user, test_requisition.id)
        assert req is not None
        assert req.id == test_requisition.id

    def test_nonexistent_req(self, db_session, test_user):
        req = get_req_for_user(db_session, test_user, 99999)
        assert req is None
