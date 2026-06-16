"""Tests for material enrichment service — lifecycle status detection.

Tests the _apply_enrichment_result function which applies AI enrichment
results (description, category, lifecycle) to MaterialCard objects.

Depends on: app.services.material_enrichment_service, app.models.MaterialCard
"""

import pytest

from app.models import MaterialCard


def _make_card(db_session, normalized_mpn, display_mpn):
    """Persist a fresh MaterialCard and return it ready for enrichment."""
    card = MaterialCard(normalized_mpn=normalized_mpn, display_mpn=display_mpn)
    db_session.add(card)
    db_session.flush()
    return card


@pytest.mark.parametrize(
    "ai_result",
    [
        pytest.param(
            {
                "mpn": "TEST123",
                "description": "Test component",
                "category": "resistors",
                "lifecycle_status": "active",
            },
            id="explicit_active",
        ),
        pytest.param(
            {
                "mpn": "TEST456",
                "description": "Another component",
                "category": "capacitors",
            },
            id="missing_defaults_active",
        ),
        pytest.param(
            {
                "mpn": "TEST789",
                "description": "Component",
                "category": "diodes",
                "lifecycle_status": "invalid_value",
            },
            id="invalid_defaults_active",
        ),
    ],
)
def test_enrichment_lifecycle_status(db_session, ai_result):
    """lifecycle_status resolves to a valid value: explicit valid value is kept,
    while missing or invalid values default to 'active'."""
    card = _make_card(db_session, ai_result["mpn"].lower(), ai_result["mpn"])

    from app.services.material_enrichment_service import _apply_enrichment_result

    _apply_enrichment_result(card, ai_result)

    assert card.lifecycle_status == "active"
    if "category" in ai_result:
        assert card.category == ai_result["category"]
    if "description" in ai_result:
        assert card.description == ai_result["description"]


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
