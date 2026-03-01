"""Tests for contact_quality — validation, dedup, scoring, stale detection.

Covers: validate_contact, dedup_contacts, score_contact_completeness,
        flag_stale_contacts, compute_enrichment_status.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app.models.crm import Company, CustomerSite, SiteContact
from tests.conftest import engine  # noqa: F401


@pytest.fixture
def _mock_settings():
    with patch("app.services.contact_quality.settings") as mock_s:
        mock_s.customer_enrichment_contacts_per_account = 3
        yield mock_s


@pytest.fixture
def site_with_contacts(db_session):
    co = Company(name="Quality Corp", domain="quality.com", is_active=True)
    db_session.add(co)
    db_session.flush()
    site = CustomerSite(company_id=co.id, site_name="HQ")
    db_session.add(site)
    db_session.flush()
    return co, site


# ── validate_contact ────────────────────────────────────────────────


def test_validate_contact_valid():
    from app.services.contact_quality import validate_contact

    valid, issues = validate_contact(
        {
            "full_name": "Jane Doe",
            "email": "jane@acme.com",
            "phone": "+1-555-0100",
        }
    )
    assert valid is True
    assert issues == []


def test_validate_contact_missing_name():
    from app.services.contact_quality import validate_contact

    valid, issues = validate_contact({"email": "test@test.com"})
    assert valid is False
    assert "missing_name" in issues


def test_validate_contact_missing_email():
    from app.services.contact_quality import validate_contact

    valid, issues = validate_contact({"full_name": "Test"})
    assert valid is False
    assert "missing_email" in issues


def test_validate_contact_bad_email():
    from app.services.contact_quality import validate_contact

    valid, issues = validate_contact({"full_name": "Test", "email": "not-an-email"})
    assert valid is False
    assert "invalid_email_format" in issues


def test_validate_contact_short_phone():
    from app.services.contact_quality import validate_contact

    valid, issues = validate_contact({"full_name": "Test", "email": "t@t.com", "phone": "123"})
    assert valid is False
    assert "phone_too_short" in issues


# ── dedup_contacts ──────────────────────────────────────────────────


def test_dedup_contacts(db_session, site_with_contacts):
    co, site = site_with_contacts
    from app.services.contact_quality import dedup_contacts

    # Add duplicates
    db_session.add(
        SiteContact(
            customer_site_id=site.id,
            full_name="Jane Doe",
            email="jane@quality.com",
            phone="+1-555-0100",
        )
    )
    db_session.add(
        SiteContact(
            customer_site_id=site.id,
            full_name="Jane D.",
            email="jane@quality.com",
            title="VP Procurement",
        )
    )
    db_session.flush()

    merged = dedup_contacts(db_session, site.id)
    assert merged == 1
    # Primary should have the merged title
    active = (
        db_session.query(SiteContact)
        .filter_by(
            customer_site_id=site.id,
            is_active=True,
        )
        .all()
    )
    assert len(active) == 1
    assert active[0].title == "VP Procurement"


def test_dedup_no_duplicates(db_session, site_with_contacts):
    co, site = site_with_contacts
    from app.services.contact_quality import dedup_contacts

    db_session.add(
        SiteContact(
            customer_site_id=site.id,
            full_name="Alice",
            email="alice@quality.com",
        )
    )
    db_session.add(
        SiteContact(
            customer_site_id=site.id,
            full_name="Bob",
            email="bob@quality.com",
        )
    )
    db_session.flush()

    merged = dedup_contacts(db_session, site.id)
    assert merged == 0


# ── score_contact_completeness ──────────────────────────────────────


def test_score_complete_contact(db_session, site_with_contacts):
    co, site = site_with_contacts
    from app.services.contact_quality import score_contact_completeness

    contact = SiteContact(
        customer_site_id=site.id,
        full_name="Jane Doe",
        email="jane@quality.com",
        phone="+1-555-0100",
        phone_verified=True,
        title="VP Procurement",
        linkedin_url="https://linkedin.com/in/jane",
        email_verified=True,
    )
    db_session.add(contact)
    db_session.flush()

    score = score_contact_completeness(contact)
    assert score == 100


def test_score_minimal_contact(db_session, site_with_contacts):
    co, site = site_with_contacts
    from app.services.contact_quality import score_contact_completeness

    contact = SiteContact(
        customer_site_id=site.id,
        full_name="Unknown",
        email="test@test.com",
    )
    db_session.add(contact)
    db_session.flush()

    score = score_contact_completeness(contact)
    assert score == 25  # Only email


# ── flag_stale_contacts ─────────────────────────────────────────────


def test_flag_stale_contacts(db_session, site_with_contacts):
    co, site = site_with_contacts
    from app.services.contact_quality import flag_stale_contacts

    old_date = datetime.now(timezone.utc) - timedelta(days=200)
    contact = SiteContact(
        customer_site_id=site.id,
        full_name="Old Contact",
        email="old@quality.com",
        last_enriched_at=old_date,
        needs_refresh=False,
    )
    db_session.add(contact)
    db_session.flush()

    flagged = flag_stale_contacts(db_session, stale_days=180)
    assert flagged == 1
    db_session.refresh(contact)
    assert contact.needs_refresh is True


def test_flag_stale_contacts_fresh(db_session, site_with_contacts):
    co, site = site_with_contacts
    from app.services.contact_quality import flag_stale_contacts

    contact = SiteContact(
        customer_site_id=site.id,
        full_name="Fresh Contact",
        email="fresh@quality.com",
        last_enriched_at=datetime.now(timezone.utc),
        needs_refresh=False,
    )
    db_session.add(contact)
    db_session.flush()

    flagged = flag_stale_contacts(db_session, stale_days=180)
    assert flagged == 0


# ── compute_enrichment_status ──────────────────────────────────────


def test_compute_status_missing(db_session, site_with_contacts, _mock_settings):
    co, site = site_with_contacts
    from app.services.contact_quality import compute_enrichment_status

    assert compute_enrichment_status(db_session, co.id) == "missing"


def test_compute_status_partial(db_session, site_with_contacts, _mock_settings):
    co, site = site_with_contacts
    from app.services.contact_quality import compute_enrichment_status

    db_session.add(
        SiteContact(
            customer_site_id=site.id,
            full_name="One",
            email="one@quality.com",
        )
    )
    db_session.flush()
    assert compute_enrichment_status(db_session, co.id) == "partial"


def test_compute_status_complete(db_session, site_with_contacts, _mock_settings):
    co, site = site_with_contacts
    from app.services.contact_quality import compute_enrichment_status

    for i in range(3):
        db_session.add(
            SiteContact(
                customer_site_id=site.id,
                full_name=f"Contact {i}",
                email=f"c{i}@quality.com",
                email_verified=True,
            )
        )
    db_session.flush()
    assert compute_enrichment_status(db_session, co.id) == "complete"


def test_compute_status_stale(db_session, site_with_contacts, _mock_settings):
    co, site = site_with_contacts
    from app.services.contact_quality import compute_enrichment_status

    for i in range(3):
        db_session.add(
            SiteContact(
                customer_site_id=site.id,
                full_name=f"Contact {i}",
                email=f"c{i}@quality.com",
                needs_refresh=True,
            )
        )
    db_session.flush()
    assert compute_enrichment_status(db_session, co.id) == "stale"
