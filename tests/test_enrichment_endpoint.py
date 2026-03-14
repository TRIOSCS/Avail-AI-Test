"""Tests for the on-demand enrichment orchestrator endpoint.

POST /api/enrich/{entity_type}/{entity_id} triggers parallel enrichment,
Claude merge, and confident field application. This file tests the endpoint
logic including validation, 404 handling, and feature flag gating.

Note: The CRM router also has /api/enrich/company/ and /api/enrich/vendor/
routes that match before the generic {entity_type} route. Tests for those
entity types call the endpoint function directly to avoid route conflicts.

Called by: pytest
Depends on: app.routers.enrichment, app.services.enrichment_orchestrator, conftest fixtures
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.routers.enrichment import api_enrich_on_demand

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _call_endpoint(entity_type, entity_id, db, user=None):
    """Call the endpoint function directly, bypassing HTTP routing."""
    if user is None:
        user = MagicMock(email="test@example.com")
    return await api_enrich_on_demand(
        entity_type=entity_type,
        entity_id=entity_id,
        user=user,
        db=db,
    )


# ---------------------------------------------------------------------------
# test_enrich_company_success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enrich_company_success(db_session, test_company):
    """Enriching a company returns 200 with applied/rejected/sources_used."""
    mock_result = {
        "entity_type": "company",
        "entity_id": test_company.id,
        "identifier": "acme-electronics.com",
        "sources_fired": ["apollo", "clearbit"],
        "sources_returned_data": ["apollo"],
        "merge_results": [
            {
                "field": "industry",
                "value": "Electronics",
                "confidence": 0.95,
                "source": "apollo",
                "reasoning": "Only source",
            }
        ],
        "applied": [
            {
                "field": "industry",
                "value": "Electronics",
                "confidence": 0.95,
                "source": "apollo",
            }
        ],
        "rejected": [],
        "sources_used": ["apollo"],
    }

    with patch(
        "app.services.enrichment_orchestrator.enrich_on_demand",
        new_callable=AsyncMock,
        return_value=mock_result,
    ):
        result = await _call_endpoint("company", test_company.id, db_session)

    assert result["entity_type"] == "company"
    assert result["entity_id"] == test_company.id
    assert "applied" in result
    assert "rejected" in result
    assert "sources_used" in result
    assert len(result["applied"]) == 1
    assert result["applied"][0]["field"] == "industry"


# ---------------------------------------------------------------------------
# test_enrich_vendor_success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enrich_vendor_success(db_session, test_vendor_card):
    """Enriching a vendor returns 200 with enrichment summary."""
    mock_result = {
        "entity_type": "vendor",
        "entity_id": test_vendor_card.id,
        "identifier": "arrow.com",
        "sources_fired": ["apollo", "clearbit"],
        "sources_returned_data": ["apollo"],
        "merge_results": [],
        "applied": [],
        "rejected": [],
        "sources_used": [],
    }

    with patch(
        "app.services.enrichment_orchestrator.enrich_on_demand",
        new_callable=AsyncMock,
        return_value=mock_result,
    ):
        result = await _call_endpoint("vendor", test_vendor_card.id, db_session)

    assert result["entity_type"] == "vendor"
    assert result["entity_id"] == test_vendor_card.id


# ---------------------------------------------------------------------------
# test_invalid_entity_type
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_entity_type(db_session):
    """Invalid entity_type raises 400."""
    with pytest.raises(HTTPException) as exc_info:
        await _call_endpoint("invalid", 123, db_session)

    assert exc_info.value.status_code == 400
    assert "Invalid entity_type" in exc_info.value.detail


# ---------------------------------------------------------------------------
# test_entity_not_found
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_entity_not_found(db_session):
    """Missing company raises 404."""
    with pytest.raises(HTTPException) as exc_info:
        await _call_endpoint("company", 999999, db_session)

    assert exc_info.value.status_code == 404
    assert "not found" in exc_info.value.detail.lower()


@pytest.mark.asyncio
async def test_vendor_not_found(db_session):
    """Missing vendor raises 404."""
    with pytest.raises(HTTPException) as exc_info:
        await _call_endpoint("vendor", 999999, db_session)

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_contact_not_found(db_session):
    """Missing contact raises 404."""
    with pytest.raises(HTTPException) as exc_info:
        await _call_endpoint("contact", 999999, db_session)

    assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# test_feature_disabled
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_feature_disabled(db_session):
    """When ON_DEMAND_ENRICHMENT_ENABLED is False, endpoint raises 503."""
    mock_settings = MagicMock()
    mock_settings.on_demand_enrichment_enabled = False

    with patch("app.routers.enrichment.settings", mock_settings):
        with pytest.raises(HTTPException) as exc_info:
            await _call_endpoint("company", 1, db_session)

    assert exc_info.value.status_code == 503
    assert "Feature disabled" in exc_info.value.detail
