"""TDD: bulk "Assign owner" name+role <select> for managers/admins.

The bulk assign-owner control used to be a raw numeric "User ID" text input. This
replaces it with a proper name+role <select> populated from the active user list,
carried ONLY for managers/admins (the action is manager/admin-only server-side).

Covers:
  - cdm_list_ctx(include_users=True) adds a non-empty "users" key (active, name-sorted)
  - cdm_list_ctx default (include_users=False) omits the "users" key entirely
  - cdm_list_ctx(include_users=True) excludes inactive users from "users"
  - A manager render of the account list carries <select name="owner_id"> (not the raw input)
  - A sales-rep render does NOT carry the user list (no <select name="owner_id">)

Called by: pytest
Depends on: conftest.py (db_session, test_user), local manager/rep fixtures + client helper
"""

from __future__ import annotations

import os

os.environ["TESTING"] = "1"

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.auth import User
from app.models.crm import Company
from app.services.crm_service import cdm_list_ctx

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mgr_user(db_session: Session) -> User:
    """A manager-role user (carries the assign-owner user list)."""
    u = User(
        email="mgr.assign@trioscs.com",
        name="Manager Assign",
        role="manager",
        azure_id="assign-test-mgr-001",
        created_at=datetime.now(UTC),
    )
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


@pytest.fixture()
def sales_rep(db_session: Session) -> User:
    """A sales-role user (must NOT carry the assign-owner user list)."""
    u = User(
        email="rep.assign@trioscs.com",
        name="Rep Assign",
        role="sales",
        azure_id="assign-test-rep-001",
        created_at=datetime.now(UTC),
    )
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


@pytest.fixture()
def inactive_user(db_session: Session) -> User:
    """An inactive user — must be excluded from the assign-owner options."""
    u = User(
        email="ghost.assign@trioscs.com",
        name="Ghost Assign",
        role="sales",
        azure_id="assign-test-ghost-001",
        is_active=False,
        created_at=datetime.now(UTC),
    )
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


def _make_client(db_session: Session, user: User):
    """Build a TestClient authenticated as *user* (mirrors test_crm_bulk_import)."""
    from app.database import get_db
    from app.dependencies import require_admin, require_buyer, require_fresh_token, require_user
    from app.main import app

    def _db():
        yield db_session

    def _u():
        return user

    async def _ft():
        return "mock-token"

    overrides = {
        get_db: _db,
        require_user: _u,
        require_admin: _u,
        require_buyer: _u,
        require_fresh_token: _ft,
    }
    for dep, fn in overrides.items():
        app.dependency_overrides[dep] = fn
    try:
        with TestClient(app) as c:
            yield c
    finally:
        for dep in overrides:
            app.dependency_overrides.pop(dep, None)


# ---------------------------------------------------------------------------
# UNIT: cdm_list_ctx include_users
# ---------------------------------------------------------------------------


def _ctx(db: Session, user: User, **kw) -> dict:
    return cdm_list_ctx(
        db,
        user,
        search="",
        staleness="",
        account_type="",
        my_only=False,
        sort="name_asc",
        limit=50,
        offset=0,
        **kw,
    )


def test_include_users_true_adds_nonempty_user_list(db_session: Session, mgr_user: User):
    """include_users=True surfaces a non-empty, active-only "users" list."""
    ctx = _ctx(db_session, mgr_user, include_users=True)
    assert "users" in ctx
    assert len(ctx["users"]) >= 1
    assert all(u.is_active for u in ctx["users"])
    assert mgr_user.id in {u.id for u in ctx["users"]}


def test_include_users_default_omits_user_list(db_session: Session, mgr_user: User):
    """The default (include_users=False) omits the "users" key entirely."""
    ctx = _ctx(db_session, mgr_user)
    assert "users" not in ctx


def test_include_users_excludes_inactive(db_session: Session, mgr_user: User, inactive_user: User):
    """Inactive users never appear in the assign-owner options."""
    ctx = _ctx(db_session, mgr_user, include_users=True)
    user_ids = {u.id for u in ctx["users"]}
    assert inactive_user.id not in user_ids


def test_include_users_sorted_by_name(db_session: Session, mgr_user: User):
    """The user list is ordered by name (stable dropdown ordering)."""
    db_session.add_all(
        [
            User(
                email="z@trioscs.com",
                name="Zach",
                role="sales",
                azure_id="assign-z",
                created_at=datetime.now(UTC),
            ),
            User(
                email="a@trioscs.com",
                name="Aaron",
                role="sales",
                azure_id="assign-a",
                created_at=datetime.now(UTC),
            ),
        ]
    )
    db_session.commit()
    ctx = _ctx(db_session, mgr_user, include_users=True)
    names = [u.name for u in ctx["users"]]
    assert names == sorted(names)


# ---------------------------------------------------------------------------
# ROUTE / TEMPLATE: owner_id <select> for managers, absent for reps
# ---------------------------------------------------------------------------


def test_manager_account_list_renders_owner_select(db_session: Session, mgr_user: User):
    """A manager render of the account list carries <select name="owner_id">."""
    db_session.add(Company(name="Sel Co", is_active=True))
    db_session.commit()
    for c in _make_client(db_session, mgr_user):
        resp = c.get("/v2/partials/customers/account-list")
        assert resp.status_code == 200
        assert '<select name="owner_id"' in resp.text
        # The raw numeric "User ID" input must be gone.
        assert 'placeholder="User ID"' not in resp.text
        # The manager themselves is an option.
        assert f'value="{mgr_user.id}"' in resp.text


def test_rep_account_list_omits_owner_select(db_session: Session, sales_rep: User):
    """A sales-rep render does NOT carry the user list (no owner_id <select>)."""
    db_session.add(Company(name="Rep Sel Co", is_active=True, account_owner_id=sales_rep.id))
    db_session.commit()
    for c in _make_client(db_session, sales_rep):
        resp = c.get("/v2/partials/customers/account-list")
        assert resp.status_code == 200
        assert '<select name="owner_id"' not in resp.text


def test_manager_full_list_renders_owner_select(db_session: Session, mgr_user: User):
    """The full list partial (initial page include) also carries the owner select."""
    db_session.add(Company(name="Full Sel Co", is_active=True))
    db_session.commit()
    for c in _make_client(db_session, mgr_user):
        resp = c.get("/v2/partials/customers")
        assert resp.status_code == 200
        assert '<select name="owner_id"' in resp.text
