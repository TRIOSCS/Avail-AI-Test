"""Central requisition-ownership authorization helper.

Policy (role-scoped, approved 2026-06-23): SALES and TRADER users may only act on
requisitions they own (created_by) — or, for unscoped/scratch resources, ones they
created themselves (owner_id fallback). BUYER / MANAGER / ADMIN are unrestricted. The
single source of truth is dependencies.RESTRICTED_ROLES.
"""

import pytest
from fastapi import HTTPException

from app.constants import UserRole
from app.dependencies import (
    RESTRICTED_ROLES,
    get_req_for_user,
    require_requisition_access,
)


def _own(db, req, owner_id):
    req.created_by = owner_id
    db.commit()


def test_restricted_roles_are_sales_and_trader():
    assert RESTRICTED_ROLES == frozenset({UserRole.SALES, UserRole.TRADER})


# ── require_requisition_access ───────────────────────────────────────────────
def test_buyer_unrestricted_even_when_not_owner(db_session, test_requisition, test_user, admin_user):
    _own(db_session, test_requisition, admin_user.id)  # owned by someone else
    # test_user is a buyer → no exception
    require_requisition_access(db_session, test_requisition.id, test_user)


def test_sales_non_owner_blocked(db_session, test_requisition, sales_user, admin_user):
    _own(db_session, test_requisition, admin_user.id)
    with pytest.raises(HTTPException) as ei:
        require_requisition_access(db_session, test_requisition.id, sales_user)
    assert ei.value.status_code == 404


def test_sales_owner_allowed(db_session, test_requisition, sales_user):
    _own(db_session, test_requisition, sales_user.id)
    require_requisition_access(db_session, test_requisition.id, sales_user)


def test_trader_non_owner_blocked(db_session, test_requisition, trader_user, admin_user):
    _own(db_session, test_requisition, admin_user.id)
    with pytest.raises(HTTPException) as ei:
        require_requisition_access(db_session, test_requisition.id, trader_user)
    assert ei.value.status_code == 404


def test_trader_owner_allowed(db_session, test_requisition, trader_user):
    _own(db_session, test_requisition, trader_user.id)
    require_requisition_access(db_session, test_requisition.id, trader_user)


def test_owner_id_fallback_for_unscoped_resource(db_session, sales_user, admin_user):
    # No requisition (e.g. scratch resource): restricted role allowed only if they own it.
    require_requisition_access(db_session, None, sales_user, owner_id=sales_user.id)
    with pytest.raises(HTTPException) as ei:
        require_requisition_access(db_session, None, sales_user, owner_id=admin_user.id)
    assert ei.value.status_code == 404


def test_missing_requisition_blocked_for_restricted(db_session, sales_user):
    with pytest.raises(HTTPException) as ei:
        require_requisition_access(db_session, 999999, sales_user)
    assert ei.value.status_code == 404


# ── get_req_for_user now restricts TRADER too (was SALES-only) ────────────────
def test_get_req_for_user_blocks_trader_non_owner(db_session, test_requisition, trader_user, admin_user):
    _own(db_session, test_requisition, admin_user.id)
    with pytest.raises(HTTPException) as ei:
        get_req_for_user(db_session, trader_user, test_requisition.id)
    assert ei.value.status_code == 404


def test_get_req_for_user_allows_buyer_non_owner(db_session, test_requisition, test_user, admin_user):
    _own(db_session, test_requisition, admin_user.id)
    req = get_req_for_user(db_session, test_user, test_requisition.id)
    assert req.id == test_requisition.id
