"""Tests for proactive matching shared helpers."""

from datetime import datetime, timedelta, timezone

from app.models import Company, User
from app.models.crm import CustomerSite
from app.models.intelligence import ProactiveDoNotOffer, ProactiveThrottle
from app.services.proactive_helpers import (
    build_batch_dno_set,
    build_batch_throttle_set,
    is_do_not_offer,
    is_throttled,
)
from tests.conftest import engine  # noqa: F401


def _make_company_and_site(db):
    owner = User(
        email="test@trioscs.com", name="Test", role="sales", azure_id="t-001", created_at=datetime.now(timezone.utc)
    )
    db.add(owner)
    db.flush()
    company = Company(name="Test Co", is_active=True, account_owner_id=owner.id)
    db.add(company)
    db.flush()
    site = CustomerSite(company_id=company.id, site_name="HQ", is_active=True)
    db.add(site)
    db.flush()
    return company, site


def test_is_do_not_offer_true(db_session):
    company, _ = _make_company_and_site(db_session)
    db_session.add(
        ProactiveDoNotOffer(
            mpn="LM358N",
            company_id=company.id,
            created_by_id=company.account_owner_id,
        )
    )
    db_session.commit()
    assert is_do_not_offer(db_session, "LM358N", company.id) is True


def test_is_do_not_offer_false(db_session):
    company, _ = _make_company_and_site(db_session)
    db_session.commit()
    assert is_do_not_offer(db_session, "LM358N", company.id) is False


def test_is_do_not_offer_normalizes_mpn(db_session):
    company, _ = _make_company_and_site(db_session)
    db_session.add(
        ProactiveDoNotOffer(
            mpn="LM358N",
            company_id=company.id,
            created_by_id=company.account_owner_id,
        )
    )
    db_session.commit()
    assert is_do_not_offer(db_session, "  lm358n  ", company.id) is True


def test_is_throttled_true(db_session):
    _, site = _make_company_and_site(db_session)
    db_session.add(
        ProactiveThrottle(
            mpn="LM358N",
            customer_site_id=site.id,
            last_offered_at=datetime.now(timezone.utc) - timedelta(days=5),
        )
    )
    db_session.commit()
    assert is_throttled(db_session, "LM358N", site.id) is True


def test_is_throttled_expired(db_session):
    _, site = _make_company_and_site(db_session)
    db_session.add(
        ProactiveThrottle(
            mpn="LM358N",
            customer_site_id=site.id,
            last_offered_at=datetime.now(timezone.utc) - timedelta(days=30),
        )
    )
    db_session.commit()
    assert is_throttled(db_session, "LM358N", site.id) is False


def test_build_batch_dno_set(db_session):
    company, _ = _make_company_and_site(db_session)
    db_session.add(
        ProactiveDoNotOffer(
            mpn="LM358N",
            company_id=company.id,
            created_by_id=company.account_owner_id,
        )
    )
    db_session.commit()
    result = build_batch_dno_set(db_session, "LM358N", {company.id})
    assert company.id in result


def test_build_batch_throttle_set(db_session):
    _, site = _make_company_and_site(db_session)
    db_session.add(
        ProactiveThrottle(
            mpn="LM358N",
            customer_site_id=site.id,
            last_offered_at=datetime.now(timezone.utc) - timedelta(days=5),
        )
    )
    db_session.commit()
    result = build_batch_throttle_set(db_session, "LM358N", {site.id})
    assert site.id in result


def test_build_batch_dno_set_empty(db_session):
    result = build_batch_dno_set(db_session, "LM358N", set())
    assert result == set()


def test_build_batch_throttle_set_empty(db_session):
    result = build_batch_throttle_set(db_session, "LM358N", set())
    assert result == set()


# ── Helper edge cases ─────────────────────────────────────────────────────


class TestHelperEdgeCases:
    """Edge cases for DNO/throttle helpers."""

    def test_is_throttled_exactly_at_boundary(self, db_session):
        """Throttle entry created exactly throttle_days ago → should NOT be throttled
        (expired)."""
        from unittest.mock import patch

        company, site = _make_company_and_site(db_session)
        cutoff = datetime.now(timezone.utc) - timedelta(days=90)
        throttle = ProactiveThrottle(
            customer_site_id=site.id,
            mpn="TEST-MPN",
            last_offered_at=cutoff,
        )
        db_session.add(throttle)
        db_session.flush()
        with patch("app.services.proactive_helpers.settings") as mock_s:
            mock_s.proactive_throttle_days = 90
            result = is_throttled(db_session, "TEST-MPN", site.id)
        assert result is False

    def test_batch_dno_with_duplicate_company_ids(self, db_session):
        """Passing duplicate company_ids should still work (set dedup)."""
        company, site = _make_company_and_site(db_session)
        owner = db_session.query(User).first()
        dno = ProactiveDoNotOffer(
            company_id=company.id,
            mpn="DUP-MPN",
            created_by_id=owner.id,
        )
        db_session.add(dno)
        db_session.flush()
        result = build_batch_dno_set(db_session, "DUP-MPN", {company.id, company.id})
        assert company.id in result

    def test_batch_throttle_empty_returns_empty(self, db_session):
        result = build_batch_throttle_set(db_session, "ANY-MPN", set())
        assert result == set()
