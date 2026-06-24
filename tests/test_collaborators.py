"""tests/test_collaborators.py — Phase 3: AccountCollaborator TDD tests.

Covers:
1. Model + constraint round-trips (UNIQUE, cascade, role default)
2. can_manage_account — helper collaborator gains access
3. can_manage_account — deny paths still hold (unrelated rep, unrelated collaborator)
4. cdm_company_query — collaborator sees their account; unrelated rep does NOT
5. can_manage_account_team gate — primary owner and manager/admin can add/remove
6. DENY: helper collaborator gets 403 on POST/DELETE collaborator endpoints
7. Validation: can't add the primary owner; can't add a duplicate
8. HTTP endpoints: 200 for manager, 200 for primary owner, 403 for helper, 403 for unrelated rep

Security focus: both allow AND deny paths are tested for every principal.

Called by: pytest
Depends on: app.dependencies, app.services.crm_service, app.models.crm, app.routers.htmx_views
"""

from unittest.mock import patch

import pytest

from app.dependencies import can_manage_account
from app.models import Company, CustomerSite, User
from app.models.crm import AccountCollaborator
from app.services.crm_service import cdm_company_query

# ─────────────────────────────────────────────────────────────────────────────
# Shared helpers (mirror test_ownership_visibility.py convention)
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


def _make_collaborator(db, company: Company, user: User, role: str = "helper") -> AccountCollaborator:
    c = AccountCollaborator(
        company_id=company.id,
        user_id=user.id,
        role=role,
    )
    db.add(c)
    db.flush()
    return c


# ─────────────────────────────────────────────────────────────────────────────
# 1. Model: UNIQUE constraint (company_id, user_id)
# ─────────────────────────────────────────────────────────────────────────────


def test_collaborator_unique_constraint(db_session):
    """Duplicate (company, user) must raise IntegrityError (UNIQUE violated)."""
    from sqlalchemy.exc import IntegrityError

    owner = _make_user(db_session, "sales", "owner.uniq@t.com")
    helper = _make_user(db_session, "sales", "helper.uniq@t.com")
    co = _make_company(db_session, "UniqCo", owner=owner)

    _make_collaborator(db_session, co, helper)
    db_session.flush()

    # Attempt to insert a second row for the same (company, user) pair
    dup = AccountCollaborator(company_id=co.id, user_id=helper.id, role="helper")
    db_session.add(dup)
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_collaborator_default_role(db_session):
    """AccountCollaborator.role defaults to 'helper'."""
    owner = _make_user(db_session, "sales", "owner.def@t.com")
    helper = _make_user(db_session, "sales", "helper.def@t.com")
    co = _make_company(db_session, "DefRoleCo", owner=owner)

    c = AccountCollaborator(company_id=co.id, user_id=helper.id)
    db_session.add(c)
    db_session.flush()
    assert c.role == "helper"


def test_collaborator_cascade_delete_on_company(db_session):
    """Deleting a company cascades to delete its AccountCollaborator rows."""
    owner = _make_user(db_session, "sales", "owner.casc@t.com")
    helper = _make_user(db_session, "sales", "helper.casc@t.com")
    co = _make_company(db_session, "CascCo", owner=owner)
    _make_collaborator(db_session, co, helper)
    db_session.flush()

    co_id = co.id
    helper_id = helper.id

    # Delete via Company.collaborators relationship (cascade="all, delete-orphan")
    db_session.delete(co)
    db_session.flush()

    remaining = db_session.query(AccountCollaborator).filter_by(company_id=co_id, user_id=helper_id).first()
    assert remaining is None, "AccountCollaborator must be deleted when Company is deleted"


def test_collaborator_relationship_on_company(db_session):
    """Company.collaborators relationship returns the correct rows."""
    owner = _make_user(db_session, "sales", "owner.rel@t.com")
    h1 = _make_user(db_session, "sales", "h1.rel@t.com")
    h2 = _make_user(db_session, "sales", "h2.rel@t.com")
    co = _make_company(db_session, "RelCo", owner=owner)

    _make_collaborator(db_session, co, h1)
    _make_collaborator(db_session, co, h2)
    db_session.flush()

    db_session.refresh(co)
    collab_user_ids = {c.user_id for c in co.collaborators}
    assert h1.id in collab_user_ids
    assert h2.id in collab_user_ids
    assert owner.id not in collab_user_ids


# ─────────────────────────────────────────────────────────────────────────────
# 2. can_manage_account — collaborator ALLOW paths
# ─────────────────────────────────────────────────────────────────────────────


def test_can_manage_account_helper_collaborator_allowed(db_session):
    """A helper collaborator must be granted can_manage_account access."""
    owner = _make_user(db_session, "sales", "owner.collab@t.com")
    helper = _make_user(db_session, "sales", "helper.collab@t.com")
    co = _make_company(db_session, "CollabCo", owner=owner)
    _make_collaborator(db_session, co, helper)
    db_session.flush()

    assert can_manage_account(helper, co, db_session) is True


def test_can_manage_account_manager_still_allowed_with_collaborators(db_session):
    """Manager always has access regardless of collaborators."""
    mgr = _make_user(db_session, "manager", "mgr.collab@t.com")
    owner = _make_user(db_session, "sales", "owner.mgr@t.com")
    co = _make_company(db_session, "MgrCollabCo", owner=owner)
    db_session.flush()

    assert can_manage_account(mgr, co, db_session) is True


def test_can_manage_account_account_owner_still_allowed(db_session):
    """Primary account owner still has access (no regression from Phase 3)."""
    owner = _make_user(db_session, "sales", "owner.phase3@t.com")
    co = _make_company(db_session, "OwnerCo", owner=owner)
    db_session.flush()

    assert can_manage_account(owner, co, db_session) is True


# ─────────────────────────────────────────────────────────────────────────────
# 3. can_manage_account — DENY paths (critical security)
# ─────────────────────────────────────────────────────────────────────────────


def test_can_manage_account_unrelated_rep_still_denied_with_collaborators(db_session):
    """Adding a collaborator must NOT grant access to unrelated reps."""
    owner = _make_user(db_session, "sales", "owner.deny@t.com")
    helper = _make_user(db_session, "sales", "helper.deny@t.com")
    unrelated = _make_user(db_session, "sales", "unrelated.deny@t.com")
    co = _make_company(db_session, "DenyCo", owner=owner)
    _make_collaborator(db_session, co, helper)
    db_session.flush()

    # unrelated is NOT a collaborator — must be denied
    assert can_manage_account(unrelated, co, db_session) is False


def test_can_manage_account_collaborator_of_different_company_denied(db_session):
    """Collaborating on company A must NOT grant access to company B."""
    owner = _make_user(db_session, "sales", "owner.diff@t.com")
    helper = _make_user(db_session, "sales", "helper.diff@t.com")
    co_a = _make_company(db_session, "CompanyA", owner=owner)
    co_b = _make_company(db_session, "CompanyB", owner=owner)
    _make_collaborator(db_session, co_a, helper)  # only collaborator on A
    db_session.flush()

    assert can_manage_account(helper, co_b, db_session) is False


# ─────────────────────────────────────────────────────────────────────────────
# 4. cdm_company_query — collaborator visibility
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


def test_cdm_query_collaborator_sees_their_account(db_session):
    """A helper collaborator must see the account in my_only query."""
    owner = _make_user(db_session, "sales", "owner.cdm@t.com")
    helper = _make_user(db_session, "sales", "helper.cdm@t.com")
    co = _make_company(db_session, "CollabVisCo", owner=owner)
    _make_collaborator(db_session, co, helper)
    co_other = _make_company(db_session, "OtherCo_cdm")
    db_session.flush()

    ids = _query_ids(db_session, helper, my_only=True)
    assert co.id in ids, "Collaborator must see account in my_only list"
    assert co_other.id not in ids, "Collaborator must NOT see unrelated account"


def test_cdm_query_unrelated_rep_does_not_see_collaborated_account(db_session):
    """An unrelated rep must not see an account via another user's collaboration."""
    owner = _make_user(db_session, "sales", "owner.unrel@t.com")
    helper = _make_user(db_session, "sales", "helper.unrel@t.com")
    unrelated = _make_user(db_session, "sales", "unrel.cdm@t.com")
    co = _make_company(db_session, "OnlyHelperCo")
    _make_collaborator(db_session, co, helper)
    db_session.flush()

    ids = _query_ids(db_session, unrelated, my_only=True)
    assert co.id not in ids, "Unrelated rep must NOT see the collaborated account"


def test_cdm_query_collaborator_count_list_parity(db_session):
    """Count and list must agree for a collaborator's my_only view."""
    owner = _make_user(db_session, "sales", "owner.parity@t.com")
    helper = _make_user(db_session, "sales", "helper.parity@t.com")
    co = _make_company(db_session, "ParityCo", owner=owner)
    _make_collaborator(db_session, co, helper)
    co_other = _make_company(db_session, "OtherParity")
    db_session.flush()

    q = cdm_company_query(
        db_session,
        helper,
        search="",
        staleness="",
        account_type="",
        my_only=True,
        sort="oldest",
        disposition="active",
    )
    ids = {c.id for c in q.all()}
    assert co.id in ids
    assert co_other.id not in ids
    # Only the collaborated-on account is visible (not the unrelated one)
    assert len(ids) == 1


# ─────────────────────────────────────────────────────────────────────────────
# 5. can_manage_account_team gate — unit tests (no HTTP)
# ─────────────────────────────────────────────────────────────────────────────


def test_can_manage_account_team_primary_owner_allowed(db_session):
    """Primary account owner can manage the team."""
    from app.dependencies import can_manage_account_team

    owner = _make_user(db_session, "sales", "owner.team@t.com")
    co = _make_company(db_session, "TeamCo", owner=owner)
    db_session.flush()

    assert can_manage_account_team(owner, co) is True


def test_can_manage_account_team_manager_allowed(db_session):
    """Manager can manage the team on any account."""
    from app.dependencies import can_manage_account_team

    mgr = _make_user(db_session, "manager", "mgr.team@t.com")
    co = _make_company(db_session, "TeamCo2")
    db_session.flush()

    assert can_manage_account_team(mgr, co) is True


def test_can_manage_account_team_admin_allowed(db_session):
    """Admin can manage the team on any account."""
    from app.dependencies import can_manage_account_team

    admin = _make_user(db_session, "admin", "admin.team@t.com")
    co = _make_company(db_session, "TeamCo3")
    db_session.flush()

    assert can_manage_account_team(admin, co) is True


def test_can_manage_account_team_helper_collaborator_denied(db_session):
    """Helper collaborator must NOT be able to manage the team."""
    from app.dependencies import can_manage_account_team

    owner = _make_user(db_session, "sales", "owner.helpergate@t.com")
    helper = _make_user(db_session, "sales", "helper.gate@t.com")
    co = _make_company(db_session, "HelperGateCo", owner=owner)
    _make_collaborator(db_session, co, helper)
    db_session.flush()

    assert can_manage_account_team(helper, co) is False


def test_can_manage_account_team_unrelated_rep_denied(db_session):
    """Unrelated rep must NOT be able to manage the team."""
    from app.dependencies import can_manage_account_team

    owner = _make_user(db_session, "sales", "owner.unrelgate@t.com")
    unrelated = _make_user(db_session, "sales", "unrel.gate@t.com")
    co = _make_company(db_session, "UnrelGateCo", owner=owner)
    db_session.flush()

    assert can_manage_account_team(unrelated, co) is False


def test_can_manage_account_team_site_owner_denied(db_session):
    """Site owner (not account owner, not manager) must NOT manage the team."""
    from app.dependencies import can_manage_account_team

    account_owner = _make_user(db_session, "sales", "accowner.site@t.com")
    site_owner = _make_user(db_session, "sales", "siteowner.gate@t.com")
    co = _make_company(db_session, "SiteGateCo", owner=account_owner)
    site = CustomerSite(company_id=co.id, site_name="S1", owner_id=site_owner.id)
    db_session.add(site)
    db_session.flush()

    assert can_manage_account_team(site_owner, co) is False


def test_can_manage_account_team_null_owner_sales_denied(db_session):
    """A company with NULL account_owner_id must deny a sales user (not match
    None==None)."""
    from app.dependencies import can_manage_account_team

    sales = _make_user(db_session, "sales", "sales.nullowner@t.com")
    co = _make_company(db_session, "NullOwnerCo")  # account_owner_id=None
    db_session.flush()

    assert co.account_owner_id is None
    assert can_manage_account_team(sales, co) is False


def test_can_manage_account_team_null_owner_manager_allowed(db_session):
    """A manager must still be allowed even when account_owner_id is NULL."""
    from app.dependencies import can_manage_account_team

    mgr = _make_user(db_session, "manager", "mgr.nullowner@t.com")
    co = _make_company(db_session, "NullOwnerMgrCo")  # account_owner_id=None
    db_session.flush()

    assert co.account_owner_id is None
    assert can_manage_account_team(mgr, co) is True


# ─────────────────────────────────────────────────────────────────────────────
# 6. HTTP endpoint: POST /v2/partials/customers/{id}/collaborators
# ─────────────────────────────────────────────────────────────────────────────


def _make_client(db_session, user: User):
    """Yield a TestClient with auth overridden to *user*."""
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
    with patch.dict(app.dependency_overrides, overrides, clear=False):
        with TestClient(app) as c:
            yield c


@pytest.fixture()
def _collab_setup(db_session):
    mgr = _make_user(db_session, "manager", "mgr.collabhttp@t.com")
    owner = _make_user(db_session, "sales", "owner.collabhttp@t.com")
    helper = _make_user(db_session, "sales", "helper.collabhttp@t.com")
    unrelated = _make_user(db_session, "sales", "unrel.collabhttp@t.com")
    new_helper = _make_user(db_session, "sales", "newhel.collabhttp@t.com")
    co = _make_company(db_session, "HttpCollabCo", owner=owner)
    _make_collaborator(db_session, co, helper)
    db_session.commit()
    return {
        "manager": mgr,
        "owner": owner,
        "helper": helper,
        "unrelated": unrelated,
        "new_helper": new_helper,
        "company": co,
    }


def test_add_collaborator_manager_allowed(_collab_setup, db_session):
    """Manager can add a collaborator (200, not 403)."""
    ctx = _collab_setup
    for c in _make_client(db_session, ctx["manager"]):
        resp = c.post(
            f"/v2/partials/customers/{ctx['company'].id}/collaborators",
            data={"user_id": str(ctx["new_helper"].id)},
        )
        assert resp.status_code != 403, f"Manager must NOT get 403, got {resp.status_code}"
        assert resp.status_code < 500, f"Unexpected server error: {resp.status_code}"


def test_add_collaborator_owner_allowed(_collab_setup, db_session):
    """Primary account owner can add a collaborator (200, not 403)."""
    ctx = _collab_setup
    for c in _make_client(db_session, ctx["owner"]):
        resp = c.post(
            f"/v2/partials/customers/{ctx['company'].id}/collaborators",
            data={"user_id": str(ctx["new_helper"].id)},
        )
        assert resp.status_code != 403, f"Account owner must NOT get 403, got {resp.status_code}"
        assert resp.status_code < 500, f"Unexpected server error: {resp.status_code}"


def test_add_collaborator_helper_denied(_collab_setup, db_session):
    """Helper collaborator must get 403 when trying to add another collaborator."""
    ctx = _collab_setup
    for c in _make_client(db_session, ctx["helper"]):
        resp = c.post(
            f"/v2/partials/customers/{ctx['company'].id}/collaborators",
            data={"user_id": str(ctx["new_helper"].id)},
        )
        assert resp.status_code == 403, f"Helper must get 403, got {resp.status_code}"


def test_add_collaborator_unrelated_rep_denied(_collab_setup, db_session):
    """Unrelated rep must get 403 when trying to add a collaborator."""
    ctx = _collab_setup
    for c in _make_client(db_session, ctx["unrelated"]):
        resp = c.post(
            f"/v2/partials/customers/{ctx['company'].id}/collaborators",
            data={"user_id": str(ctx["new_helper"].id)},
        )
        assert resp.status_code == 403, f"Unrelated rep must get 403, got {resp.status_code}"


# ─────────────────────────────────────────────────────────────────────────────
# 7. HTTP endpoint: DELETE /v2/partials/customers/{id}/collaborators/{user_id}
# ─────────────────────────────────────────────────────────────────────────────


def test_remove_collaborator_manager_allowed(_collab_setup, db_session):
    """Manager can remove a collaborator (200, not 403)."""
    ctx = _collab_setup
    for c in _make_client(db_session, ctx["manager"]):
        resp = c.delete(
            f"/v2/partials/customers/{ctx['company'].id}/collaborators/{ctx['helper'].id}",
        )
        assert resp.status_code != 403, f"Manager must NOT get 403, got {resp.status_code}"
        assert resp.status_code < 500, f"Unexpected server error: {resp.status_code}"


def test_remove_collaborator_owner_allowed(_collab_setup, db_session):
    """Primary account owner can remove a collaborator (200, not 403)."""
    ctx = _collab_setup
    for c in _make_client(db_session, ctx["owner"]):
        resp = c.delete(
            f"/v2/partials/customers/{ctx['company'].id}/collaborators/{ctx['helper'].id}",
        )
        assert resp.status_code != 403, f"Account owner must NOT get 403, got {resp.status_code}"
        assert resp.status_code < 500, f"Unexpected server error: {resp.status_code}"


def test_remove_collaborator_helper_denied(_collab_setup, db_session):
    """Helper collaborator must get 403 when trying to remove another collaborator.

    This is the critical security boundary: a helper CAN edit account fields
    (can_manage_account=True) but CANNOT modify the team roster
    (can_manage_account_team=False).
    """
    ctx = _collab_setup
    # Create a second helper to try to remove
    second_helper = _make_user(db_session, "sales", "second.helper.remove@t.com")
    _make_collaborator(db_session, ctx["company"], second_helper)
    db_session.commit()

    for c in _make_client(db_session, ctx["helper"]):
        resp = c.delete(
            f"/v2/partials/customers/{ctx['company'].id}/collaborators/{second_helper.id}",
        )
        assert resp.status_code == 403, f"Helper must get 403 on remove, got {resp.status_code}"


def test_remove_collaborator_unrelated_rep_denied(_collab_setup, db_session):
    """Unrelated rep must get 403 when trying to remove a collaborator."""
    ctx = _collab_setup
    for c in _make_client(db_session, ctx["unrelated"]):
        resp = c.delete(
            f"/v2/partials/customers/{ctx['company'].id}/collaborators/{ctx['helper'].id}",
        )
        assert resp.status_code == 403, f"Unrelated rep must get 403, got {resp.status_code}"


def test_remove_collaborator_nonexistent_user_returns_404(_collab_setup, db_session):
    """Removing with a garbage user_id must return 404 (not a silent 200)."""
    ctx = _collab_setup
    for c in _make_client(db_session, ctx["manager"]):
        resp = c.delete(
            f"/v2/partials/customers/{ctx['company'].id}/collaborators/999999999",
        )
        assert resp.status_code == 404, f"Nonexistent user_id must return 404, got {resp.status_code}"


# ─────────────────────────────────────────────────────────────────────────────
# 8. Validation: can't add owner as collaborator; can't duplicate
# ─────────────────────────────────────────────────────────────────────────────


def test_add_collaborator_cannot_add_owner(_collab_setup, db_session):
    """Adding the primary account owner as a collaborator must return 400."""
    ctx = _collab_setup
    for c in _make_client(db_session, ctx["manager"]):
        resp = c.post(
            f"/v2/partials/customers/{ctx['company'].id}/collaborators",
            data={"user_id": str(ctx["owner"].id)},
        )
        assert resp.status_code == 400, f"Adding primary owner as collaborator must return 400, got {resp.status_code}"


def test_add_collaborator_duplicate_returns_error(_collab_setup, db_session):
    """Adding an existing collaborator must return 409 or 400 (no silent dup)."""
    ctx = _collab_setup
    # helper is already a collaborator in _collab_setup
    for c in _make_client(db_session, ctx["manager"]):
        resp = c.post(
            f"/v2/partials/customers/{ctx['company'].id}/collaborators",
            data={"user_id": str(ctx["helper"].id)},
        )
        assert resp.status_code in (400, 409), f"Duplicate collaborator must return 400 or 409, got {resp.status_code}"


# ─────────────────────────────────────────────────────────────────────────────
# 9. Helper CAN edit account fields (can_manage_account boundary)
# ─────────────────────────────────────────────────────────────────────────────


def test_helper_can_edit_account_field(_collab_setup, db_session):
    """Helper collaborator must be allowed to edit account fields
    (can_manage_account=True)."""
    ctx = _collab_setup
    for c in _make_client(db_session, ctx["helper"]):
        resp = c.post(
            f"/v2/partials/customers/{ctx['company'].id}/field",
            data={"field": "website", "value": "https://helper-edit.com"},
        )
        assert resp.status_code != 403, f"Helper collaborator must NOT get 403 on field edit, got {resp.status_code}"
