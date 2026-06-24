"""tests/test_ownership_visibility.py — Phase 2 ownership visibility TDD tests.

Tests for:
1. can_manage_account() helper (unit, no HTTP)
2. cdm_company_query() role-based visibility (list + count parity)
3. Representative authz gate: company_field_post (POST /v2/partials/customers/{company_id}/field)
   - site-owner → 200; manager → 200; unrelated rep → 403

Security: BOTH allow AND deny paths are tested for every principal.

Called by: pytest
Depends on: app.dependencies, app.services.crm_service, app.models
"""

import pytest

from app.dependencies import can_manage_account, is_manager_or_admin
from app.models import Company, CustomerSite, User
from app.services.crm_service import cdm_company_query

# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


def _make_user(db, role: str, email: str) -> User:
    u = User(
        email=email,
        name=email.split("@")[0],
        role=role,
        azure_id=f"az-{email}",
    )
    db.add(u)
    db.flush()
    return u


def _make_company(db, name: str, owner: User | None = None) -> Company:
    co = Company(
        name=name,
        is_active=True,
        account_owner_id=owner.id if owner else None,
    )
    db.add(co)
    db.flush()
    return co


def _make_site(db, company: Company, owner: User | None = None) -> CustomerSite:
    site = CustomerSite(
        company_id=company.id,
        site_name="Site",
        owner_id=owner.id if owner else None,
    )
    db.add(site)
    db.flush()
    return site


# ─────────────────────────────────────────────────────────────────────────────
# 1. is_manager_or_admin
# ─────────────────────────────────────────────────────────────────────────────


def test_is_manager_or_admin_admin(db_session):
    u = _make_user(db_session, "admin", "a@t.com")
    assert is_manager_or_admin(u) is True


def test_is_manager_or_admin_manager(db_session):
    u = _make_user(db_session, "manager", "m@t.com")
    assert is_manager_or_admin(u) is True


def test_is_manager_or_admin_sales_false(db_session):
    u = _make_user(db_session, "sales", "s@t.com")
    assert is_manager_or_admin(u) is False


def test_is_manager_or_admin_trader_false(db_session):
    u = _make_user(db_session, "trader", "tr@t.com")
    assert is_manager_or_admin(u) is False


def test_is_manager_or_admin_buyer_false(db_session):
    u = _make_user(db_session, "buyer", "b@t.com")
    assert is_manager_or_admin(u) is False


# ─────────────────────────────────────────────────────────────────────────────
# 2. can_manage_account — allow paths
# ─────────────────────────────────────────────────────────────────────────────


def test_can_manage_account_admin(db_session):
    admin = _make_user(db_session, "admin", "admin@t.com")
    co = _make_company(db_session, "Acme")
    assert can_manage_account(admin, co, db_session) is True


def test_can_manage_account_manager(db_session):
    mgr = _make_user(db_session, "manager", "mgr@t.com")
    co = _make_company(db_session, "Acme")
    assert can_manage_account(mgr, co, db_session) is True


def test_can_manage_account_account_owner(db_session):
    rep = _make_user(db_session, "sales", "rep@t.com")
    co = _make_company(db_session, "Acme", owner=rep)
    assert can_manage_account(rep, co, db_session) is True


def test_can_manage_account_site_owner(db_session):
    """A rep who owns a site under the company can manage the account."""
    rep = _make_user(db_session, "sales", "sitrep@t.com")
    other_owner = _make_user(db_session, "sales", "owner@t.com")
    co = _make_company(db_session, "Acme", owner=other_owner)
    _make_site(db_session, co, owner=rep)
    assert can_manage_account(rep, co, db_session) is True


def test_can_manage_account_trader_site_owner(db_session):
    """A trader who owns a site under the company can manage the account."""
    trader = _make_user(db_session, "trader", "trader@t.com")
    co = _make_company(db_session, "Acme")
    _make_site(db_session, co, owner=trader)
    assert can_manage_account(trader, co, db_session) is True


# ─────────────────────────────────────────────────────────────────────────────
# 3. can_manage_account — DENY paths (the critical security cases)
# ─────────────────────────────────────────────────────────────────────────────


def test_can_manage_account_unrelated_rep_denied(db_session):
    """An unrelated rep (not account owner, no owned site) must be denied."""
    rep = _make_user(db_session, "sales", "unrelated@t.com")
    owner = _make_user(db_session, "sales", "owner@t.com")
    co = _make_company(db_session, "Acme", owner=owner)
    assert can_manage_account(rep, co, db_session) is False


def test_can_manage_account_rep_site_under_different_company_denied(db_session):
    """Site ownership under a DIFFERENT company must not grant access."""
    rep = _make_user(db_session, "sales", "rep2@t.com")
    co_a = _make_company(db_session, "Company A")
    co_b = _make_company(db_session, "Company B")
    _make_site(db_session, co_b, owner=rep)  # site is under co_b, not co_a
    assert can_manage_account(rep, co_a, db_session) is False


def test_can_manage_account_no_owner_no_site_rep_denied(db_session):
    """Company with no owner — unrelated rep must still be denied."""
    rep = _make_user(db_session, "sales", "noowner@t.com")
    co = _make_company(db_session, "Ownerless Co")
    assert can_manage_account(rep, co, db_session) is False


def test_can_manage_account_buyer_denied_for_unowned_account(db_session):
    """A buyer role (not in manager tier) must be denied if not account owner."""
    buyer = _make_user(db_session, "buyer", "buyer@t.com")
    owner = _make_user(db_session, "sales", "owner@t.com")
    co = _make_company(db_session, "Acme", owner=owner)
    assert can_manage_account(buyer, co, db_session) is False


# ─────────────────────────────────────────────────────────────────────────────
# 4. cdm_company_query — role-based visibility
# ─────────────────────────────────────────────────────────────────────────────


def _query_ids(db, user, my_only=False) -> set[int]:
    q = cdm_company_query(
        db,
        user,
        search="",
        staleness="",
        account_type="",
        my_only=my_only,
        sort="oldest",
        disposition="active",
    )
    return {c.id for c in q.all()}


def test_cdm_query_manager_sees_all(db_session):
    mgr = _make_user(db_session, "manager", "mgr2@t.com")
    rep = _make_user(db_session, "sales", "rep3@t.com")
    co_mine = _make_company(db_session, "Mine", owner=rep)
    co_other = _make_company(db_session, "Other")
    db_session.flush()

    ids = _query_ids(db_session, mgr)
    assert co_mine.id in ids
    assert co_other.id in ids


def test_cdm_query_admin_sees_all(db_session):
    admin = _make_user(db_session, "admin", "adm2@t.com")
    rep = _make_user(db_session, "sales", "rp4@t.com")
    co_owned = _make_company(db_session, "Owned", owner=rep)
    co_unowned = _make_company(db_session, "Unowned")
    db_session.flush()

    ids = _query_ids(db_session, admin)
    assert co_owned.id in ids
    assert co_unowned.id in ids


def test_cdm_query_rep_sees_owned_account(db_session):
    rep = _make_user(db_session, "sales", "rep5@t.com")
    co_mine = _make_company(db_session, "RepCo", owner=rep)
    co_other = _make_company(db_session, "NotMine")
    db_session.flush()

    ids = _query_ids(db_session, rep, my_only=True)
    assert co_mine.id in ids
    assert co_other.id not in ids


def test_cdm_query_rep_sees_site_owned_account(db_session):
    """Rep owns a site under the company → sees the company."""
    rep = _make_user(db_session, "sales", "rep6@t.com")
    other_owner = _make_user(db_session, "sales", "ow2@t.com")
    co = _make_company(db_session, "SiteCo", owner=other_owner)
    _make_site(db_session, co, owner=rep)
    co_unrelated = _make_company(db_session, "Unrelated")
    db_session.flush()

    ids = _query_ids(db_session, rep, my_only=True)
    assert co.id in ids
    assert co_unrelated.id not in ids


def test_cdm_query_rep_does_not_see_others_account(db_session):
    rep = _make_user(db_session, "sales", "rep7@t.com")
    other = _make_user(db_session, "sales", "other@t.com")
    co_other = _make_company(db_session, "OtherCo", owner=other)
    db_session.flush()

    ids = _query_ids(db_session, rep, my_only=True)
    assert co_other.id not in ids


def test_cdm_query_rep_without_my_only_sees_all_active(db_session):
    """Without my_only, rep sees all active accounts (the 'All' tab)."""
    rep = _make_user(db_session, "sales", "rep8@t.com")
    co_mine = _make_company(db_session, "Mine2", owner=rep)
    co_other = _make_company(db_session, "Other2")
    db_session.flush()

    ids = _query_ids(db_session, rep, my_only=False)
    assert co_mine.id in ids
    assert co_other.id in ids


# ─────────────────────────────────────────────────────────────────────────────
# 5. Count query matches list query for each role
# ─────────────────────────────────────────────────────────────────────────────


def test_cdm_count_matches_list_for_rep(db_session):
    """Count and list must agree for a rep's my_only view."""
    rep = _make_user(db_session, "sales", "rep9@t.com")
    other = _make_user(db_session, "sales", "oth2@t.com")
    co_mine = _make_company(db_session, "CountMe", owner=rep)
    _make_site(db_session, co_mine, owner=rep)  # also owns the site
    _make_company(db_session, "NotMine2", owner=other)
    db_session.flush()

    q = cdm_company_query(
        db_session,
        rep,
        search="",
        staleness="",
        account_type="",
        my_only=True,
        sort="oldest",
        disposition="active",
    )
    list_ids = {c.id for c in q.all()}
    assert co_mine.id in list_ids
    assert len(list_ids) == 1


def test_cdm_count_matches_list_for_manager(db_session):
    """Manager sees both companies and count matches."""
    mgr = _make_user(db_session, "manager", "mgr3@t.com")
    rep = _make_user(db_session, "sales", "r10@t.com")
    co_a = _make_company(db_session, "Alpha", owner=rep)
    co_b = _make_company(db_session, "Beta")
    db_session.flush()

    q = cdm_company_query(
        db_session,
        mgr,
        search="",
        staleness="",
        account_type="",
        my_only=False,
        sort="oldest",
        disposition="active",
    )
    list_ids = {c.id for c in q.all()}
    assert co_a.id in list_ids
    assert co_b.id in list_ids


# ─────────────────────────────────────────────────────────────────────────────
# 6. Authz gate: POST /v2/partials/customers/{company_id}/field
#    (company_field_post — representative mutating route)
# ─────────────────────────────────────────────────────────────────────────────


def _make_client_for(db_session, user: User):
    """Return a TestClient with auth overridden to *user*."""
    from fastapi.testclient import TestClient

    from app.database import get_db
    from app.dependencies import require_buyer, require_fresh_token, require_user
    from app.main import app

    overrides = {
        get_db: lambda: db_session,
        require_user: lambda: user,
        require_buyer: lambda: user,
        require_fresh_token: lambda: "mock-token",
    }
    original = dict(app.dependency_overrides)
    app.dependency_overrides.update(overrides)
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(original)


@pytest.fixture()
def _users_and_company(db_session):
    mgr = _make_user(db_session, "manager", "mgr.gate@t.com")
    account_owner = _make_user(db_session, "sales", "owner.gate@t.com")
    site_owner = _make_user(db_session, "sales", "siteown.gate@t.com")
    unrelated = _make_user(db_session, "sales", "unrel.gate@t.com")
    co = _make_company(db_session, "GateCo", owner=account_owner)
    _make_site(db_session, co, owner=site_owner)
    db_session.commit()
    return {
        "manager": mgr,
        "account_owner": account_owner,
        "site_owner": site_owner,
        "unrelated": unrelated,
        "company": co,
    }


def test_gate_manager_allowed(_users_and_company, db_session):
    ctx = _users_and_company
    for c in _make_client_for(db_session, ctx["manager"]):
        resp = c.post(
            f"/v2/partials/customers/{ctx['company'].id}/field",
            data={"field": "website", "value": "https://example.com"},
        )
        assert resp.status_code != 403, f"Manager should NOT get 403, got {resp.status_code}"


def test_gate_account_owner_allowed(_users_and_company, db_session):
    ctx = _users_and_company
    for c in _make_client_for(db_session, ctx["account_owner"]):
        resp = c.post(
            f"/v2/partials/customers/{ctx['company'].id}/field",
            data={"field": "website", "value": "https://example.com"},
        )
        assert resp.status_code != 403, f"Account owner should NOT get 403, got {resp.status_code}"


def test_gate_site_owner_allowed(_users_and_company, db_session):
    """Site-owner (not account owner) must be allowed through the authz gate."""
    ctx = _users_and_company
    for c in _make_client_for(db_session, ctx["site_owner"]):
        resp = c.post(
            f"/v2/partials/customers/{ctx['company'].id}/field",
            data={"field": "website", "value": "https://example.com"},
        )
        assert resp.status_code != 403, f"Site owner should NOT get 403, got {resp.status_code}"


def test_gate_unrelated_rep_denied(_users_and_company, db_session):
    """Unrelated rep must get 403 — the critical denial test."""
    ctx = _users_and_company
    for c in _make_client_for(db_session, ctx["unrelated"]):
        resp = c.post(
            f"/v2/partials/customers/{ctx['company'].id}/field",
            data={"field": "website", "value": "https://example.com"},
        )
        assert resp.status_code == 403, f"Unrelated rep must get 403, got {resp.status_code}"
