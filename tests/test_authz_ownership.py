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
    get_buyplan_for_user,
    get_req_for_user,
    require_requisition_access,
)
from app.models import BuyPlan
from app.schemas.requisition_list import ReqListFilters
from app.services.requisition_list_service import (
    get_requisition_detail,
    list_requisitions,
)


def _own(db, req, owner_id):
    req.created_by = owner_id
    db.commit()


def _make_buyplan(db, req, quote) -> BuyPlan:
    """Create a BuyPlan whose ownership derives through the parent requisition."""
    plan = BuyPlan(quote_id=quote.id, requisition_id=req.id)
    db.add(plan)
    db.commit()
    db.refresh(plan)
    return plan


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


# ── get_buyplan_for_user (ownership via parent requisition) ───────────────────
def test_get_buyplan_for_user_allows_owner(db_session, test_requisition, test_quote, sales_user):
    _own(db_session, test_requisition, sales_user.id)
    plan = _make_buyplan(db_session, test_requisition, test_quote)
    got = get_buyplan_for_user(db_session, sales_user, plan.id)
    assert got.id == plan.id


def test_get_buyplan_for_user_allows_buyer_non_owner(db_session, test_requisition, test_quote, test_user, admin_user):
    _own(db_session, test_requisition, admin_user.id)
    plan = _make_buyplan(db_session, test_requisition, test_quote)
    got = get_buyplan_for_user(db_session, test_user, plan.id)  # test_user is a buyer
    assert got.id == plan.id


def test_get_buyplan_for_user_allows_manager_non_owner(
    db_session, test_requisition, test_quote, manager_user, admin_user
):
    _own(db_session, test_requisition, admin_user.id)
    plan = _make_buyplan(db_session, test_requisition, test_quote)
    got = get_buyplan_for_user(db_session, manager_user, plan.id)
    assert got.id == plan.id


def test_get_buyplan_for_user_allows_admin_non_owner(db_session, test_requisition, test_quote, admin_user, sales_user):
    _own(db_session, test_requisition, sales_user.id)  # owned by someone else
    plan = _make_buyplan(db_session, test_requisition, test_quote)
    got = get_buyplan_for_user(db_session, admin_user, plan.id)
    assert got.id == plan.id


def test_get_buyplan_for_user_blocks_sales_non_owner(db_session, test_requisition, test_quote, sales_user, admin_user):
    _own(db_session, test_requisition, admin_user.id)
    plan = _make_buyplan(db_session, test_requisition, test_quote)
    with pytest.raises(HTTPException) as ei:
        get_buyplan_for_user(db_session, sales_user, plan.id)
    assert ei.value.status_code == 404


def test_get_buyplan_for_user_blocks_trader_non_owner(
    db_session, test_requisition, test_quote, trader_user, admin_user
):
    _own(db_session, test_requisition, admin_user.id)
    plan = _make_buyplan(db_session, test_requisition, test_quote)
    with pytest.raises(HTTPException) as ei:
        get_buyplan_for_user(db_session, trader_user, plan.id)
    assert ei.value.status_code == 404


def test_get_buyplan_for_user_missing_raises_404(db_session, sales_user):
    with pytest.raises(HTTPException) as ei:
        get_buyplan_for_user(db_session, sales_user, 999999)
    assert ei.value.status_code == 404


# ── Regression: list/detail scoping now restricts TRADER too (was SALES-only) ──
def test_list_requisitions_scopes_trader_to_own(db_session, test_requisition, trader_user, admin_user):
    """A TRADER's list is scoped to requisitions they own (mirrors SALES)."""
    _own(db_session, test_requisition, admin_user.id)  # not the trader's
    result = list_requisitions(db_session, ReqListFilters(), trader_user.id, trader_user.role)
    ids = {row["id"] for row in result["requisitions"]}
    assert test_requisition.id not in ids

    _own(db_session, test_requisition, trader_user.id)  # now owned by the trader
    result = list_requisitions(db_session, ReqListFilters(), trader_user.id, trader_user.role)
    ids = {row["id"] for row in result["requisitions"]}
    assert test_requisition.id in ids


def test_get_requisition_detail_scopes_trader_to_own(db_session, test_requisition, trader_user, admin_user):
    """A TRADER's detail fetch is scoped to requisitions they own (mirrors SALES)."""
    _own(db_session, test_requisition, admin_user.id)
    assert get_requisition_detail(db_session, test_requisition.id, trader_user.id, trader_user.role) is None

    _own(db_session, test_requisition, trader_user.id)
    detail = get_requisition_detail(db_session, test_requisition.id, trader_user.id, trader_user.role)
    assert detail is not None and detail["req"]["id"] == test_requisition.id


def test_list_requisitions_buyer_sees_non_owned(db_session, test_requisition, test_user, admin_user):
    """A BUYER (unrestricted) sees requisitions they don't own."""
    _own(db_session, test_requisition, admin_user.id)
    result = list_requisitions(db_session, ReqListFilters(), test_user.id, test_user.role)
    ids = {row["id"] for row in result["requisitions"]}
    assert test_requisition.id in ids
