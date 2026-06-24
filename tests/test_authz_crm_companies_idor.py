"""Regression tests: cross-tenant IDOR guards on app/routers/crm/companies.py.

A SALES (restricted) user who does NOT own/manage a company must be blocked (403)
from mutating it or spending AI budget against it:
  PUT  /api/companies/{id}              (update_company)
  POST /api/companies/{id}/summarize   (summarize_company — AI spend)
  POST /api/companies/{id}/analyze-tags (analyze_company_tags — AI spend + tag writes)

Owner (account_owner_id == user) and manager/admin happy paths are allowed.

Setup style mirrors tests/test_authz_app_routers_crm_offers.py: the `client` fixture
auth-overrides to `test_user`; we mutate test_user.role and the company's ownership to
exercise non-owner vs owner/manager.
"""

import pytest

from app.constants import UserRole
from app.models import Company


@pytest.fixture()
def foreign_company(db_session, admin_user):
    """A company owned by *someone else* (admin), so a SALES test_user is a non-
    owner."""
    co = Company(name="Foreign Megacorp", account_owner_id=admin_user.id, is_active=True)
    db_session.add(co)
    db_session.commit()
    db_session.refresh(co)
    return co


def _make_sales(test_user, db_session):
    test_user.role = UserRole.SALES
    db_session.commit()


# ── Non-owner SALES is blocked (403) ────────────────────────────────────


def test_update_company_blocks_non_owner_sales(client, db_session, test_user, foreign_company):
    _make_sales(test_user, db_session)
    resp = client.put(f"/api/companies/{foreign_company.id}", json={"name": "Hijacked"})
    assert resp.status_code == 403
    db_session.refresh(foreign_company)
    assert foreign_company.name == "Foreign Megacorp"


def test_summarize_company_blocks_non_owner_sales(client, db_session, test_user, foreign_company):
    _make_sales(test_user, db_session)
    resp = client.post(f"/api/companies/{foreign_company.id}/summarize")
    assert resp.status_code == 403


def test_analyze_tags_blocks_non_owner_sales(client, db_session, test_user, foreign_company):
    _make_sales(test_user, db_session)
    resp = client.post(f"/api/companies/{foreign_company.id}/analyze-tags")
    assert resp.status_code == 403


# ── Owner SALES passes the gate (update is a clean happy path) ───────────


def test_update_company_allows_owning_sales(client, db_session, test_user):
    """When the SALES user OWNS the account, the gate is a no-op."""
    test_user.role = UserRole.SALES
    co = Company(name="My Account", account_owner_id=test_user.id, is_active=True)
    db_session.add(co)
    db_session.commit()
    db_session.refresh(co)
    resp = client.put(f"/api/companies/{co.id}", json={"name": "My Account Renamed"})
    assert resp.status_code == 200
    db_session.refresh(co)
    assert co.name == "My Account Renamed"


# ── Manager passes the gate regardless of ownership ─────────────────────


def test_update_company_allows_manager(client, db_session, test_user, foreign_company):
    """Manager/admin tier manages every account."""
    test_user.role = UserRole.MANAGER
    db_session.commit()
    resp = client.put(f"/api/companies/{foreign_company.id}", json={"name": "Manager Edit"})
    assert resp.status_code == 200
    db_session.refresh(foreign_company)
    assert foreign_company.name == "Manager Edit"


# ── Missing company still 404s (no existence leak via the new gate) ──────


def test_update_company_missing_404(client, db_session, test_user):
    test_user.role = UserRole.MANAGER
    db_session.commit()
    resp = client.put("/api/companies/999999", json={"name": "x"})
    assert resp.status_code == 404
