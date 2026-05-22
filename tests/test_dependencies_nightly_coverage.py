"""test_dependencies_nightly_coverage.py — Extra coverage for app/dependencies.py.

Covers: require_user (agent key, deactivated user), require_admin, require_settings_access,
require_buyer (non-buyer role), get_req_for_user (sales filter), get_quote_for_user.

Called by: pytest
Depends on: app/dependencies.py, conftest.py
"""

import os
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

os.environ["TESTING"] = "1"

from app.dependencies import (
    get_quote_for_user,
    get_req_for_user,
    get_user,
    require_admin,
    require_buyer,
    require_settings_access,
    require_user,
)
from app.models import Quote, Requisition, User


def _mock_request(session_data=None, headers=None):
    req = MagicMock()
    # Use a MagicMock for session so .clear() is trackable
    mock_session = MagicMock()
    mock_session.get = (session_data or {}).get
    req.session = mock_session
    req.headers = headers or {}
    req.method = "GET"
    req.url = MagicMock()
    req.url.path = "/test"
    return req


# ── get_user exception branch ─────────────────────────────────────────


class TestGetUserExceptionBranch:
    def test_clears_session_on_db_exception(self, db_session):
        """When db.get raises, session is cleared and None returned."""
        request = _mock_request({"user_id": 999})
        bad_db = MagicMock(spec=Session)
        bad_db.get.side_effect = Exception("DB connection lost")
        result = get_user(request, bad_db)
        assert result is None
        request.session.clear.assert_called_once()


# ── require_user branches ─────────────────────────────────────────────


class TestRequireUserBranches:
    def test_raises_401_when_no_user_no_agent_key(self, db_session):
        request = _mock_request({})
        with pytest.raises(HTTPException) as exc:
            require_user(request, db_session)
        assert exc.value.status_code == 401

    def test_agent_key_with_no_agent_user_raises_503(self, db_session):
        """Valid agent key but no agent user in DB → 503."""
        request = _mock_request({}, headers={"x-agent-key": "test-agent-key-secret"})
        with patch("app.config.settings") as mock_settings:
            mock_settings.agent_api_key = "test-agent-key-secret"
            with pytest.raises(HTTPException) as exc:
                require_user(request, db_session)
        assert exc.value.status_code == 503

    def test_agent_key_with_agent_user_in_db(self, db_session):
        """Valid agent key + agent user in DB → returns agent user."""
        agent_user = User(
            email="agent@availai.local",
            name="Agent",
            role="agent",
            azure_id="agent-azure-id",
        )
        db_session.add(agent_user)
        db_session.commit()

        request = _mock_request({}, headers={"x-agent-key": "test-agent-key-secret"})
        with patch("app.config.settings") as mock_settings:
            mock_settings.agent_api_key = "test-agent-key-secret"
            user = require_user(request, db_session)
        assert user.email == "agent@availai.local"

    def test_wrong_agent_key_raises_401(self, db_session):
        """Wrong agent key → 401, not authenticated."""
        request = _mock_request({}, headers={"x-agent-key": "wrong-key"})
        with patch("app.config.settings") as mock_settings:
            mock_settings.agent_api_key = "correct-key"
            with pytest.raises(HTTPException) as exc:
                require_user(request, db_session)
        assert exc.value.status_code == 401

    def test_deactivated_user_raises_403(self, db_session):
        """Deactivated user → 403."""
        user = User(
            email="inactive@trioscs.com",
            name="Inactive",
            role="buyer",
            azure_id="inactive-azure-id",
            is_active=False,
        )
        db_session.add(user)
        db_session.commit()

        request = _mock_request({"user_id": user.id})
        with pytest.raises(HTTPException) as exc:
            require_user(request, db_session)
        assert exc.value.status_code == 403
        assert "deactivated" in str(exc.value.detail).lower()


# ── require_admin branches ────────────────────────────────────────────


class TestRequireAdminBranches:
    def test_agent_email_blocked(self, db_session):
        """agent@availai.local cannot access admin endpoints."""
        agent_user = User(
            email="agent@availai.local",
            name="Agent",
            role="admin",
            azure_id="agent-azure-id-2",
        )
        db_session.add(agent_user)
        db_session.commit()

        request = _mock_request({"user_id": agent_user.id})
        with pytest.raises(HTTPException) as exc:
            require_admin(request, db_session)
        assert exc.value.status_code == 403
        assert "Agent keys" in str(exc.value.detail)

    def test_non_admin_role_raises_403(self, db_session, test_user):
        """Buyer role cannot access admin endpoints."""
        request = _mock_request({"user_id": test_user.id})
        with pytest.raises(HTTPException) as exc:
            require_admin(request, db_session)
        assert exc.value.status_code == 403
        assert "Admin access required" in str(exc.value.detail)

    def test_admin_role_passes(self, db_session, admin_user):
        """Admin role gets through."""
        request = _mock_request({"user_id": admin_user.id})
        user = require_admin(request, db_session)
        assert user.id == admin_user.id


# ── require_settings_access branches ─────────────────────────────────


class TestRequireSettingsAccessBranches:
    def test_agent_email_blocked(self, db_session):
        """agent@availai.local cannot access settings."""
        agent_user = User(
            email="agent@availai.local",
            name="Agent",
            role="admin",
            azure_id="agent-azure-id-3",
        )
        db_session.add(agent_user)
        db_session.commit()

        request = _mock_request({"user_id": agent_user.id})
        with pytest.raises(HTTPException) as exc:
            require_settings_access(request, db_session)
        assert exc.value.status_code == 403
        assert "Agent keys" in str(exc.value.detail)

    def test_non_admin_raises_403(self, db_session, test_user):
        request = _mock_request({"user_id": test_user.id})
        with pytest.raises(HTTPException) as exc:
            require_settings_access(request, db_session)
        assert exc.value.status_code == 403
        assert "Settings access required" in str(exc.value.detail)

    def test_admin_passes(self, db_session, admin_user):
        request = _mock_request({"user_id": admin_user.id})
        user = require_settings_access(request, db_session)
        assert user.id == admin_user.id


# ── require_buyer branches ────────────────────────────────────────────


class TestRequireBuyerBranches:
    def test_non_buyer_role_raises_403(self, db_session):
        """User with viewer role cannot access buyer endpoints."""
        user = User(
            email="viewer@trioscs.com",
            name="Viewer",
            role="viewer",
            azure_id="viewer-azure-id",
        )
        db_session.add(user)
        db_session.commit()

        request = _mock_request({"user_id": user.id})
        with pytest.raises(HTTPException) as exc:
            require_buyer(request, db_session)
        assert exc.value.status_code == 403
        assert "Buyer role required" in str(exc.value.detail)

    def test_buyer_role_passes(self, db_session, test_user):
        request = _mock_request({"user_id": test_user.id})
        user = require_buyer(request, db_session)
        assert user.id == test_user.id

    def test_sales_role_passes(self, db_session, sales_user):
        request = _mock_request({"user_id": sales_user.id})
        user = require_buyer(request, db_session)
        assert user.id == sales_user.id

    def test_admin_role_passes(self, db_session, admin_user):
        request = _mock_request({"user_id": admin_user.id})
        user = require_buyer(request, db_session)
        assert user.id == admin_user.id


# ── get_req_for_user sales filter ────────────────────────────────────


class TestGetReqForUserSalesFilter:
    def test_sales_user_cannot_see_others_requisitions(self, db_session, sales_user, test_user):
        """Sales user cannot see requisitions created by others."""
        req = Requisition(
            name="REQ-OTHER",
            customer_name="Other",
            status="active",
            created_by=test_user.id,
        )
        db_session.add(req)
        db_session.commit()

        with pytest.raises(HTTPException) as exc:
            get_req_for_user(db_session, sales_user, req.id)
        assert exc.value.status_code == 404

    def test_sales_user_can_see_own_requisition(self, db_session, sales_user):
        """Sales user can see their own requisitions."""
        req = Requisition(
            name="REQ-OWN",
            customer_name="Mine",
            status="active",
            created_by=sales_user.id,
        )
        db_session.add(req)
        db_session.commit()

        result = get_req_for_user(db_session, sales_user, req.id)
        assert result.id == req.id

    def test_custom_load_options(self, db_session, test_user, test_requisition):
        """Custom load options are accepted."""
        from sqlalchemy.orm import joinedload

        result = get_req_for_user(
            db_session, test_user, test_requisition.id, options=[joinedload(Requisition.requirements)]
        )
        assert result.id == test_requisition.id


# ── get_quote_for_user ────────────────────────────────────────────────


class TestGetQuoteForUser:
    def _make_quote(self, db_session, user, requisition, quote_number="Q-2024-0001"):

        quote = Quote(
            requisition_id=requisition.id,
            quote_number=quote_number,
            created_by_id=user.id,
        )
        db_session.add(quote)
        db_session.commit()
        db_session.refresh(quote)
        return quote

    def test_buyer_can_get_any_quote(self, db_session, test_user, test_requisition):
        quote = self._make_quote(db_session, test_user, test_requisition)
        result = get_quote_for_user(db_session, test_user, quote.id)
        assert result.id == quote.id

    def test_nonexistent_quote_raises_404(self, db_session, test_user):
        with pytest.raises(HTTPException) as exc:
            get_quote_for_user(db_session, test_user, 99999)
        assert exc.value.status_code == 404

    def test_sales_user_can_get_own_quote(self, db_session, sales_user, test_requisition):
        """Sales user can get quotes for their own requisitions."""
        test_requisition.created_by = sales_user.id
        db_session.commit()

        quote = self._make_quote(db_session, sales_user, test_requisition, "Q-2024-S01")
        result = get_quote_for_user(db_session, sales_user, quote.id)
        assert result.id == quote.id

    def test_sales_user_cannot_get_others_quote(self, db_session, sales_user, test_user, test_requisition):
        """Sales user cannot get quotes for other users' requisitions."""
        test_requisition.created_by = test_user.id
        db_session.commit()

        quote = self._make_quote(db_session, test_user, test_requisition, "Q-2024-O01")
        with pytest.raises(HTTPException) as exc:
            get_quote_for_user(db_session, sales_user, quote.id)
        assert exc.value.status_code == 404

    def test_custom_load_options(self, db_session, test_user, test_requisition):
        """Custom load options are accepted."""
        quote = self._make_quote(db_session, test_user, test_requisition, "Q-2024-C01")
        result = get_quote_for_user(db_session, test_user, quote.id, options=[])
        assert result.id == quote.id
