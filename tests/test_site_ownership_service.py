"""tests/test_site_ownership_service.py — Site ownership service unit tests.

Tests: get_open_pool_sites, claim_site, run_site_ownership_sweep, get_my_sites, get_sites_at_risk.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from app.models import ActivityLog, Company, CustomerSite, User


@pytest.fixture()
def company(db_session: Session) -> Company:
    co = Company(
        name="Test Corp",
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(co)
    db_session.commit()
    db_session.refresh(co)
    return co


@pytest.fixture()
def sales(db_session: Session) -> User:
    u = User(
        email="sales@test.com",
        name="Sales Rep",
        role="sales",
        azure_id="sales-az",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


@pytest.fixture()
def buyer(db_session: Session) -> User:
    u = User(
        email="buyer@test.com",
        name="Buyer Rep",
        role="buyer",
        azure_id="buyer-az",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


@pytest.fixture()
def unowned_site(db_session: Session, company: Company) -> CustomerSite:
    site = CustomerSite(
        company_id=company.id,
        site_name="Open Site",
        contact_name="Alice",
        contact_email="alice@test.com",
        city="Houston",
        state="TX",
        is_active=True,
    )
    db_session.add(site)
    db_session.commit()
    db_session.refresh(site)
    return site


@pytest.fixture()
def owned_site(db_session: Session, company: Company, sales: User) -> CustomerSite:
    site = CustomerSite(
        company_id=company.id,
        site_name="Owned Site",
        is_active=True,
        owner_id=sales.id,
        last_activity_at=datetime.now(timezone.utc) - timedelta(days=10),
    )
    db_session.add(site)
    db_session.commit()
    db_session.refresh(site)
    return site


# ── get_open_pool_sites ──────────────────────────────────────────────


class TestGetOpenPoolSites:
    def test_returns_unowned_active(self, db_session, unowned_site, company):
        from app.services.ownership_service import get_open_pool_sites

        result = get_open_pool_sites(db_session)
        assert len(result) >= 1
        site = next(s for s in result if s["site_id"] == unowned_site.id)
        assert site["site_name"] == "Open Site"
        assert site["company_name"] == "Test Corp"
        assert site["city"] == "Houston"

    def test_excludes_owned(self, db_session, owned_site, unowned_site):
        from app.services.ownership_service import get_open_pool_sites

        result = get_open_pool_sites(db_session)
        ids = [s["site_id"] for s in result]
        assert unowned_site.id in ids
        assert owned_site.id not in ids

    def test_excludes_inactive(self, db_session, company):
        from app.services.ownership_service import get_open_pool_sites

        inactive = CustomerSite(
            company_id=company.id,
            site_name="Inactive",
            is_active=False,
        )
        db_session.add(inactive)
        db_session.commit()

        result = get_open_pool_sites(db_session)
        ids = [s["site_id"] for s in result]
        assert inactive.id not in ids


# ── claim_site ───────────────────────────────────────────────────────


class TestClaimSite:
    def test_sales_can_claim(self, db_session, unowned_site, sales):
        from app.services.ownership_service import claim_site

        ok = claim_site(unowned_site.id, sales.id, db_session)
        assert ok is True
        db_session.refresh(unowned_site)
        assert unowned_site.owner_id == sales.id
        assert unowned_site.ownership_cleared_at is None

    def test_buyer_cannot_claim(self, db_session, unowned_site, buyer):
        from app.services.ownership_service import claim_site

        ok = claim_site(unowned_site.id, buyer.id, db_session)
        assert ok is False
        db_session.refresh(unowned_site)
        assert unowned_site.owner_id is None

    def test_already_owned_fails(self, db_session, owned_site, sales):
        from app.services.ownership_service import claim_site

        # Create another sales user trying to steal
        other = User(
            email="other@test.com",
            name="Other Sales",
            role="sales",
            azure_id="other-az",
        )
        db_session.add(other)
        db_session.commit()

        ok = claim_site(owned_site.id, other.id, db_session)
        assert ok is False

    def test_nonexistent_site(self, db_session, sales):
        from app.services.ownership_service import claim_site

        ok = claim_site(99999, sales.id, db_session)
        assert ok is False


# ── run_site_ownership_sweep ─────────────────────────────────────────


class TestSiteOwnershipSweep:
    def test_clears_stale_ownership(self, db_session, company, sales):
        """Site with 31 days of inactivity gets cleared."""
        from app.services.ownership_service import run_site_ownership_sweep

        stale = CustomerSite(
            company_id=company.id,
            site_name="Stale Site",
            is_active=True,
            owner_id=sales.id,
            last_activity_at=datetime.now(timezone.utc) - timedelta(days=31),
        )
        db_session.add(stale)
        db_session.commit()

        result = run_site_ownership_sweep(db_session)
        assert result["cleared"] >= 1

        db_session.refresh(stale)
        assert stale.owner_id is None
        assert stale.ownership_cleared_at is not None

    def test_warns_in_warning_zone(self, db_session, company, sales):
        """Site at day 24 gets a warning logged."""
        from app.services.ownership_service import run_site_ownership_sweep

        warn_site = CustomerSite(
            company_id=company.id,
            site_name="Warning Site",
            is_active=True,
            owner_id=sales.id,
            last_activity_at=datetime.now(timezone.utc) - timedelta(days=24),
        )
        db_session.add(warn_site)
        db_session.commit()

        result = run_site_ownership_sweep(db_session)
        assert result["warned"] >= 1

        # Verify warning activity log was created
        warning = (
            db_session.query(ActivityLog)
            .filter(
                ActivityLog.customer_site_id == warn_site.id,
                ActivityLog.activity_type == "ownership_warning",
            )
            .first()
        )
        assert warning is not None

    def test_healthy_site_untouched(self, db_session, owned_site):
        """Site with recent activity is not cleared or warned."""
        from app.services.ownership_service import run_site_ownership_sweep

        result = run_site_ownership_sweep(db_session)
        assert result["cleared"] == 0
        assert result["warned"] == 0

        db_session.refresh(owned_site)
        assert owned_site.owner_id is not None

    def test_no_activity_uses_created_at(self, db_session, company, sales):
        """Site with no last_activity_at uses created_at for age calculation."""
        from app.services.ownership_service import run_site_ownership_sweep

        old_site = CustomerSite(
            company_id=company.id,
            site_name="Old No Activity",
            is_active=True,
            owner_id=sales.id,
            created_at=datetime.now(timezone.utc) - timedelta(days=35),
        )
        db_session.add(old_site)
        db_session.commit()

        result = run_site_ownership_sweep(db_session)
        assert result["cleared"] >= 1

        db_session.refresh(old_site)
        assert old_site.owner_id is None


# ── get_my_sites ─────────────────────────────────────────────────────


class TestGetMySites:
    def test_returns_owned_with_health(self, db_session, owned_site, sales):
        from app.services.ownership_service import get_my_sites

        result = get_my_sites(sales.id, db_session)
        assert len(result) >= 1
        site = next(s for s in result if s["site_id"] == owned_site.id)
        assert site["status"] == "green"
        assert site["days_inactive"] is not None

    def test_yellow_status(self, db_session, company, sales):
        from app.services.ownership_service import get_my_sites

        site = CustomerSite(
            company_id=company.id,
            site_name="Yellow Site",
            is_active=True,
            owner_id=sales.id,
            last_activity_at=datetime.now(timezone.utc) - timedelta(days=25),
        )
        db_session.add(site)
        db_session.commit()

        result = get_my_sites(sales.id, db_session)
        s = next(s for s in result if s["site_id"] == site.id)
        assert s["status"] == "yellow"

    def test_no_activity_status(self, db_session, company, sales):
        from app.services.ownership_service import get_my_sites

        site = CustomerSite(
            company_id=company.id,
            site_name="No Activity Site",
            is_active=True,
            owner_id=sales.id,
        )
        db_session.add(site)
        db_session.commit()

        result = get_my_sites(sales.id, db_session)
        s = next(s for s in result if s["site_id"] == site.id)
        assert s["status"] == "no_activity"

    def test_empty_for_other_user(self, db_session, owned_site, buyer):
        from app.services.ownership_service import get_my_sites

        result = get_my_sites(buyer.id, db_session)
        ids = [s["site_id"] for s in result]
        assert owned_site.id not in ids


# ── get_sites_at_risk ────────────────────────────────────────────────


class TestGetSitesAtRisk:
    def test_stale_site_at_risk(self, db_session, company, sales):
        from app.services.ownership_service import get_sites_at_risk

        stale = CustomerSite(
            company_id=company.id,
            site_name="At Risk Site",
            is_active=True,
            owner_id=sales.id,
            last_activity_at=datetime.now(timezone.utc) - timedelta(days=24),
        )
        db_session.add(stale)
        db_session.commit()

        result = get_sites_at_risk(db_session)
        assert len(result) >= 1
        s = next(s for s in result if s["site_id"] == stale.id)
        assert s["days_remaining"] <= 7
        assert s["owner_name"] == "Sales Rep"

    def test_healthy_not_at_risk(self, db_session, owned_site):
        from app.services.ownership_service import get_sites_at_risk

        result = get_sites_at_risk(db_session)
        ids = [s["site_id"] for s in result]
        assert owned_site.id not in ids

    def test_sorted_by_urgency(self, db_session, company, sales):
        from app.services.ownership_service import get_sites_at_risk

        for i, days in enumerate([25, 28, 24]):
            s = CustomerSite(
                company_id=company.id,
                site_name=f"Risk {i}",
                is_active=True,
                owner_id=sales.id,
                last_activity_at=datetime.now(timezone.utc) - timedelta(days=days),
            )
            db_session.add(s)
        db_session.commit()

        result = get_sites_at_risk(db_session)
        # Most urgent (fewest days remaining) should come first
        remaining = [s["days_remaining"] for s in result]
        assert remaining == sorted(remaining)
