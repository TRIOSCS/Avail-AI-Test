"""Tests for warm intro detection and one-liner generation.

Tests:
- detect_warm_intros: VendorCard domain match, SiteContact match
- generate_one_liner: priority order of signal types
- /api/prospects/suggested serialization includes one_liner

Called by: pytest
Depends on: conftest (db_session, test_user)
"""

from datetime import datetime, timezone

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
    warm = {"has_warm_intro": True, "warmth": "hot", "contacts": [{"name": "John Smith", "email": "j@test.com"}]}
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
        last_contact_at=datetime.now(timezone.utc),
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
    """Hot warm intro with no contacts returns 'Active vendor relationship' one-liner."""
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
    """Warm intro with no contacts, no sightings, no internal contacts returns fallback."""
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


def test_one_liner_expansion_event(db_session):
    """Expansion event generates 'Expanding operations' one-liner."""
    p = _make_prospect(
        db_session,
        readiness_signals={"events": [{"type": "expansion", "description": "New plant"}]},
    )
    result = generate_one_liner(p)
    assert "Expanding operations" in result


def test_one_liner_product_launch_event(db_session):
    """Product launch event generates 'New product launch' one-liner."""
    p = _make_prospect(
        db_session,
        readiness_signals={"events": [{"type": "product_launch", "description": "Widget v2"}]},
    )
    result = generate_one_liner(p)
    assert "New product launch" in result


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
#  Lines 236-266: enrich_warm_intros_batch()
# ═══════════════════════════════════════════════════════════════════════


def test_enrich_warm_intros_batch_basic(db_session):
    """Batch enrichment processes qualifying prospects and returns summary."""
    from app.services.prospect_warm_intros import enrich_warm_intros_batch

    # Create qualifying prospect (status=suggested, fit_score >= 40)
    p = _make_prospect(db_session, domain="batchtest.com", fit_score=80)

    result = enrich_warm_intros_batch(db_session, min_fit_score=40)
    assert result["processed"] == 1
    assert result["errors"] == 0

    # Verify enrichment_data was stored
    db_session.refresh(p)
    ed = p.enrichment_data or {}
    assert "warm_intro" in ed
    assert "one_liner" in ed


def test_enrich_warm_intros_batch_warm_found(db_session):
    """Batch enrichment increments warm_found when warm intro detected."""
    from app.services.prospect_warm_intros import enrich_warm_intros_batch

    # Create a VendorCard for the domain
    vc = VendorCard(
        display_name="Batch Warm Corp",
        domain="batchwarm.com",
        normalized_name="batch warm corp",
        engagement_score=70.0,
        last_contact_at=datetime.now(timezone.utc),
    )
    db_session.add(vc)
    db_session.commit()

    p = _make_prospect(db_session, domain="batchwarm.com", fit_score=60)

    result = enrich_warm_intros_batch(db_session, min_fit_score=40)
    assert result["processed"] == 1
    assert result["warm_found"] == 1
    assert result["errors"] == 0


def test_enrich_warm_intros_batch_skips_low_fit(db_session):
    """Batch enrichment skips prospects below min_fit_score."""
    from app.services.prospect_warm_intros import enrich_warm_intros_batch

    _make_prospect(db_session, domain="lowfit.com", fit_score=20)

    result = enrich_warm_intros_batch(db_session, min_fit_score=40)
    assert result["processed"] == 0


def test_enrich_warm_intros_batch_skips_non_suggested(db_session):
    """Batch enrichment skips prospects not in 'suggested' status."""
    from app.services.prospect_warm_intros import enrich_warm_intros_batch

    _make_prospect(db_session, domain="claimed.com", fit_score=80, status="claimed")

    result = enrich_warm_intros_batch(db_session, min_fit_score=40)
    assert result["processed"] == 0


def test_enrich_warm_intros_batch_handles_errors(db_session):
    """Batch enrichment counts errors without crashing."""
    from unittest.mock import patch

    from app.services.prospect_warm_intros import enrich_warm_intros_batch

    p = _make_prospect(db_session, domain="errortest.com", fit_score=80)

    with patch(
        "app.services.prospect_warm_intros.detect_warm_intros",
        side_effect=RuntimeError("boom"),
    ):
        result = enrich_warm_intros_batch(db_session, min_fit_score=40)

    assert result["errors"] == 1
    assert result["processed"] == 0


def test_enrich_warm_intros_batch_multiple_prospects(db_session):
    """Batch enrichment processes multiple qualifying prospects."""
    from app.services.prospect_warm_intros import enrich_warm_intros_batch

    _make_prospect(db_session, domain="multi1.com", fit_score=50, name="Multi 1")
    _make_prospect(db_session, domain="multi2.com", fit_score=90, name="Multi 2")
    _make_prospect(db_session, domain="multi3.com", fit_score=30, name="Multi 3")  # below threshold

    result = enrich_warm_intros_batch(db_session, min_fit_score=40)
    assert result["processed"] == 2
    assert result["errors"] == 0


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


def test_one_liner_strong_intent_no_topics(db_session):
    """Strong intent with no component_topics returns generic intent one-liner."""
    p = _make_prospect(
        db_session,
        readiness_signals={"intent": {"strength": "strong", "component_topics": []}},
    )
    result = generate_one_liner(p)
    assert "Strong buying intent for electronic components detected" in result


def test_one_liner_acquisition_event(db_session):
    """Acquisition/M&A event generates procurement consolidation one-liner."""
    p = _make_prospect(
        db_session,
        readiness_signals={"events": [{"type": "acquisition", "description": "Acquired Widget Inc"}]},
    )
    result = generate_one_liner(p)
    assert "M&A activity" in result
    assert "procurement consolidation" in result.lower()
