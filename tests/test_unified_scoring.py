"""Tests for unified confidence scoring (score_unified, confidence_color).

Covers all four source types (live API, historical, vendor affinity, AI
research), badge assignment, and color thresholds.

Called by: pytest
Depends on: app.scoring
"""

from app.scoring import confidence_color, score_unified


# -- confidence_color ------------------------------------------------------

def test_confidence_color_green():
    assert confidence_color(75) == "green"
    assert confidence_color(100) == "green"
    assert confidence_color(90) == "green"


def test_confidence_color_amber():
    assert confidence_color(50) == "amber"
    assert confidence_color(74) == "amber"
    assert confidence_color(60) == "amber"


def test_confidence_color_red():
    assert confidence_color(49) == "red"
    assert confidence_color(0) == "red"
    assert confidence_color(10) == "red"


# -- Live API scoring ------------------------------------------------------

def test_live_api_authorized_high_score():
    """Authorized distributor with price + qty should score 90-95%."""
    result = score_unified(
        source_type="nexar",
        vendor_score=90.0,
        is_authorized=True,
        unit_price=1.50,
        median_price=1.60,
        qty_available=10000,
        target_qty=5000,
        age_hours=1.0,
        has_price=True,
        has_qty=True,
        has_lead_time=True,
        has_condition=True,
    )
    assert 90 <= result["confidence_pct"] <= 95
    assert result["source_badge"] == "Live Stock"
    assert result["confidence_color"] == "green"


def test_live_api_unknown_low_score():
    """Unknown vendor with no price info should score 70-75%."""
    result = score_unified(
        source_type="brokerbin",
        vendor_score=None,
        is_authorized=False,
        has_price=False,
        has_qty=False,
    )
    assert 70 <= result["confidence_pct"] <= 78
    assert result["source_badge"] == "Live Stock"


# -- Historical scoring ----------------------------------------------------

def test_historical_recent():
    """1 month old sighting should score around 75%."""
    result = score_unified(
        source_type="historical",
        age_hours=720.0,  # 1 month
    )
    # base 80 - 5 = 75
    assert result["confidence_pct"] == 75
    assert result["source_badge"] == "Historical"
    assert result["confidence_color"] == "green"


def test_historical_stale():
    """6 month old sighting should score around 50%."""
    result = score_unified(
        source_type="historical",
        age_hours=720.0 * 6,  # 6 months
    )
    # base 80 - 30 = 50
    assert result["confidence_pct"] == 50
    assert result["source_badge"] == "Historical"
    assert result["confidence_color"] == "amber"


def test_historical_repeated_boost():
    """Repeat sightings should boost score by +2% each, max +10%."""
    base_result = score_unified(
        source_type="historical",
        age_hours=720.0 * 2,  # 2 months old → base 80-10 = 70
    )
    boosted_result = score_unified(
        source_type="historical",
        age_hours=720.0 * 2,
        repeat_sighting_count=3,  # +6%
    )
    assert boosted_result["confidence_pct"] == base_result["confidence_pct"] + 6

    # Max boost capped at +10
    max_boost = score_unified(
        source_type="historical",
        age_hours=720.0 * 2,
        repeat_sighting_count=10,  # would be +20 but capped at +10
    )
    assert max_boost["confidence_pct"] == base_result["confidence_pct"] + 10


# -- Vendor affinity scoring -----------------------------------------------

def test_vendor_affinity_passthrough():
    """Vendor affinity uses claude_confidence directly."""
    result = score_unified(
        source_type="vendor_affinity",
        claude_confidence=0.65,
    )
    assert result["confidence_pct"] == 65
    assert result["source_badge"] == "Vendor Match"
    assert result["confidence_color"] == "amber"


def test_vendor_affinity_high():
    result = score_unified(
        source_type="vendor_affinity",
        claude_confidence=0.85,
    )
    assert result["confidence_pct"] == 85
    assert result["confidence_color"] == "green"


# -- AI research scoring ---------------------------------------------------

def test_ai_research_capped():
    """AI research confidence is capped at 60%."""
    result = score_unified(
        source_type="ai_live_web",
        claude_confidence=0.95,
    )
    assert result["confidence_pct"] == 60
    assert result["source_badge"] == "AI Found"
    assert result["confidence_color"] == "amber"


def test_ai_research_low():
    result = score_unified(
        source_type="ai_live_web",
        claude_confidence=0.30,
    )
    assert result["confidence_pct"] == 30
    assert result["confidence_color"] == "red"


# -- Source badges ----------------------------------------------------------

def test_source_badges():
    """Each source type gets the correct badge."""
    live = score_unified(source_type="nexar")
    assert live["source_badge"] == "Live Stock"

    hist = score_unified(source_type="historical")
    assert hist["source_badge"] == "Historical"

    affinity = score_unified(source_type="vendor_affinity", claude_confidence=0.5)
    assert affinity["source_badge"] == "Vendor Match"

    ai = score_unified(source_type="ai_live_web", claude_confidence=0.4)
    assert ai["source_badge"] == "AI Found"
