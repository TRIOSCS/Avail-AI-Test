"""Tests for Prospecting accounts-first hierarchy and 200-site cap.

Tests the new endpoints:
- GET /api/prospecting/my-accounts (grouped from owned sites)
- GET /api/prospecting/accounts/{id}/sites (sites within an account)
- GET /api/prospecting/capacity (site count vs 200 cap)
- POST /api/prospecting/release/{id} (release site back to pool)
- 200-site cap enforcement on claim endpoints

Called by: pytest
Depends on: conftest (db_session, sales_user, test_company)
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Company, User
from app.models.crm import CustomerSite

# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture()
def sales_client(db_session, sales_user):
    """TestClient authenticated as the sales user."""
    from app.database import get_db
    from app.dependencies import require_user
    from app.main import app

    def _override_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_user] = lambda: sales_user

    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(require_user, None)


def _make_company(db: Session, name: str = "Acme Corp", **kw) -> Company:
    defaults = {
        "name": name,
        "domain": f"{name.lower().replace(' ', '')}.com",
        "industry": "Aerospace & Defense",
        "hq_city": "Dallas",
        "hq_state": "TX",
        "is_active": True,
    }
    defaults.update(kw)
    co = Company(**defaults)
    db.add(co)
    db.commit()
    db.refresh(co)
    return co


def _make_site(db: Session, company: Company, owner: User | None = None, **kw) -> CustomerSite:
    defaults = {
        "company_id": company.id,
        "site_name": f"{company.name} - HQ",
        "owner_id": owner.id if owner else None,
        "is_active": True,
        "city": "Dallas",
        "state": "TX",
    }
    defaults.update(kw)
    site = CustomerSite(**defaults)
    db.add(site)
    db.commit()
    db.refresh(site)
    return site


# ═══════════════════════════════════════════════════════════════════════
#  GET /api/prospecting/my-accounts
# ═══════════════════════════════════════════════════════════════════════


def test_my_accounts_empty(sales_client):
    """No accounts when user owns no sites."""
    r = sales_client.get("/api/prospecting/my-accounts")
    assert r.status_code == 200
    assert r.json() == []


def test_my_accounts_grouped(sales_client, db_session, sales_user):
    """Accounts are grouped from owned sites."""
    co1 = _make_company(db_session, "Alpha Corp")
    co2 = _make_company(db_session, "Beta Corp")
    _make_site(db_session, co1, sales_user, site_name="Alpha HQ")
    _make_site(db_session, co1, sales_user, site_name="Alpha Branch")
    _make_site(db_session, co2, sales_user, site_name="Beta HQ")

    r = sales_client.get("/api/prospecting/my-accounts")
    assert r.status_code == 200
    accounts = r.json()
    assert len(accounts) == 2
    names = {a["name"] for a in accounts}
    assert "Alpha Corp" in names
    assert "Beta Corp" in names

    alpha = next(a for a in accounts if a["name"] == "Alpha Corp")
    assert alpha["site_count"] == 2
    assert alpha["company_id"] == co1.id


def test_my_accounts_health_status(sales_client, db_session, sales_user):
    """Health status correctly computed from last_activity_at."""
    co = _make_company(db_session, "Health Test")
    now = datetime.now(timezone.utc)

    # Active site (activity within 30 days)
    _make_site(
        db_session,
        co,
        sales_user,
        site_name="Active Site",
        last_activity_at=now - timedelta(days=5),
    )
    # Inactive site (no activity)
    _make_site(
        db_session,
        co,
        sales_user,
        site_name="Inactive Site",
    )

    r = sales_client.get("/api/prospecting/my-accounts")
    accounts = r.json()
    assert len(accounts) == 1
    # Mix of active and inactive = yellow
    assert accounts[0]["health"] == "yellow"
    assert accounts[0]["active_sites"] == 1
    assert accounts[0]["inactive_sites"] == 1


def test_my_accounts_excludes_other_users(sales_client, db_session, sales_user):
    """Only shows accounts where the current user owns sites."""
    other = User(email="other@test.com", name="Other", role="sales", azure_id="az-other")
    db_session.add(other)
    db_session.commit()
    db_session.refresh(other)

    co = _make_company(db_session, "Other Corp")
    _make_site(db_session, co, other)

    r = sales_client.get("/api/prospecting/my-accounts")
    assert r.json() == []


# ═══════════════════════════════════════════════════════════════════════
#  GET /api/prospecting/accounts/{id}/sites
# ═══════════════════════════════════════════════════════════════════════


def test_account_sites(sales_client, db_session, sales_user):
    """Returns sites for a specific company owned by user."""
    co = _make_company(db_session, "Sites Corp")
    s1 = _make_site(db_session, co, sales_user, site_name="HQ")
    s2 = _make_site(db_session, co, sales_user, site_name="Branch")

    r = sales_client.get(f"/api/prospecting/accounts/{co.id}/sites")
    assert r.status_code == 200
    data = r.json()
    assert data["company"]["name"] == "Sites Corp"
    assert len(data["sites"]) == 2


def test_account_sites_404(sales_client):
    """Returns 404 for nonexistent company."""
    r = sales_client.get("/api/prospecting/accounts/99999/sites")
    assert r.status_code == 404


# ═══════════════════════════════════════════════════════════════════════
#  GET /api/prospecting/capacity
# ═══════════════════════════════════════════════════════════════════════


def test_capacity_empty(sales_client):
    """Zero sites = 200 remaining."""
    r = sales_client.get("/api/prospecting/capacity")
    assert r.status_code == 200
    data = r.json()
    assert data["used"] == 0
    assert data["cap"] == 200
    assert data["remaining"] == 200
    assert data["at_cap"] is False


def test_capacity_with_sites(sales_client, db_session, sales_user):
    """Correctly counts owned sites."""
    co = _make_company(db_session, "Cap Test")
    for i in range(5):
        _make_site(db_session, co, sales_user, site_name=f"Site {i}")

    r = sales_client.get("/api/prospecting/capacity")
    data = r.json()
    assert data["used"] == 5
    assert data["remaining"] == 195
    assert data["at_cap"] is False


def test_capacity_stale_accounts(sales_client, db_session, sales_user):
    """Returns stale accounts (90+ days inactive) for nudges."""
    co = _make_company(db_session, "Stale Corp")
    old = datetime.now(timezone.utc) - timedelta(days=120)
    _make_site(
        db_session,
        co,
        sales_user,
        site_name="Stale Site",
        last_activity_at=old,
    )

    r = sales_client.get("/api/prospecting/capacity")
    data = r.json()
    assert len(data["stale_accounts"]) == 1
    assert data["stale_accounts"][0]["company_name"] == "Stale Corp"


# ═══════════════════════════════════════════════════════════════════════
#  POST /api/prospecting/release/{id}
# ═══════════════════════════════════════════════════════════════════════


def test_release_site(sales_client, db_session, sales_user):
    """Release a site back to the pool."""
    co = _make_company(db_session, "Release Corp")
    site = _make_site(db_session, co, sales_user)

    r = sales_client.post(f"/api/prospecting/release/{site.id}")
    assert r.status_code == 200
    assert r.json()["status"] == "released"

    db_session.refresh(site)
    assert site.owner_id is None
    assert site.ownership_cleared_at is not None


def test_release_site_not_yours(sales_client, db_session, sales_user):
    """Cannot release a site owned by another user."""
    other = User(email="other2@test.com", name="Other2", role="sales", azure_id="az-other2")
    db_session.add(other)
    db_session.commit()
    db_session.refresh(other)

    co = _make_company(db_session, "Other Corp 2")
    site = _make_site(db_session, co, other)

    r = sales_client.post(f"/api/prospecting/release/{site.id}")
    assert r.status_code == 403


def test_release_site_404(sales_client):
    """Returns 404 for nonexistent site."""
    r = sales_client.post("/api/prospecting/release/99999")
    assert r.status_code == 404


# ═══════════════════════════════════════════════════════════════════════
#  200-Site Cap Enforcement on Claim
# ═══════════════════════════════════════════════════════════════════════


def test_claim_site_under_cap(sales_client, db_session, sales_user):
    """Can claim a site when under the 200 cap."""
    co = _make_company(db_session, "Claim Corp")
    site = _make_site(db_session, co, owner=None)

    r = sales_client.post(f"/api/prospecting/claim/{site.id}")
    assert r.status_code == 200
    assert r.json()["status"] == "claimed"

    db_session.refresh(site)
    assert site.owner_id == sales_user.id


def test_claim_site_at_cap_blocked(sales_client, db_session, sales_user):
    """Cannot claim when at the 200-site cap."""
    # Create 200 owned sites
    co = _make_company(db_session, "Cap Corp")
    for i in range(200):
        _make_site(db_session, co, sales_user, site_name=f"Site {i}")

    # Try to claim one more
    co2 = _make_company(db_session, "New Corp")
    site = _make_site(db_session, co2, owner=None)

    r = sales_client.post(f"/api/prospecting/claim/{site.id}")
    assert r.status_code == 409
    body = r.json()
    msg = body.get("error") or body.get("detail") or ""
    assert "cap is 200" in msg.lower()


def test_claim_after_release_works(sales_client, db_session, sales_user):
    """Can claim again after releasing a site to get under cap."""
    co = _make_company(db_session, "Release Claim Corp")
    sites = []
    for i in range(200):
        sites.append(_make_site(db_session, co, sales_user, site_name=f"Site {i}"))

    # Release one
    r = sales_client.post(f"/api/prospecting/release/{sites[0].id}")
    assert r.status_code == 200

    # Now can claim
    co2 = _make_company(db_session, "Freed Corp")
    new_site = _make_site(db_session, co2, owner=None)
    r = sales_client.post(f"/api/prospecting/claim/{new_site.id}")
    assert r.status_code == 200
