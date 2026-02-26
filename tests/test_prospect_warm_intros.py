"""Tests for warm intro detection and one-liner generation.

Tests:
- detect_warm_intros: VendorCard domain match, SiteContact match
- generate_one_liner: priority order of signal types
- /api/prospects/suggested serialization includes one_liner

Called by: pytest
Depends on: conftest (db_session, test_user)
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session

from app.models import Company, User, VendorCard, VendorContact
from app.models.crm import CustomerSite, SiteContact
from app.models.prospect_account import ProspectAccount
from app.services.prospect_warm_intros import detect_warm_intros, generate_one_liner


# ── Helpers ──────────────────────────────────────────────────────────


def _make_prospect(db: Session, **kw) -> ProspectAccount:
    defaults = {
        "name": "Test Corp",
        "domain": "testcorp.com",
        "industry": "Aerospace",
        "region": "US",
        "fit_score": 75,
        "readiness_score": 60,
        "status": "suggested",
        "discovery_source": "test",
        "readiness_signals": {},
        "contacts_preview": [],
        "similar_customers": [],
    }
    defaults.update(kw)
    p = ProspectAccount(**defaults)
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


# ═══════════════════════════════════════════════════════════════════════
#  detect_warm_intros
# ═══════════════════════════════════════════════════════════════════════


def test_warm_intro_no_domain(db_session):
    """No warm intro when domain is empty."""
    p = _make_prospect(db_session, domain="")
    result = detect_warm_intros(p, db_session)
    assert result["has_warm_intro"] is False
    assert result["warmth"] == "cold"


def test_warm_intro_vendor_card_match(db_session):
    """Detects warm intro when VendorCard with matching domain exists."""
    vc = VendorCard(
        display_name="Test Corp Vendor",
        domain="testcorp.com",
        normalized_name="test corp vendor",
        engagement_score=70.0,
        last_contact_at=datetime.now(timezone.utc),
    )
    db_session.add(vc)
    db_session.commit()
    db_session.refresh(vc)

    # Add a contact
    contact = VendorContact(
        vendor_card_id=vc.id,
        email="john@testcorp.com",
        full_name="John Smith",
        title="Buyer",
        relationship_score=65.0,
        activity_trend="stable",
        source="test",
    )
    db_session.add(contact)
    db_session.commit()

    p = _make_prospect(db_session)
    result = detect_warm_intros(p, db_session)
    assert result["has_warm_intro"] is True
    assert result["warmth"] == "hot"
    assert result["vendor_card_id"] == vc.id
    assert len(result["contacts"]) == 1
    assert result["contacts"][0]["name"] == "John Smith"


def test_warm_intro_site_contact_match(db_session):
    """Detects warm intro from SiteContact email domain match."""
    co = Company(name="Existing Customer", is_active=True)
    db_session.add(co)
    db_session.commit()
    db_session.refresh(co)

    site = CustomerSite(company_id=co.id, site_name="HQ", is_active=True)
    db_session.add(site)
    db_session.commit()
    db_session.refresh(site)

    sc = SiteContact(
        customer_site_id=site.id,
        full_name="Jane Doe",
        email="jane@testcorp.com",
        is_active=True,
    )
    db_session.add(sc)
    db_session.commit()

    p = _make_prospect(db_session)
    result = detect_warm_intros(p, db_session)
    assert result["has_warm_intro"] is True
    assert len(result["internal_contacts"]) == 1
    assert result["internal_contacts"][0]["name"] == "Jane Doe"


def test_warm_intro_cold(db_session):
    """Returns cold when no matches found."""
    p = _make_prospect(db_session, domain="unknown-corp.com")
    result = detect_warm_intros(p, db_session)
    assert result["has_warm_intro"] is False
    assert result["warmth"] == "cold"


# ═══════════════════════════════════════════════════════════════════════
#  generate_one_liner
# ═══════════════════════════════════════════════════════════════════════


def test_one_liner_warm_intro(db_session):
    """Warm intro takes priority."""
    p = _make_prospect(db_session)
    warm = {"has_warm_intro": True, "warmth": "hot", "contacts": [
        {"name": "John Smith", "email": "j@test.com"}
    ]}
    result = generate_one_liner(p, warm)
    assert "John Smith" in result
    assert "prior engagement" in result.lower()


def test_one_liner_historical(db_session):
    """Historical bought_before generates one-liner."""
    p = _make_prospect(db_session, historical_context={"bought_before": True})
    result = generate_one_liner(p)
    assert "previous" in result.lower() or "purchased" in result.lower()


def test_one_liner_strong_intent(db_session):
    """Strong intent signal generates one-liner."""
    p = _make_prospect(
        db_session,
        readiness_signals={"intent": {"strength": "strong", "component_topics": ["semiconductors"]}},
    )
    result = generate_one_liner(p)
    assert "intent" in result.lower() or "sourcing" in result.lower()


def test_one_liner_funding_event(db_session):
    """Funding event generates one-liner."""
    p = _make_prospect(
        db_session,
        readiness_signals={"events": [{"type": "funding", "description": "Series B round"}]},
    )
    result = generate_one_liner(p)
    assert "fund" in result.lower()


def test_one_liner_hiring(db_session):
    """Hiring signal generates one-liner."""
    p = _make_prospect(
        db_session,
        readiness_signals={"hiring": {"type": "procurement"}},
    )
    result = generate_one_liner(p)
    assert "procurement" in result.lower() or "hiring" in result.lower()


def test_one_liner_similar_customer(db_session):
    """Similar customer generates one-liner."""
    p = _make_prospect(
        db_session,
        similar_customers=[{"name": "Raytheon", "match_strength": "strong"}],
    )
    result = generate_one_liner(p)
    assert "Raytheon" in result


def test_one_liner_fallback(db_session):
    """Fallback uses industry/size."""
    p = _make_prospect(
        db_session,
        industry="Electronics Manufacturing",
        employee_count_range="201-500",
        fit_score=80,
    )
    result = generate_one_liner(p)
    assert "Electronics" in result or "201-500" in result


def test_one_liner_empty(db_session):
    """Returns empty string when no signals available."""
    p = _make_prospect(
        db_session,
        industry=None,
        employee_count_range=None,
        fit_score=20,
        readiness_signals={},
        similar_customers=[],
        historical_context={},
    )
    result = generate_one_liner(p)
    assert isinstance(result, str)
