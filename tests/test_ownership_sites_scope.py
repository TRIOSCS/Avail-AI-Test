"""tests/test_ownership_sites_scope.py — Phase 2b: per-account site-scope TDD tests.

Tests that company_contact_rows() applies the three-way viewer scoping rule:
  - manager/admin → all sites
  - account owner → all sites
  - site-owner rep → only their owned sites
  - unrelated rep → empty (no sites owned)

Called by: pytest
Depends on: app.services.crm_service, app.models, app.models.auth
"""

import pytest
from sqlalchemy.orm import Session

from app.models import Company, CustomerSite, SiteContact
from app.models.auth import User
from app.services.crm_service import company_contact_rows

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _make_user(db: Session, role: str, email: str) -> User:
    u = User(
        email=email,
        name=email.split("@")[0],
        role=role,
        azure_id=f"az-{email}",
    )
    db.add(u)
    db.flush()
    return u


def _make_company(db: Session, name: str, owner: User | None = None) -> Company:
    co = Company(
        name=name,
        is_active=True,
        account_owner_id=owner.id if owner else None,
    )
    db.add(co)
    db.flush()
    return co


def _make_site(db: Session, company: Company, owner: User | None = None, name: str = "Site") -> CustomerSite:
    site = CustomerSite(
        company_id=company.id,
        site_name=name,
        owner_id=owner.id if owner else None,
        is_active=True,
    )
    db.add(site)
    db.flush()
    return site


def _make_contact(db: Session, site: CustomerSite, name: str = "Test Contact") -> SiteContact:
    c = SiteContact(
        customer_site_id=site.id,
        full_name=name,
        is_active=True,
    )
    db.add(c)
    db.flush()
    return c


def _site_ids(rows: list[dict]) -> set[int | None]:
    return {r["site"].id for r in rows if r["site"] is not None}


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures: one company, three sites, account owner = userA
#   S1.owner = userB, S2.owner = userC, S3.owner = userA
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture()
def scene(db_session: Session):
    user_a = _make_user(db_session, "sales", "usera@t.com")
    user_b = _make_user(db_session, "sales", "userb@t.com")
    user_c = _make_user(db_session, "sales", "userc@t.com")
    manager = _make_user(db_session, "manager", "mgr@t.com")
    admin = _make_user(db_session, "admin", "adm@t.com")
    unrelated = _make_user(db_session, "sales", "nobody@t.com")

    company = _make_company(db_session, "Acme Corp", owner=user_a)
    s1 = _make_site(db_session, company, owner=user_b, name="S1")
    s2 = _make_site(db_session, company, owner=user_c, name="S2")
    s3 = _make_site(db_session, company, owner=user_a, name="S3")

    # One SiteContact per site so rows exist
    _make_contact(db_session, s1, "Contact B")
    _make_contact(db_session, s2, "Contact C")
    _make_contact(db_session, s3, "Contact A")

    db_session.flush()
    return {
        "company": company,
        "s1": s1,
        "s2": s2,
        "s3": s3,
        "user_a": user_a,
        "user_b": user_b,
        "user_c": user_c,
        "manager": manager,
        "admin": admin,
        "unrelated": unrelated,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────


def test_site_owner_rep_sees_only_own_site(scene, db_session: Session):
    """UserB owns only S1; they should see only S1's contacts."""
    rows = company_contact_rows(db_session, scene["company"].id, viewer=scene["user_b"])
    ids = _site_ids(rows)
    assert scene["s1"].id in ids
    assert scene["s2"].id not in ids
    assert scene["s3"].id not in ids


def test_account_owner_sees_all_sites(scene, db_session: Session):
    """UserA is the account owner — they should see all three sites."""
    rows = company_contact_rows(db_session, scene["company"].id, viewer=scene["user_a"])
    ids = _site_ids(rows)
    assert {scene["s1"].id, scene["s2"].id, scene["s3"].id} == ids


def test_manager_sees_all_sites(scene, db_session: Session):
    """Manager role → all sites regardless of ownership."""
    rows = company_contact_rows(db_session, scene["company"].id, viewer=scene["manager"])
    ids = _site_ids(rows)
    assert {scene["s1"].id, scene["s2"].id, scene["s3"].id} == ids


def test_admin_sees_all_sites(scene, db_session: Session):
    """Admin role → all sites regardless of ownership."""
    rows = company_contact_rows(db_session, scene["company"].id, viewer=scene["admin"])
    ids = _site_ids(rows)
    assert {scene["s1"].id, scene["s2"].id, scene["s3"].id} == ids


def test_unrelated_rep_sees_no_sites(scene, db_session: Session):
    """Rep with no sites and not the account owner → zero rows."""
    rows = company_contact_rows(db_session, scene["company"].id, viewer=scene["unrelated"])
    assert rows == []


def test_no_viewer_returns_all_sites(scene, db_session: Session):
    """Backward compat: viewer=None → no scoping, all sites shown."""
    rows = company_contact_rows(db_session, scene["company"].id)
    ids = _site_ids(rows)
    assert {scene["s1"].id, scene["s2"].id, scene["s3"].id} == ids


def test_site_owner_c_sees_only_s2(scene, db_session: Session):
    """UserC owns only S2; they must not see S1 or S3."""
    rows = company_contact_rows(db_session, scene["company"].id, viewer=scene["user_c"])
    ids = _site_ids(rows)
    assert scene["s2"].id in ids
    assert scene["s1"].id not in ids
    assert scene["s3"].id not in ids


def test_site_owner_row_count_matches_own_site(scene, db_session: Session):
    """Row count for a site-owner is exactly their contacts (no cross-site bleed)."""
    rows = company_contact_rows(db_session, scene["company"].id, viewer=scene["user_b"])
    # S1 has exactly 1 SiteContact
    assert len([r for r in rows if not r["legacy"]]) == 1


def test_legacy_rows_scoped_correctly(scene, db_session: Session):
    """Legacy contact_* fields on a site are also scoped per viewer."""
    # Add a legacy contact on s1 (no SiteContact — just site.contact_name)
    scene["s1"].contact_name = "Legacy Person"
    scene["s1"].contact_email = "legacy@s1.com"
    db_session.flush()

    rows_b = company_contact_rows(db_session, scene["company"].id, viewer=scene["user_b"])
    legacy_rows = [r for r in rows_b if r["legacy"]]
    # userB owns S1; they should see its legacy row
    assert any(r["site"].id == scene["s1"].id for r in legacy_rows)

    # userC owns S2 — they must NOT see S1's legacy row
    rows_c = company_contact_rows(db_session, scene["company"].id, viewer=scene["user_c"])
    assert not any(r["site"].id == scene["s1"].id for r in rows_c)
