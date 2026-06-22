"""Tests for provenance-aware apply_enrichment_to_company / apply_enrichment_to_vendor.

Covers:
  (a) writes new fields including ticker/naics/revenue_range
  (b) higher-tier provenance overwrites lower-tier provenanced value
  (c) never clobbers an unprovenanced (manual/legacy) existing value
"""

from types import SimpleNamespace

from app.enrichment_service import apply_enrichment_to_company, apply_enrichment_to_vendor


def _co(**kw):
    base = dict(
        domain=None,
        linkedin_url=None,
        legal_name=None,
        industry=None,
        employee_size=None,
        hq_city=None,
        hq_state=None,
        hq_country=None,
        website=None,
        ticker=None,
        naics=None,
        revenue_range=None,
        enrichment_provenance={},
        last_enriched_at=None,
        enrichment_source=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _vc(**kw):
    """Stand-in for VendorCard — same field set as Company for apply purposes."""
    return _co(**kw)


# ── (a) Writes new fields including ticker/naics/revenue_range ────────────


def test_writes_new_firmographic_fields_incl_ticker():
    c = _co()
    data = {
        "industry": "Electronics",
        "ticker": "ARW",
        "source": "explorium",
        "_provenance": {
            "industry": {"source": "explorium", "tier": 85, "confidence": 1.0},
            "ticker": {"source": "explorium", "tier": 90, "confidence": 1.0},
        },
    }
    updated = apply_enrichment_to_company(c, data)
    assert c.ticker == "ARW"
    assert c.industry == "Electronics"
    assert "ticker" in updated
    assert "industry" in updated


def test_writes_naics_and_revenue_range():
    c = _co()
    data = {
        "naics": "5065",
        "revenue_range": "$100M-$500M",
        "source": "explorium",
        "_provenance": {
            "naics": {"source": "explorium", "tier": 80, "confidence": 1.0},
            "revenue_range": {"source": "explorium", "tier": 80, "confidence": 1.0},
        },
    }
    updated = apply_enrichment_to_company(c, data)
    assert c.naics == "5065"
    assert c.revenue_range == "$100M-$500M"
    assert "naics" in updated
    assert "revenue_range" in updated


def test_writes_field_without_provenance_when_empty():
    """Fields with no incoming provenance can still be written when the object is
    empty."""
    c = _co()
    data = {"domain": "arrow.com", "source": "manual"}
    updated = apply_enrichment_to_company(c, data)
    assert c.domain == "arrow.com"
    assert "domain" in updated


# ── (b) Higher-tier overwrites lower-tier provenanced value ──────────────


def test_higher_tier_overwrites_lower_tier():
    c = _co(
        industry="Wholesale",
        enrichment_provenance={"industry": {"source": "apollo", "tier": 70, "confidence": 1.0}},
    )
    data = {
        "industry": "Electronics Distribution",
        "_provenance": {
            "industry": {"source": "explorium", "tier": 85, "confidence": 1.0},
        },
    }
    apply_enrichment_to_company(c, data)
    assert c.industry == "Electronics Distribution"


def test_same_tier_does_not_overwrite():
    """Equal tier: no overwrite (strictly greater required)."""
    c = _co(
        industry="Wholesale",
        enrichment_provenance={"industry": {"source": "apollo", "tier": 70, "confidence": 1.0}},
    )
    data = {
        "industry": "Electronics Distribution",
        "_provenance": {
            "industry": {"source": "explorium", "tier": 70, "confidence": 1.0},
        },
    }
    apply_enrichment_to_company(c, data)
    assert c.industry == "Wholesale"


def test_lower_tier_does_not_overwrite():
    c = _co(
        industry="Wholesale",
        enrichment_provenance={"industry": {"source": "explorium", "tier": 85, "confidence": 1.0}},
    )
    data = {
        "industry": "AI Guess",
        "_provenance": {
            "industry": {"source": "ai", "tier": 30, "confidence": 0.8},
        },
    }
    apply_enrichment_to_company(c, data)
    assert c.industry == "Wholesale"


def test_higher_tier_updates_stored_provenance():
    """After overwrite, enrichment_provenance[field] reflects the new source/tier."""
    c = _co(
        industry="Wholesale",
        enrichment_provenance={"industry": {"source": "apollo", "tier": 70, "confidence": 1.0}},
    )
    data = {
        "industry": "Electronics Distribution",
        "source": "explorium",
        "_provenance": {
            "industry": {"source": "explorium", "tier": 85, "confidence": 1.0},
        },
    }
    apply_enrichment_to_company(c, data)
    assert c.enrichment_provenance["industry"]["tier"] == 85
    assert c.enrichment_provenance["industry"]["source"] == "explorium"


# ── (c) Never clobbers an unprovenanced (manual/legacy) existing value ────


def test_never_clobbers_unprovenanced_value():
    """An existing value with no stored provenance is treated as manual — protect it."""
    c = _co(industry="Hand-typed", enrichment_provenance={})
    data = {
        "industry": "AI Guess",
        "_provenance": {"industry": {"source": "ai", "tier": 30, "confidence": 1.0}},
    }
    apply_enrichment_to_company(c, data)
    assert c.industry == "Hand-typed"


def test_never_clobbers_unprovenanced_even_with_high_tier():
    """Even a high-tier source cannot overwrite a value that has no stored
    provenance."""
    c = _co(domain="manual.example.com", enrichment_provenance={})
    data = {
        "domain": "explorium.example.com",
        "_provenance": {"domain": {"source": "explorium", "tier": 99, "confidence": 1.0}},
    }
    apply_enrichment_to_company(c, data)
    assert c.domain == "manual.example.com"


def test_no_incoming_provenance_never_clobbers_existing():
    """No _provenance key at all → must not overwrite any existing value."""
    c = _co(industry="Legacy")
    data = {"industry": "New Value", "source": "import"}
    apply_enrichment_to_company(c, data)
    assert c.industry == "Legacy"


# ── VendorCard path ───────────────────────────────────────────────────────


def test_vendor_card_writes_ticker():
    v = _vc()
    data = {
        "ticker": "TEL",
        "source": "explorium",
        "_provenance": {"ticker": {"source": "explorium", "tier": 85, "confidence": 1.0}},
    }
    updated = apply_enrichment_to_vendor(v, data)
    assert v.ticker == "TEL"
    assert "ticker" in updated


def test_vendor_card_protects_manual_value():
    v = _vc(industry="Distributor", enrichment_provenance={})
    data = {
        "industry": "Electronics",
        "_provenance": {"industry": {"source": "explorium", "tier": 85, "confidence": 1.0}},
    }
    apply_enrichment_to_vendor(v, data)
    assert v.industry == "Distributor"


# ── last_enriched_at / enrichment_source written on any update ────────────


def test_metadata_written_when_updated():
    c = _co()
    data = {
        "industry": "Electronics",
        "source": "explorium",
        "_provenance": {"industry": {"source": "explorium", "tier": 85, "confidence": 1.0}},
    }
    apply_enrichment_to_company(c, data)
    assert c.last_enriched_at is not None
    assert c.enrichment_source == "explorium"


def test_metadata_not_written_when_nothing_updated():
    """If all fields are blocked (protected), metadata stays None."""
    c = _co(industry="Hand-typed", enrichment_provenance={})
    data = {
        "industry": "AI Guess",
        "source": "explorium",
        "_provenance": {"industry": {"source": "explorium", "tier": 85, "confidence": 1.0}},
    }
    apply_enrichment_to_company(c, data)
    assert c.last_enriched_at is None
    assert c.enrichment_source is None
