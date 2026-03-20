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
