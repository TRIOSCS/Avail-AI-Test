"""tests/test_site_ownership_service.py — Site ownership service unit tests.

Tests: get_open_pool_sites, claim_site, run_site_ownership_sweep, get_my_sites, get_sites_at_risk.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from app.models import ActivityLog, Company, CustomerSite, User


@pytest.fixture()
def company(db_session: Session) -> Company:
    co = Company(
        name="Test Corp",
        is_active=True,
        created_at=datetime.now(UTC),
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
        created_at=datetime.now(UTC),
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
        created_at=datetime.now(UTC),
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
        last_activity_at=datetime.now(UTC) - timedelta(days=10),
    )
    db_session.add(site)
    db_session.commit()
    db_session.refresh(site)
    return site


# ── get_open_pool_sites ──────────────────────────────────────────────


# ── claim_site ───────────────────────────────────────────────────────


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
            last_activity_at=datetime.now(UTC) - timedelta(days=31),
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
            last_activity_at=datetime.now(UTC) - timedelta(days=24),
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
            created_at=datetime.now(UTC) - timedelta(days=35),
        )
        db_session.add(old_site)
        db_session.commit()

        result = run_site_ownership_sweep(db_session)
        assert result["cleared"] >= 1

        db_session.refresh(old_site)
        assert old_site.owner_id is None


# ── get_my_sites ─────────────────────────────────────────────────────


# ── get_sites_at_risk ────────────────────────────────────────────────
