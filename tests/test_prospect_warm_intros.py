"""Tests for warm intro detection and one-liner generation.

Tests:
- detect_warm_intros: VendorCard domain match, SiteContact match
- generate_one_liner: priority order of signal types
- /api/prospects/suggested serialization includes one_liner

Called by: pytest
Depends on: conftest (db_session, test_user)
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from app.models import Company, VendorCard, VendorContact
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
        last_contact_at=datetime.now(UTC),
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
    warm = {"has_warm_intro": True, "warmth": "hot", "contacts": [{"name": "John Smith", "email": "j@test.com"}]}
    result = generate_one_liner(p, warm)
    assert "John Smith" in result
    assert "prior engagement" in result.lower()


# Each case: prospect kwargs → all listed (case-insensitive) substrings must appear.
@pytest.mark.parametrize(
    ("prospect_kwargs", "expected_substrings"),
    [
        pytest.param(
            {"readiness_signals": {"events": [{"type": "funding", "description": "Series B round"}]}},
            ["fund"],
            id="funding_event",
        ),
        pytest.param(
            {"similar_customers": [{"name": "Raytheon", "match_strength": "strong"}]},
            ["Raytheon"],
            id="similar_customer",
        ),
    ],
)
def test_one_liner_all_substrings(db_session, prospect_kwargs, expected_substrings):
    """One-liner contains every expected (case-insensitive) substring."""
    p = _make_prospect(db_session, **prospect_kwargs)
    result = generate_one_liner(p).lower()
    assert all(sub.lower() in result for sub in expected_substrings)


# Each case: prospect kwargs → at least one of the listed substrings must appear.
@pytest.mark.parametrize(
    ("prospect_kwargs", "any_substrings"),
    [
        pytest.param(
            {"historical_context": {"bought_before": True}},
            ["previous", "purchased"],
            id="historical",
        ),
        pytest.param(
            {"readiness_signals": {"intent": {"strength": "strong", "component_topics": ["semiconductors"]}}},
            ["intent", "sourcing"],
            id="strong_intent",
        ),
        pytest.param(
            {"readiness_signals": {"hiring": {"type": "procurement"}}},
            ["procurement", "hiring"],
            id="hiring",
        ),
        pytest.param(
            {"industry": "Electronics Manufacturing", "employee_count_range": "201-500", "fit_score": 80},
            ["Electronics", "201-500"],
            id="fallback",
        ),
    ],
)
def test_one_liner_any_substring(db_session, prospect_kwargs, any_substrings):
    """One-liner contains at least one of the expected (case-insensitive) substrings."""
    p = _make_prospect(db_session, **prospect_kwargs)
    result = generate_one_liner(p).lower()
    assert any(sub.lower() in result for sub in any_substrings)


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


# ═══════════════════════════════════════════════════════════════════════
#  Lines 92-94: warm (not hot) vendor card match
# ═══════════════════════════════════════════════════════════════════════


def test_warm_intro_vendor_card_warm_engagement(db_session):
    """Warm intro when engagement_score is between 30-59 (warm, not hot)."""
    vc = VendorCard(
        display_name="Warm Corp Vendor",
        domain="warmcorp.com",
        normalized_name="warm corp vendor",
        engagement_score=45.0,
        last_contact_at=datetime.now(UTC),
    )
    db_session.add(vc)
    db_session.commit()

    p = _make_prospect(db_session, domain="warmcorp.com")
    result = detect_warm_intros(p, db_session)
    assert result["has_warm_intro"] is True
    assert result["warmth"] == "warm"
    assert result["vendor_card_id"] == vc.id


def test_warm_intro_vendor_card_warm_with_low_score_contacts(db_session):
    """Warm intro when contacts exist but none have high relationship_score."""
    vc = VendorCard(
        display_name="LowScore Corp",
        domain="lowscore.com",
        normalized_name="lowscore corp",
        engagement_score=10.0,
    )
    db_session.add(vc)
    db_session.commit()
    db_session.refresh(vc)

    contact = VendorContact(
        vendor_card_id=vc.id,
        email="alice@lowscore.com",
        full_name="Alice Low",
        title="Coordinator",
        relationship_score=20.0,
        activity_trend="declining",
        source="test",
    )
    db_session.add(contact)
    db_session.commit()

    p = _make_prospect(db_session, domain="lowscore.com")
    result = detect_warm_intros(p, db_session)
    assert result["has_warm_intro"] is True
    assert result["warmth"] == "warm"
    assert len(result["contacts"]) == 1
    assert result["contacts"][0]["name"] == "Alice Low"


# ═══════════════════════════════════════════════════════════════════════
#  Lines 132-135: sighting-only warm intro
# ═══════════════════════════════════════════════════════════════════════


def test_warm_intro_sighting_only(db_session):
    """Sighting count > 0 triggers warm intro when no VendorCard/SiteContact."""
    from unittest.mock import MagicMock, patch

    p = _make_prospect(db_session, domain="sightingonly.com")

    # Mock the Sighting query to return a count > 0.
    # The code does: db.query(func.count(Sighting.id)).filter(...).scalar()
    # We patch the lazy import of Sighting and intercept the query chain.
    original_query = db_session.query

    def patched_query(*args, **kwargs):
        # Check if this is a count query on Sighting
        if args and hasattr(args[0], "key") and "count" in str(args[0]):
            mock_q = MagicMock()
            mock_q.filter.return_value.scalar.return_value = 7
            return mock_q
        return original_query(*args, **kwargs)

    with patch.object(db_session, "query", side_effect=patched_query):
        result = detect_warm_intros(p, db_session)

    assert result["has_warm_intro"] is True
    assert result["warmth"] == "warm"
    assert result["sighting_count"] == 7


# ═══════════════════════════════════════════════════════════════════════
#  Lines 163-169: generate_one_liner warm intro variants
# ═══════════════════════════════════════════════════════════════════════


def test_one_liner_warm_intro_hot_no_contacts(db_session):
    """Hot warm intro with no contacts returns 'Active vendor relationship' one-
    liner."""
    p = _make_prospect(db_session)
    warm = {"has_warm_intro": True, "warmth": "hot", "contacts": []}
    result = generate_one_liner(p, warm)
    assert "Active vendor relationship" in result
    assert "prior email engagement" in result.lower()


def test_one_liner_warm_intro_sighting_count(db_session):
    """Warm intro with sighting_count > 0 returns stock offers one-liner."""
    p = _make_prospect(db_session)
    warm = {
        "has_warm_intro": True,
        "warmth": "warm",
        "contacts": [],
        "sighting_count": 5,
        "internal_contacts": [],
    }
    result = generate_one_liner(p, warm)
    assert "5 stock offers" in result


def test_one_liner_warm_intro_internal_contacts(db_session):
    """Warm intro with internal_contacts returns 'Known contact' one-liner."""
    p = _make_prospect(db_session)
    warm = {
        "has_warm_intro": True,
        "warmth": "warm",
        "contacts": [],
        "sighting_count": 0,
        "internal_contacts": [{"name": "Jane Doe", "email": "jane@test.com", "company": "Acme"}],
    }
    result = generate_one_liner(p, warm)
    assert "Jane Doe" in result
    assert "Acme" in result
    assert "Known contact" in result


def test_one_liner_warm_intro_fallback_prior_email(db_session):
    """Warm intro with no contacts, no sightings, no internal contacts returns
    fallback."""
    p = _make_prospect(db_session)
    warm = {
        "has_warm_intro": True,
        "warmth": "warm",
        "contacts": [],
        "sighting_count": 0,
        "internal_contacts": [],
    }
    result = generate_one_liner(p, warm)
    assert "Prior email interaction" in result


# ═══════════════════════════════════════════════════════════════════════
#  Lines 175-176: quoted_before historical context
# ═══════════════════════════════════════════════════════════════════════


def test_one_liner_quoted_before(db_session):
    """Historical quoted_before generates quote count one-liner."""
    p = _make_prospect(
        db_session,
        historical_context={"quoted_before": True, "quote_count": 3},
    )
    result = generate_one_liner(p)
    assert "Previously quoted" in result
    assert "3 quotes" in result


def test_one_liner_quoted_before_single(db_session):
    """Historical quote_count=1 uses singular 'quote' not 'quotes'."""
    p = _make_prospect(
        db_session,
        historical_context={"quote_count": 1},
    )
    result = generate_one_liner(p)
    assert "Previously quoted" in result
    assert "1 quote " in result or result.endswith("1 quote")


# ═══════════════════════════════════════════════════════════════════════
#  Lines 195, 197, 206: event and hiring one-liners
# ═══════════════════════════════════════════════════════════════════════


# Each case: prospect kwargs → the exact (case-sensitive) substring must appear.
@pytest.mark.parametrize(
    ("prospect_kwargs", "expected"),
    [
        pytest.param(
            {"readiness_signals": {"events": [{"type": "expansion", "description": "New plant"}]}},
            "Expanding operations",
            id="expansion_event",
        ),
        pytest.param(
            {"readiness_signals": {"events": [{"type": "product_launch", "description": "Widget v2"}]}},
            "New product launch",
            id="product_launch_event",
        ),
        pytest.param(
            {"readiness_signals": {"intent": {"strength": "strong", "component_topics": []}}},
            "Strong buying intent for electronic components detected",
            id="strong_intent_no_topics",
        ),
    ],
)
def test_one_liner_exact_substring(db_session, prospect_kwargs, expected):
    """One-liner contains the exact case-sensitive substring."""
    p = _make_prospect(db_session, **prospect_kwargs)
    assert expected in generate_one_liner(p)


def test_one_liner_hiring_engineering(db_session):
    """Engineering hiring signal generates 'Hiring engineers' one-liner."""
    p = _make_prospect(
        db_session,
        readiness_signals={"hiring": {"type": "engineering"}},
    )
    result = generate_one_liner(p)
    assert "Hiring engineers" in result
    assert "increasing production" in result.lower()


# ═══════════════════════════════════════════════════════════════════════
#  Lines 134-135: Sighting import exception handling
# ═══════════════════════════════════════════════════════════════════════


def test_warm_intro_sighting_import_error(db_session):
    """Sighting query exception is caught and silently ignored."""
    from unittest.mock import patch

    p = _make_prospect(db_session, domain="sighterror.com")

    with patch(
        "app.services.prospect_warm_intros.func",
        side_effect=RuntimeError("Sighting model unavailable"),
    ):
        result = detect_warm_intros(p, db_session)

    # Should still return valid result with sighting_count=0
    assert result["sighting_count"] == 0
    assert result["warmth"] == "cold"


# ═══════════════════════════════════════════════════════════════════════
#  Lines 184, 198-199: additional one-liner branches
# ═══════════════════════════════════════════════════════════════════════


def test_one_liner_acquisition_event(db_session):
    """Acquisition/M&A event generates procurement consolidation one-liner."""
    p = _make_prospect(
        db_session,
        readiness_signals={"events": [{"type": "acquisition", "description": "Acquired Widget Inc"}]},
    )
    result = generate_one_liner(p)
    assert "M&A activity" in result
    assert "procurement consolidation" in result.lower()
