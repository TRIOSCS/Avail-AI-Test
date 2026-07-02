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
from app.models import BuyPlan, Requisition


def _own(db, req, owner_id):
    req.created_by = owner_id
    db.commit()


def _mk_req(db, owner_id, name):
    """A non-scratch requisition owned by owner_id, surfaced by the list route."""
    r = Requisition(name=name, status="open", urgency="normal", customer_name="Cust", created_by=owner_id)
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


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
# Re-pointed from the retired requisitions2 list service to the canonical Sales Hub list
# route (GET /v2/partials/requisitions) and detail route (GET /v2/partials/requisitions/{id})
# in app/routers/htmx/requisitions.py, which now own the same ownership invariants.
def test_list_requisitions_scopes_trader_to_own(client, db_session, test_user, admin_user):
    """A TRADER's list is scoped to requisitions they own (mirrors SALES)."""
    mine = _mk_req(db_session, test_user.id, "MINE-REQ-TRADER")
    foreign = _mk_req(db_session, admin_user.id, "FOREIGN-REQ-TRADER")
    test_user.role = UserRole.TRADER  # client auths as test_user
    db_session.commit()

    resp = client.get("/v2/partials/requisitions")
    assert resp.status_code == 200
    assert mine.name in resp.text  # own requisition visible
    assert foreign.name not in resp.text  # restricted: foreign requisition hidden


def test_get_requisition_detail_scopes_trader_to_own(client, db_session, test_user, admin_user):
    """A TRADER's detail fetch is scoped to requisitions they own (mirrors SALES)."""
    req = _mk_req(db_session, admin_user.id, "DETAIL-REQ-TRADER")
    test_user.role = UserRole.TRADER
    db_session.commit()

    # Non-owner trader → gated (404, not found), never leaks the foreign detail.
    assert client.get(f"/v2/partials/requisitions/{req.id}").status_code == 404

    _own(db_session, req, test_user.id)  # now owned by the trader
    resp = client.get(f"/v2/partials/requisitions/{req.id}")
    assert resp.status_code == 200
    assert req.name in resp.text


def test_list_requisitions_buyer_sees_non_owned(client, db_session, test_user, admin_user):
    """A BUYER (unrestricted) sees requisitions they don't own."""
    assert test_user.role == UserRole.BUYER  # client auths as an unrestricted buyer
    foreign = _mk_req(db_session, admin_user.id, "FOREIGN-REQ-BUYER")

    resp = client.get("/v2/partials/requisitions")
    assert resp.status_code == 200
    assert foreign.name in resp.text  # unrestricted: sees non-owned requisition
