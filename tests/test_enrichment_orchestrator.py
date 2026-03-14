"""Tests for On-Demand Enrichment Orchestrator.

Validates parallel source firing, Claude merge logic, confidence-based
field application, and the end-to-end enrich_on_demand pipeline.

Called by: pytest
Depends on: app.services.enrichment_orchestrator, conftest fixtures
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.enrichment_orchestrator import (
    apply_confident_data,
    claude_merge,
    enrich_on_demand,
    fire_all_sources,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_entity(**kwargs):
    """Create a mock entity with settable attributes."""
    entity = MagicMock()
    # Set up attribute access — hasattr returns True for given fields
    attrs = {
        "legal_name": None,
        "domain": None,
        "industry": None,
        "employee_size": None,
        "hq_city": None,
        "hq_state": None,
        "hq_country": None,
        "website": None,
        "linkedin_url": None,
        "phone": None,
        "last_enriched_at": None,
        "enrichment_source": None,
    }
    attrs.update(kwargs)
    for k, v in attrs.items():
        setattr(entity, k, v)

    # Make hasattr work correctly for known fields only
    real_fields = set(attrs.keys())

    def _has(name):
        return name in real_fields

    entity.__class__ = type("MockEntity", (), {k: None for k in real_fields})
    for k, v in attrs.items():
        setattr(entity, k, v)

    return entity


# ---------------------------------------------------------------------------
# test_fire_all_sources_company
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fire_all_sources_company():
    """All 4 company sources should be called in parallel."""
    mock_apollo = AsyncMock(return_value={"legal_name": "Acme Corp", "source": "apollo"})
    mock_clearbit = AsyncMock(return_value={"legal_name": "Acme Corporation", "source": "clearbit"})
    mock_gradient = AsyncMock(return_value={"industry": "Electronics", "source": "gradient"})
    mock_explorium = AsyncMock(return_value={"hq_city": "Austin", "source": "explorium"})

    patched_funcs = {
        "_safe_apollo_company": mock_apollo,
        "_safe_clearbit": mock_clearbit,
        "_safe_gradient": mock_gradient,
        "_safe_explorium": mock_explorium,
    }
    with patch.dict("app.services.enrichment_orchestrator._SOURCE_FUNCS", patched_funcs):
        results = await fire_all_sources("company", "acme.com")

    assert "apollo" in results
    assert "clearbit" in results
    assert "gradient" in results
    assert "explorium" in results
    assert results["apollo"]["legal_name"] == "Acme Corp"
    assert results["clearbit"]["legal_name"] == "Acme Corporation"
    mock_apollo.assert_called_once_with("acme.com")
    mock_clearbit.assert_called_once_with("acme.com")
    mock_gradient.assert_called_once_with("acme.com")
    mock_explorium.assert_called_once_with("acme.com")


# ---------------------------------------------------------------------------
# test_fire_all_sources_contact
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fire_all_sources_contact():
    """All 4 contact sources should be called in parallel."""
    mock_apollo = AsyncMock(return_value={"full_name": "Jane Doe", "source": "apollo"})
    mock_lusha = AsyncMock(return_value={"phone": "+1-555-0100", "source": "lusha"})
    mock_hunter = AsyncMock(return_value={"email": "jane@acme.com", "source": "hunter"})
    mock_rocketreach = AsyncMock(return_value={"title": "VP Sales", "source": "rocketreach"})

    patched_funcs = {
        "_safe_apollo_contacts": mock_apollo,
        "_safe_lusha": mock_lusha,
        "_safe_hunter": mock_hunter,
        "_safe_rocketreach": mock_rocketreach,
    }
    with patch.dict("app.services.enrichment_orchestrator._SOURCE_FUNCS", patched_funcs):
        results = await fire_all_sources("contact", "jane@acme.com")

    assert "apollo" in results
    assert "lusha" in results
    assert "hunter" in results
    assert "rocketreach" in results
    assert results["apollo"]["full_name"] == "Jane Doe"
    assert results["lusha"]["phone"] == "+1-555-0100"
    mock_apollo.assert_called_once_with("jane@acme.com")
    mock_lusha.assert_called_once_with("jane@acme.com")


# ---------------------------------------------------------------------------
# test_partial_failure_handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_partial_failure_handling():
    """One source raising an exception should not crash others."""
    mock_apollo = AsyncMock(return_value={"legal_name": "Acme Corp"})
    mock_clearbit = AsyncMock(side_effect=RuntimeError("API key expired"))
    mock_gradient = AsyncMock(return_value={"industry": "Electronics"})
    mock_explorium = AsyncMock(return_value=None)

    patched_funcs = {
        "_safe_apollo_company": mock_apollo,
        "_safe_clearbit": mock_clearbit,
        "_safe_gradient": mock_gradient,
        "_safe_explorium": mock_explorium,
    }
    with patch.dict("app.services.enrichment_orchestrator._SOURCE_FUNCS", patched_funcs):
        results = await fire_all_sources("company", "acme.com")

    # Apollo and gradient succeeded
    assert results["apollo"] == {"legal_name": "Acme Corp"}
    assert results["gradient"] == {"industry": "Electronics"}
    # Clearbit raised — should be None
    assert results["clearbit"] is None
    # Explorium returned None explicitly
    assert results["explorium"] is None


# ---------------------------------------------------------------------------
# test_claude_merge_picks_best
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claude_merge_picks_best():
    """Claude should pick best field values from multiple sources."""
    raw_results = {
        "apollo": {"phone": "+1-555-0100", "legal_name": "Acme Corp"},
        "clearbit": {"phone": "+1-555-0200", "legal_name": "Acme Corporation Inc."},
        "gradient": {"phone": "+1-555-0100", "industry": "Electronics"},
    }

    claude_response = [
        {
            "field": "phone",
            "value": "+1-555-0100",
            "confidence": 0.95,
            "source": "apollo",
            "reasoning": "Two sources agree on this number",
        },
        {
            "field": "legal_name",
            "value": "Acme Corporation Inc.",
            "confidence": 0.90,
            "source": "clearbit",
            "reasoning": "Clearbit typically has accurate legal names",
        },
        {
            "field": "industry",
            "value": "Electronics",
            "confidence": 0.85,
            "source": "gradient",
            "reasoning": "Only source with industry data",
        },
    ]

    with patch("app.services.enrichment_orchestrator.claude_json", new_callable=AsyncMock) as mock_claude:
        mock_claude.return_value = claude_response
        merged = await claude_merge(raw_results, "company")

    assert len(merged) == 3
    phone_entry = next(e for e in merged if e["field"] == "phone")
    assert phone_entry["value"] == "+1-555-0100"
    assert phone_entry["confidence"] == 0.95
    assert phone_entry["source"] == "apollo"

    name_entry = next(e for e in merged if e["field"] == "legal_name")
    assert name_entry["value"] == "Acme Corporation Inc."
    assert name_entry["confidence"] == 0.90

    mock_claude.assert_called_once()


# ---------------------------------------------------------------------------
# test_apply_confident_above_threshold
# ---------------------------------------------------------------------------


def test_apply_confident_above_threshold():
    """Fields with confidence >= 0.90 should be applied to the entity."""
    entity = _make_entity()
    db = MagicMock()

    merged = [
        {
            "field": "phone",
            "value": "+1-555-0100",
            "confidence": 0.95,
            "source": "apollo",
            "reasoning": "High confidence",
        },
        {
            "field": "legal_name",
            "value": "Acme Corp",
            "confidence": 0.92,
            "source": "clearbit",
            "reasoning": "Verified name",
        },
    ]

    result = apply_confident_data(entity, merged, db, threshold=0.90)

    assert len(result["applied"]) == 2
    assert len(result["rejected"]) == 0
    assert entity.phone == "+1-555-0100"
    assert entity.legal_name == "Acme Corp"
    assert "apollo" in result["sources_used"]
    assert "clearbit" in result["sources_used"]
    db.commit.assert_called_once()


# ---------------------------------------------------------------------------
# test_apply_confident_below_threshold
# ---------------------------------------------------------------------------


def test_apply_confident_below_threshold():
    """Fields with confidence < 0.90 should be rejected."""
    entity = _make_entity()
    db = MagicMock()

    merged = [
        {
            "field": "phone",
            "value": "+1-555-0100",
            "confidence": 0.95,
            "source": "apollo",
            "reasoning": "High confidence",
        },
        {
            "field": "industry",
            "value": "Maybe Electronics",
            "confidence": 0.60,
            "source": "gradient",
            "reasoning": "Low confidence guess",
        },
    ]

    result = apply_confident_data(entity, merged, db, threshold=0.90)

    assert len(result["applied"]) == 1
    assert len(result["rejected"]) == 1
    assert entity.phone == "+1-555-0100"
    # Industry should NOT have been set (confidence too low)
    rejected = result["rejected"][0]
    assert rejected["field"] == "industry"
    assert rejected["confidence"] == 0.60
    assert "Below threshold" in rejected["reason"]


# ---------------------------------------------------------------------------
# test_enrich_on_demand_end_to_end
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enrich_on_demand_end_to_end():
    """Full pipeline: load entity -> fire sources -> merge -> apply -> summary."""
    mock_entity = _make_entity(domain="acme.com", legal_name=None)
    mock_db = MagicMock()

    source_results = {
        "apollo": {"legal_name": "Acme Corp", "industry": "Electronics"},
        "clearbit": {"legal_name": "Acme Corporation"},
        "gradient": None,
        "explorium": None,
    }

    merge_result = [
        {
            "field": "legal_name",
            "value": "Acme Corporation",
            "confidence": 0.95,
            "source": "clearbit",
            "reasoning": "More complete name",
        },
        {
            "field": "industry",
            "value": "Electronics",
            "confidence": 0.91,
            "source": "apollo",
            "reasoning": "Only source with this field",
        },
    ]

    with (
        patch("app.services.enrichment_orchestrator._load_entity", return_value=mock_entity),
        patch("app.services.enrichment_orchestrator._get_identifier", return_value="acme.com"),
        patch(
            "app.services.enrichment_orchestrator.fire_all_sources",
            new_callable=AsyncMock,
            return_value=source_results,
        ),
        patch(
            "app.services.enrichment_orchestrator.claude_merge",
            new_callable=AsyncMock,
            return_value=merge_result,
        ),
    ):
        result = await enrich_on_demand("company", 42, mock_db)

    assert result["entity_type"] == "company"
    assert result["entity_id"] == 42
    assert result["identifier"] == "acme.com"
    assert "apollo" in result["sources_fired"]
    assert "clearbit" in result["sources_fired"]
    assert len(result["applied"]) == 2
    assert mock_entity.legal_name == "Acme Corporation"
    assert mock_entity.industry == "Electronics"
