"""
tests/test_prospecting.py — Prospecting Pool API endpoint tests.

Tests: pool listing, claim, my-sites, at-risk, admin assign, ownership guard on site update.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Company, CustomerSite, User

# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture()
def sales_client(db_session: Session, sales_user: User) -> TestClient:
    """TestClient authenticated as a sales user."""
    from app.database import get_db
    from app.dependencies import require_user
    from app.main import app

    def _db():
        yield db_session

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[require_user] = lambda: sales_user

    with TestClient(app) as c:
        yield c

    app.dependency_overrides.clear()


@pytest.fixture()
def admin_client(db_session: Session, admin_user: User) -> TestClient:
    """TestClient authenticated as an admin user."""
    from app.database import get_db
    from app.dependencies import require_admin, require_user
    from app.main import app

    def _db():
        yield db_session

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[require_user] = lambda: admin_user
    app.dependency_overrides[require_admin] = lambda: admin_user

    with TestClient(app) as c:
        yield c

    app.dependency_overrides.clear()


@pytest.fixture()
def buyer_client(db_session: Session, test_user: User) -> TestClient:
    """TestClient authenticated as a buyer user (non-sales)."""
    from app.database import get_db
    from app.dependencies import require_user
    from app.main import app

    def _db():
        yield db_session

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[require_user] = lambda: test_user

    with TestClient(app) as c:
        yield c

    app.dependency_overrides.clear()


@pytest.fixture()
def unowned_site(db_session: Session, test_company: Company) -> CustomerSite:
    """An active site with no owner."""
    site = CustomerSite(
        company_id=test_company.id,
        site_name="Unowned Branch",
        contact_name="Bob Test",
        contact_email="bob@acme-electronics.com",
        city="Austin",
        state="TX",
        is_active=True,
    )
    db_session.add(site)
    db_session.commit()
    db_session.refresh(site)
    return site


@pytest.fixture()
def owned_site(db_session: Session, test_company: Company, sales_user: User) -> CustomerSite:
    """An active site owned by sales_user."""
    site = CustomerSite(
        company_id=test_company.id,
        site_name="Owned HQ",
        contact_name="Jane Test",
        contact_email="jane@acme-electronics.com",
        city="Dallas",
        state="TX",
        is_active=True,
        owner_id=sales_user.id,
        last_activity_at=datetime.now(timezone.utc) - timedelta(days=5),
    )
    db_session.add(site)
    db_session.commit()
    db_session.refresh(site)
    return site


# ── GET /api/prospecting/pool ─────────────────────────────────────────


class TestProspectingPool:
    def test_pool_empty(self, sales_client, db_session, test_company):
        """Pool is empty when all sites have owners."""
        resp = sales_client.get("/api/prospecting/pool")
        assert resp.status_code == 200
        # No sites created yet, pool should be empty
        assert resp.json() == []

    def test_pool_populated(self, sales_client, unowned_site):
        """Pool returns unowned active sites."""
        resp = sales_client.get("/api/prospecting/pool")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) >= 1
        site = next(s for s in data if s["site_id"] == unowned_site.id)
        assert site["site_name"] == "Unowned Branch"
        assert site["company_name"] == "Acme Electronics"
        assert site["city"] == "Austin"

    def test_pool_excludes_owned(self, sales_client, owned_site, unowned_site):
        """Pool excludes sites with an owner."""
        resp = sales_client.get("/api/prospecting/pool")
        data = resp.json()
        ids = [s["site_id"] for s in data]
        assert unowned_site.id in ids
        assert owned_site.id not in ids


# ── POST /api/prospecting/claim/{site_id} ─────────────────────────────


class TestProspectingClaim:
    def test_claim_success(self, sales_client, unowned_site, sales_user, db_session):
        """Sales user can claim an unowned site."""
        resp = sales_client.post(f"/api/prospecting/claim/{unowned_site.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "claimed"
        assert data["site_id"] == unowned_site.id

        # Verify in DB
        db_session.refresh(unowned_site)
        assert unowned_site.owner_id == sales_user.id

    def test_claim_409_already_owned(self, sales_client, owned_site):
        """Claiming an already-owned site returns 409."""
        resp = sales_client.post(f"/api/prospecting/claim/{owned_site.id}")
        assert resp.status_code == 409

    def test_claim_404_not_found(self, sales_client):
        """Claiming a non-existent site returns 404."""
        resp = sales_client.post("/api/prospecting/claim/99999")
        assert resp.status_code == 404

    def test_claim_403_buyer_role(self, buyer_client, unowned_site):
        """Buyer role cannot claim sites."""
        resp = buyer_client.post(f"/api/prospecting/claim/{unowned_site.id}")
        assert resp.status_code == 403


# ── GET /api/prospecting/my-sites ──────────────────────────────────────


class TestProspectingMySites:
    def test_my_sites_returns_owned(self, sales_client, owned_site):
        """Returns sites owned by the current user."""
        resp = sales_client.get("/api/prospecting/my-sites")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) >= 1
        site = next(s for s in data if s["site_id"] == owned_site.id)
        assert site["site_name"] == "Owned HQ"
        assert site["status"] == "green"  # recent activity

    def test_my_sites_empty_for_buyer(self, buyer_client, owned_site):
        """Buyer has no owned sites."""
        resp = buyer_client.get("/api/prospecting/my-sites")
        assert resp.status_code == 200
        assert resp.json() == []


# ── GET /api/prospecting/at-risk ───────────────────────────────────────


class TestProspectingAtRisk:
    def test_at_risk_empty_when_healthy(self, sales_client, owned_site):
        """No sites at risk when activity is recent."""
        resp = sales_client.get("/api/prospecting/at-risk")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_at_risk_includes_stale_site(self, sales_client, db_session, test_company, sales_user):
        """Site with old activity appears in at-risk list."""
        stale = CustomerSite(
            company_id=test_company.id,
            site_name="Stale Site",
            is_active=True,
            owner_id=sales_user.id,
            last_activity_at=datetime.now(timezone.utc) - timedelta(days=25),
        )
        db_session.add(stale)
        db_session.commit()

        resp = sales_client.get("/api/prospecting/at-risk")
        data = resp.json()
        assert len(data) >= 1
        site = next(s for s in data if s["site_id"] == stale.id)
        assert site["days_remaining"] <= 7


# ── PUT /api/prospecting/sites/{site_id}/owner (admin) ────────────────


class TestProspectingAssignOwner:
    def test_admin_assign(self, admin_client, unowned_site, sales_user, db_session):
        """Admin can assign a site to a user."""
        resp = admin_client.put(
            f"/api/prospecting/sites/{unowned_site.id}/owner",
            json={"owner_id": sales_user.id},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["owner_id"] == sales_user.id

        db_session.refresh(unowned_site)
        assert unowned_site.owner_id == sales_user.id

    def test_admin_unassign(self, admin_client, owned_site, db_session):
        """Admin can unassign a site."""
        resp = admin_client.put(
            f"/api/prospecting/sites/{owned_site.id}/owner",
            json={"owner_id": None},
        )
        assert resp.status_code == 200
        db_session.refresh(owned_site)
        assert owned_site.owner_id is None

    def test_assign_404(self, admin_client):
        """Assigning to non-existent site returns 404."""
        resp = admin_client.put(
            "/api/prospecting/sites/99999/owner",
            json={"owner_id": 1},
        )
        assert resp.status_code == 404

    def test_assign_blocked_at_cap(self, admin_client, db_session, sales_user, test_company):
        """Admin assign blocked when target user already at 200-site cap."""
        from app.routers.v13_features import SITE_CAP_PER_USER

        # Create SITE_CAP_PER_USER active sites for sales_user
        for i in range(SITE_CAP_PER_USER):
            db_session.add(
                CustomerSite(
                    company_id=test_company.id,
                    site_name=f"Cap Site {i}",
                    owner_id=sales_user.id,
                    is_active=True,
                )
            )
        db_session.commit()

        # Create a new unowned site
        new_site = CustomerSite(
            company_id=test_company.id,
            site_name="Over Cap Site",
            is_active=True,
        )
        db_session.add(new_site)
        db_session.commit()
        db_session.refresh(new_site)

        # Assign should be blocked without force
        resp = admin_client.put(
            f"/api/prospecting/sites/{new_site.id}/owner",
            json={"owner_id": sales_user.id},
        )
        assert resp.status_code == 409
        assert "cap" in resp.json()["error"].lower()

    def test_assign_force_override(self, admin_client, db_session, sales_user, test_company):
        """Admin can force-assign past cap with force=true."""
        from app.routers.v13_features import SITE_CAP_PER_USER

        for i in range(SITE_CAP_PER_USER):
            db_session.add(
                CustomerSite(
                    company_id=test_company.id,
                    site_name=f"Force Cap Site {i}",
                    owner_id=sales_user.id,
                    is_active=True,
                )
            )
        db_session.commit()

        new_site = CustomerSite(
            company_id=test_company.id,
            site_name="Forced Site",
            is_active=True,
        )
        db_session.add(new_site)
        db_session.commit()
        db_session.refresh(new_site)

        resp = admin_client.put(
            f"/api/prospecting/sites/{new_site.id}/owner",
            json={"owner_id": sales_user.id, "force": True},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert "warning" in data


# ── PUT /api/sites/{site_id} ownership guard ──────────────────────────


class TestSiteUpdateOwnershipGuard:
    def test_non_admin_cannot_reassign_owned(self, sales_client, owned_site, db_session):
        """Sales user cannot change owner_id on an owned site."""
        other_user = User(
            email="other@trioscs.com",
            name="Other",
            role="sales",
            azure_id="other-id",
        )
        db_session.add(other_user)
        db_session.commit()

        resp = sales_client.put(
            f"/api/sites/{owned_site.id}",
            json={"owner_id": other_user.id},
        )
        assert resp.status_code == 403

    def test_non_admin_cannot_unassign(self, sales_client, owned_site):
        """Sales user cannot set owner_id to None on an owned site."""
        resp = sales_client.put(
            f"/api/sites/{owned_site.id}",
            json={"owner_id": None},
        )
        assert resp.status_code == 403

    def test_admin_can_reassign(self, admin_client, owned_site, admin_user, db_session):
        """Admin can reassign an owned site."""
        resp = admin_client.put(
            f"/api/sites/{owned_site.id}",
            json={"owner_id": admin_user.id},
        )
        assert resp.status_code == 200
        db_session.refresh(owned_site)
        assert owned_site.owner_id == admin_user.id

    def test_non_owner_fields_allowed(self, sales_client, owned_site, db_session):
        """Sales user can update non-owner fields on a site."""
        resp = sales_client.put(
            f"/api/sites/{owned_site.id}",
            json={"notes": "Updated notes"},
        )
        assert resp.status_code == 200
        db_session.refresh(owned_site)
        assert owned_site.notes == "Updated notes"
