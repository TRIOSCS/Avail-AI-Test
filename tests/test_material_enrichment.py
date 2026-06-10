"""Tests for material enrichment service — lifecycle status detection.

Tests the _apply_enrichment_result function which applies AI enrichment
results (description, category, lifecycle) to MaterialCard objects.

Depends on: app.services.material_enrichment_service, app.models.MaterialCard
"""

from app.models import MaterialCard


def test_enrichment_sets_lifecycle_status(db_session):
    """Enrichment should set lifecycle_status from AI response."""
    card = MaterialCard(normalized_mpn="test123", display_mpn="TEST123")
    db_session.add(card)
    db_session.flush()

    from app.services.material_enrichment_service import _apply_enrichment_result

    ai_result = {
        "mpn": "TEST123",
        "description": "Test component",
        "category": "resistors",
        "lifecycle_status": "active",
    }
    _apply_enrichment_result(card, ai_result)

    assert card.lifecycle_status == "active"
    assert card.category == "resistors"
    assert card.description == "Test component"


def test_enrichment_defaults_lifecycle_when_missing(db_session):
    """If AI response lacks lifecycle_status, default to 'active'."""
    card = MaterialCard(normalized_mpn="test456", display_mpn="TEST456")
    db_session.add(card)
    db_session.flush()

    from app.services.material_enrichment_service import _apply_enrichment_result

    ai_result = {
        "mpn": "TEST456",
        "description": "Another component",
        "category": "capacitors",
    }
    _apply_enrichment_result(card, ai_result)

    assert card.lifecycle_status == "active"


def test_enrichment_rejects_invalid_lifecycle(db_session):
    """Invalid lifecycle values should default to 'active'."""
    card = MaterialCard(normalized_mpn="test789", display_mpn="TEST789")
    db_session.add(card)
    db_session.flush()

    from app.services.material_enrichment_service import _apply_enrichment_result

    ai_result = {
        "mpn": "TEST789",
        "description": "Component",
        "category": "diodes",
        "lifecycle_status": "invalid_value",
    }
    _apply_enrichment_result(card, ai_result)

    assert card.lifecycle_status == "active"


def test_enrichment_category_routes_through_ladder(db_session):
    """Haiku categorization goes through spec_tiers.set_category: it stamps provenance
    on a fill and can never overwrite a higher-tier (decode/vendor/TRIO) category."""
    from app.services.material_enrichment_service import _apply_enrichment_result

    fresh = MaterialCard(normalized_mpn="ladder1", display_mpn="LADDER1")
    decoded = MaterialCard(
        normalized_mpn="ladder2",
        display_mpn="LADDER2",
        category="dram",
        category_source="mpn_decode",
        category_confidence=0.95,
        category_tier=85,
    )
    db_session.add_all([fresh, decoded])
    db_session.flush()

    ai_result = {"mpn": "X", "description": "d", "category": "resistors", "lifecycle_status": "active"}
    _apply_enrichment_result(fresh, ai_result)
    _apply_enrichment_result(decoded, ai_result)

    assert fresh.category == "resistors"  # fill wins (existing None)
    assert fresh.category_source == "claude_haiku"
    assert fresh.category_tier == 40
    assert decoded.category == "dram"  # tier 40 cannot flip tier 85
    assert decoded.category_source == "mpn_decode"
