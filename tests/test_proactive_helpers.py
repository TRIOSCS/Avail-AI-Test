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
