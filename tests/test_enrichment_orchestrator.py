"""Tests for On-Demand Enrichment Orchestrator.

Validates parallel source firing, Claude merge logic, confidence-based
field application, and the end-to-end enrich_on_demand pipeline.

Called by: pytest
Depends on: app.services.enrichment_orchestrator, conftest fixtures
"""

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
    """All 3 company sources should be called in parallel."""
    mock_apollo = AsyncMock(return_value={"legal_name": "Acme Corp", "source": "apollo"})
    mock_clearbit = AsyncMock(return_value={"legal_name": "Acme Corporation", "source": "clearbit"})
    mock_explorium = AsyncMock(return_value={"hq_city": "Austin", "source": "explorium"})

    patched_funcs = {
        "_safe_apollo_company": mock_apollo,
        "_safe_clearbit": mock_clearbit,
        "_safe_explorium": mock_explorium,
    }
    with patch.dict("app.services.enrichment_orchestrator._SOURCE_FUNCS", patched_funcs):
        results = await fire_all_sources("company", "acme.com")

    assert "apollo" in results
    assert "clearbit" in results
    assert "explorium" in results
    assert results["apollo"]["legal_name"] == "Acme Corp"
    assert results["clearbit"]["legal_name"] == "Acme Corporation"
    mock_apollo.assert_called_once_with("acme.com")
    mock_clearbit.assert_called_once_with("acme.com")
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
    mock_explorium = AsyncMock(return_value=None)

    patched_funcs = {
        "_safe_apollo_company": mock_apollo,
        "_safe_clearbit": mock_clearbit,
        "_safe_explorium": mock_explorium,
    }
    with patch.dict("app.services.enrichment_orchestrator._SOURCE_FUNCS", patched_funcs):
        results = await fire_all_sources("company", "acme.com")

    # Apollo succeeded
    assert results["apollo"] == {"legal_name": "Acme Corp"}
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
        "apollo": {"phone": "+1-555-0100", "legal_name": "Acme Corp", "industry": "Electronics"},
        "clearbit": {"phone": "+1-555-0200", "legal_name": "Acme Corporation Inc."},
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
            "source": "apollo",
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
            "source": "apollo",
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


class TestApplyConfidentBoundaries:
    """Edge cases for the 0.90 confidence threshold gate."""

    def _make_entity(self):
        entity = MagicMock()
        for attr in ("legal_name", "domain", "industry", "last_enriched_at", "enrichment_source"):
            setattr(entity, attr, None)
        entity.__class__.__name__ = "Company"
        return entity

    def test_confidence_exactly_at_threshold(self, db_session):
        entity = self._make_entity()
        merged = [{"field": "legal_name", "value": "Acme", "confidence": 0.90, "source": "s1", "reasoning": "x"}]
        result = apply_confident_data(entity, merged, db_session, threshold=0.90)
        assert len(result["applied"]) == 1
        assert result["applied"][0]["value"] == "Acme"

    def test_confidence_just_below_threshold(self, db_session):
        entity = self._make_entity()
        merged = [{"field": "legal_name", "value": "Acme", "confidence": 0.8999, "source": "s1", "reasoning": "x"}]
        result = apply_confident_data(entity, merged, db_session, threshold=0.90)
        assert len(result["applied"]) == 0
        assert len(result["rejected"]) == 1

    def test_confidence_just_above_threshold(self, db_session):
        entity = self._make_entity()
        merged = [{"field": "legal_name", "value": "Acme", "confidence": 0.9001, "source": "s1", "reasoning": "x"}]
        result = apply_confident_data(entity, merged, db_session, threshold=0.90)
        assert len(result["applied"]) == 1

    def test_confidence_none_raises_type_error(self, db_session):
        """Confidence=None triggers TypeError on >= comparison.

        This documents current behavior.
        """
        entity = self._make_entity()
        merged = [{"field": "legal_name", "value": "Acme", "confidence": None, "source": "s1", "reasoning": "x"}]
        with pytest.raises(TypeError):
            apply_confident_data(entity, merged, db_session, threshold=0.90)

    def test_confidence_zero(self, db_session):
        entity = self._make_entity()
        merged = [{"field": "legal_name", "value": "Acme", "confidence": 0.0, "source": "s1", "reasoning": "x"}]
        result = apply_confident_data(entity, merged, db_session, threshold=0.90)
        assert len(result["applied"]) == 0
        assert len(result["rejected"]) == 1

    def test_confidence_one(self, db_session):
        entity = self._make_entity()
        merged = [{"field": "legal_name", "value": "Acme", "confidence": 1.0, "source": "s1", "reasoning": "x"}]
        result = apply_confident_data(entity, merged, db_session, threshold=0.90)
        assert len(result["applied"]) == 1

    def test_confidence_above_one_still_applies(self, db_session):
        entity = self._make_entity()
        merged = [{"field": "legal_name", "value": "Acme", "confidence": 1.5, "source": "s1", "reasoning": "x"}]
        result = apply_confident_data(entity, merged, db_session, threshold=0.90)
        assert len(result["applied"]) == 1

    def test_custom_threshold(self, db_session):
        entity = self._make_entity()
        merged = [{"field": "legal_name", "value": "Acme", "confidence": 0.94, "source": "s1", "reasoning": "x"}]
        result = apply_confident_data(entity, merged, db_session, threshold=0.95)
        assert len(result["applied"]) == 0
        assert len(result["rejected"]) == 1

    def test_empty_merged_list(self, db_session):
        entity = self._make_entity()
        result = apply_confident_data(entity, [], db_session)
        assert result["applied"] == []
        assert result["rejected"] == []
        assert result["sources_used"] == []

    def test_field_not_on_entity_skipped(self, db_session):
        entity = MagicMock(spec=[])  # no attributes
        merged = [{"field": "nonexistent_field", "value": "x", "confidence": 0.95, "source": "s1", "reasoning": "x"}]
        result = apply_confident_data(entity, merged, db_session)
        assert len(result["applied"]) == 0
        assert len(result["rejected"]) == 0

    def test_mixed_apply_and_reject(self, db_session):
        entity = self._make_entity()
        merged = [
            {"field": "legal_name", "value": "Acme", "confidence": 0.95, "source": "s1", "reasoning": "x"},
            {"field": "domain", "value": "acme.com", "confidence": 0.50, "source": "s2", "reasoning": "y"},
            {"field": "industry", "value": "Electronics", "confidence": 0.91, "source": "s3", "reasoning": "z"},
        ]
        result = apply_confident_data(entity, merged, db_session)
        assert len(result["applied"]) == 2
        assert len(result["rejected"]) == 1
        assert result["rejected"][0]["field"] == "domain"


class TestFireAllSourcesEdges:
    """Edge cases for async source orchestration."""

    @pytest.mark.asyncio
    @patch("app.services.enrichment_orchestrator._SOURCE_FUNCS", {})
    async def test_unknown_entity_type_returns_empty(self):
        result = await fire_all_sources("unknown_type", "test-id")
        assert result == {}

    @pytest.mark.asyncio
    async def test_all_sources_return_none(self):
        """When every source function returns None, all values in result are None."""
        null_fn = AsyncMock(return_value=None)
        with (
            patch("app.services.enrichment_orchestrator.COMPANY_SOURCES", {"null_src": "_safe_null"}),
            patch.dict("app.services.enrichment_orchestrator._SOURCE_FUNCS", {"_safe_null": null_fn}),
        ):
            result = await fire_all_sources("company", "test-id")
            assert all(v is None for v in result.values())


class TestClaudeMergeEdges:
    """Edge cases for multi-source merge logic."""

    @pytest.mark.asyncio
    async def test_no_valid_sources_returns_empty(self):
        result = await claude_merge({"src1": None, "src2": None}, "company")
        assert result == []

    @pytest.mark.asyncio
    @patch("app.services.enrichment_orchestrator.claude_json", new_callable=AsyncMock)
    async def test_single_source_skips_claude(self, mock_claude):
        raw = {"src1": {"legal_name": "Acme"}}
        result = await claude_merge(raw, "company")
        mock_claude.assert_not_called()
        assert len(result) > 0
        assert all(item["confidence"] == 0.85 for item in result)
