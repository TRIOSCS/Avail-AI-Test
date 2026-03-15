"""Phase 4 integration tests — end-to-end verification of all four search layers and the
enrichment pipeline.

What: Tests that search results contain Live Stock, Historical, Vendor Match,
      and AI Found layers; that enrichment applies/rejects fields correctly;
      that the smart AI trigger fires when results are thin; that affinity
      dedup works; and that all result types share consistent scoring fields.
Called by: pytest
Depends on: app.search_service, app.scoring, app.services.vendor_affinity_service,
            app.services.enrichment_orchestrator
"""

import os

os.environ["TESTING"] = "1"

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models import Company, Requirement, Requisition, User
from app.scoring import confidence_color, score_unified
from app.search_service import search_requirement
from app.services.enrichment_orchestrator import (
    apply_confident_data,
    claude_merge,
)
from tests.conftest import engine  # noqa: F401 — ensures SQLite engine is used

pytestmark = pytest.mark.slow


# ── Helpers ──────────────────────────────────────────────────────────────


def _make_user(db: Session) -> User:
    u = User(
        email="integration-phase4@trioscs.com",
        name="Phase4 Integration",
        role="buyer",
        azure_id="p4-integ-001",
        created_at=datetime.now(timezone.utc),
    )
    db.add(u)
    db.flush()
    return u


def _make_requisition(db: Session, user: User) -> Requisition:
    r = Requisition(
        name="P4-INTEG-001",
        customer_name="Test Co",
        status="active",
        created_by=user.id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(r)
    db.flush()
    return r


def _make_requirement(db: Session, requisition: Requisition, mpn: str = "LM317T") -> Requirement:
    req = Requirement(
        requisition_id=requisition.id,
        primary_mpn=mpn,
        target_qty=100,
        created_at=datetime.now(timezone.utc),
    )
    db.add(req)
    db.commit()
    db.refresh(req)
    return req


# ── Mock data covering all four layers ───────────────────────────────────

MOCK_FRESH_LIVE = [
    {
        "vendor_name": "Arrow",
        "mpn_matched": "LM317T",
        "vendor_sku": "ARR-LM317T",
        "source_type": "nexar",
        "is_authorized": True,
        "confidence": 5,
        "manufacturer": "TI",
        "qty_available": 5000,
        "unit_price": 0.45,
        "currency": "USD",
    },
    {
        "vendor_name": "Mouser",
        "mpn_matched": "LM317T",
        "vendor_sku": "MOU-LM317T",
        "source_type": "mouser",
        "is_authorized": True,
        "confidence": 5,
        "manufacturer": "TI",
        "qty_available": 2000,
        "unit_price": 0.52,
        "currency": "USD",
    },
]

MOCK_STATS_OK = [
    {"source": "nexar", "results": 1, "ms": 80, "error": None, "status": "ok"},
    {"source": "mouser", "results": 1, "ms": 95, "error": None, "status": "ok"},
]

MOCK_AFFINITY = [
    {
        "vendor_name": "Vendor Alpha",
        "vendor_id": 201,
        "mpn_count": 7,
        "manufacturer": "TI",
        "level": 1,
        "confidence": 0.65,
        "reasoning": "Vendor supplied 7 other MPN(s) from TI",
    },
    {
        "vendor_name": "Vendor Beta",
        "vendor_id": 202,
        "mpn_count": 3,
        "manufacturer": "TI",
        "level": 2,
        "confidence": 0.44,
        "reasoning": "Vendor shares commodity tags (3 matching tag(s))",
    },
]

# Simulates thin results that should trigger the smart AI second pass
MOCK_FRESH_THIN = [
    {
        "vendor_name": "SmallBroker",
        "mpn_matched": "RARE-IC-001",
        "vendor_sku": "SB-001",
        "source_type": "brokerbin",
        "is_authorized": False,
        "confidence": 3,
        "manufacturer": "Analog Devices",
        "qty_available": 50,
        "unit_price": 12.50,
        "currency": "USD",
    },
]

MOCK_STATS_THIN = [
    {"source": "brokerbin", "results": 1, "ms": 120, "error": None, "status": "ok"},
]

MOCK_AI_RESULTS = [
    {
        "vendor_name": "WebVendor X",
        "mpn_matched": "RARE-IC-001",
        "vendor_sku": "",
        "source_type": "ai_live_web",
        "is_authorized": False,
        "confidence": 0.7,
        "manufacturer": "Analog Devices",
        "qty_available": 200,
        "unit_price": 14.00,
        "currency": "USD",
    },
]


# ── Test: search returns all four layers ─────────────────────────────────


class TestSearchReturnsAllFourLayers:
    """Verify that a full search produces results from Live Stock, Historical, Vendor
    Match, and AI Found layers with proper scoring fields."""

    @pytest.mark.asyncio
    async def test_search_returns_all_four_layers(self, db_session):
        """Mock all connectors + affinity + AI to produce results from all four source
        types, then verify badges, sort order, and confidence colors."""
        user = _make_user(db_session)
        reqn = _make_requisition(db_session, user)
        req = _make_requirement(db_session, reqn, mpn="LM317T")

        # _fetch_fresh returns only 2 live results (< 5 triggers AI)
        # We also need AI results to come back through _fetch_fresh's second pass.
        # Since TESTING=1 disables real AI connector, we mock _fetch_fresh to
        # return live + AI results combined, and mock affinity for vendor match.
        combined_fresh = list(MOCK_FRESH_LIVE) + list(MOCK_AI_RESULTS)
        combined_stats = list(MOCK_STATS_OK) + [
            {"source": "ai_live_web", "results": 1, "ms": 200, "error": None, "status": "ok"},
        ]

        with (
            patch("app.search_service._fetch_fresh", new_callable=AsyncMock) as mock_fetch,
            patch("app.search_service.find_vendor_affinity", return_value=list(MOCK_AFFINITY)),
        ):
            mock_fetch.return_value = (combined_fresh, combined_stats)
            result = await search_requirement(req, db_session)

        sightings = result["sightings"]
        assert len(sightings) > 0, "Should have at least some results"

        # Collect source_badge values present
        badges = {s.get("source_badge") for s in sightings}

        # Live Stock from nexar/mouser
        assert "Live Stock" in badges, f"Missing 'Live Stock' badge, got {badges}"
        # Vendor Match from affinity
        assert "Vendor Match" in badges, f"Missing 'Vendor Match' badge, got {badges}"
        # AI Found from ai_live_web
        assert "AI Found" in badges, f"Missing 'AI Found' badge, got {badges}"

        # Historical may or may not appear (depends on MaterialCard history in DB).
        # We verify the badge system works for historical via the scoring unit test below.

        # Verify sorted by confidence_pct descending
        pcts = [s.get("confidence_pct", 0) for s in sightings]
        assert pcts == sorted(pcts, reverse=True), f"Results not sorted by confidence_pct descending: {pcts}"

        # Every result must have confidence_color
        for s in sightings:
            assert "confidence_color" in s, f"Missing confidence_color on result: {s.get('vendor_name')}"
            assert s["confidence_color"] in {"green", "amber", "red"}, (
                f"Invalid confidence_color '{s['confidence_color']}' on {s.get('vendor_name')}"
            )


# ── Test: enrich company pipeline ────────────────────────────────────────


class TestEnrichCompanyPipeline:
    """Verify the enrichment orchestrator fires sources, merges via Claude, and applies
    high-confidence fields while rejecting low-confidence ones."""

    @pytest.mark.asyncio
    async def test_enrich_company_pipeline(self, db_session):
        """Mock enrichment sources + Claude merge, then verify apply/reject logic."""
        # Create a company entity
        company = Company(
            name="Test Corp",
            domain="testcorp.com",
            website="https://testcorp.com",
            is_active=True,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(company)
        db_session.commit()
        db_session.refresh(company)

        # Simulate Claude merge output with mixed confidence levels
        merged_fields = [
            {
                "field": "industry",
                "value": "Electronic Components Distribution",
                "confidence": 0.95,
                "source": "apollo",
                "reasoning": "All sources agree on industry classification",
            },
            {
                "field": "employee_size",
                "value": "51-200",
                "confidence": 0.92,
                "source": "clearbit",
                "reasoning": "Clearbit and Apollo agree on employee count range",
            },
            {
                "field": "hq_city",
                "value": "Austin",
                "confidence": 0.70,
                "source": "gradient",
                "reasoning": "Only Gradient reports city; others disagree",
            },
            {
                "field": "hq_country",
                "value": "US",
                "confidence": 0.98,
                "source": "apollo",
                "reasoning": "All sources agree on country",
            },
            {
                "field": "legal_name",
                "value": "Test Corporation Inc.",
                "confidence": 0.45,
                "source": "explorium",
                "reasoning": "Only one source, low confidence",
            },
        ]

        # Apply with 0.90 threshold
        summary = apply_confident_data(company, merged_fields, db_session, threshold=0.90)

        # High-confidence fields should be applied
        assert len(summary["applied"]) == 3
        applied_fields = {item["field"] for item in summary["applied"]}
        assert "industry" in applied_fields
        assert "employee_size" in applied_fields
        assert "hq_country" in applied_fields

        # Verify the values were actually set on the entity
        assert company.industry == "Electronic Components Distribution"
        assert company.employee_size == "51-200"
        assert company.hq_country == "US"

        # Low-confidence fields should be rejected
        assert len(summary["rejected"]) == 2
        rejected_fields = {item["field"] for item in summary["rejected"]}
        assert "hq_city" in rejected_fields
        assert "legal_name" in rejected_fields

        # hq_city should NOT have been applied
        assert company.hq_city is None

        # sources_used should list the sources that contributed applied fields
        assert "apollo" in summary["sources_used"]
        assert "clearbit" in summary["sources_used"]

    @pytest.mark.asyncio
    async def test_claude_merge_single_source(self):
        """When only one source returns data, skip Claude call and use it directly."""
        raw_results = {
            "apollo": {"industry": "Semiconductors", "employee_size": "201-500"},
            "clearbit": None,
            "gradient": None,
            "explorium": None,
        }

        merged = await claude_merge(raw_results, "company")

        assert len(merged) == 2
        for item in merged:
            assert item["source"] == "apollo"
            assert item["confidence"] == 0.85
            assert "field" in item
            assert "value" in item

    @pytest.mark.asyncio
    async def test_claude_merge_no_data(self):
        """When no sources return data, merge returns empty list."""
        raw_results = {
            "apollo": None,
            "clearbit": None,
        }

        merged = await claude_merge(raw_results, "company")
        assert merged == []


# ── Test: smart trigger integration ──────────────────────────────────────


class TestSmartTriggerIntegration:
    """Verify that when connectors return few results, the AI search fires as a second
    pass and its results appear in the final output."""

    @pytest.mark.asyncio
    async def test_smart_trigger_fires_with_few_results(self, db_session):
        """With < 5 API results, AI search triggers and AI results are included."""
        user = _make_user(db_session)
        reqn = _make_requisition(db_session, user)
        req = _make_requirement(db_session, reqn, mpn="RARE-IC-001")

        # Return thin results + AI results (simulating the second pass fired)
        combined_fresh = list(MOCK_FRESH_THIN) + list(MOCK_AI_RESULTS)
        combined_stats = list(MOCK_STATS_THIN) + [
            {"source": "ai_live_web", "results": 1, "ms": 300, "error": None, "status": "ok"},
        ]

        with (
            patch("app.search_service._fetch_fresh", new_callable=AsyncMock) as mock_fetch,
            patch("app.search_service.find_vendor_affinity", return_value=[]),
        ):
            mock_fetch.return_value = (combined_fresh, combined_stats)
            result = await search_requirement(req, db_session)

        sightings = result["sightings"]

        # AI results should be present
        ai_results = [s for s in sightings if s.get("source_badge") == "AI Found"]
        assert len(ai_results) >= 1, "AI Found results should be in final output"

        # The AI result should have the capped confidence (max 60)
        for ar in ai_results:
            assert ar["confidence_pct"] <= 60, (
                f"AI Found confidence_pct should be capped at 60, got {ar['confidence_pct']}"
            )

        # The original thin result should also be there
        live_results = [s for s in sightings if s.get("source_badge") == "Live Stock"]
        assert len(live_results) >= 1, "Original live results should still be present"

    @pytest.mark.asyncio
    async def test_smart_trigger_skipped_with_rich_results(self, db_session):
        """With >= 5 API results and prices below target, AI should not trigger."""
        from app.search_service import should_trigger_ai_search

        result = should_trigger_ai_search(
            api_result_count=10,
            has_price_below_target=True,
            is_obsolete=False,
            months_since_last_sighting=1.0,
        )
        assert result is False


# ── Test: affinity dedup in full search ──────────────────────────────────


class TestAffinityDedupInFullSearch:
    """Verify that when affinity returns a vendor already in live results, it does not
    appear as a duplicate."""

    @pytest.mark.asyncio
    async def test_affinity_dedup_removes_duplicate_vendor(self, db_session):
        """Affinity vendor 'Arrow' already in live results should be deduped."""
        user = _make_user(db_session)
        reqn = _make_requisition(db_session, user)
        req = _make_requirement(db_session, reqn, mpn="LM317T")

        # Affinity includes "Arrow" which is already in live MOCK_FRESH_LIVE
        affinity_with_dupe = list(MOCK_AFFINITY) + [
            {
                "vendor_name": "Arrow",
                "vendor_id": 300,
                "mpn_count": 10,
                "manufacturer": "TI",
                "level": 1,
                "confidence": 0.72,
                "reasoning": "Vendor supplied 10 other MPN(s) from TI",
            },
        ]

        with (
            patch("app.search_service._fetch_fresh", new_callable=AsyncMock) as mock_fetch,
            patch("app.search_service.find_vendor_affinity", return_value=affinity_with_dupe),
        ):
            mock_fetch.return_value = (list(MOCK_FRESH_LIVE), list(MOCK_STATS_OK))
            result = await search_requirement(req, db_session)

        sightings = result["sightings"]

        # Count Arrow occurrences — should be exactly 1 (from live, not affinity)
        arrow_results = [s for s in sightings if s.get("vendor_name", "").lower() == "arrow"]
        assert len(arrow_results) == 1, f"Arrow should appear exactly once but found {len(arrow_results)}"
        # The one Arrow result should be from live, not affinity
        assert arrow_results[0].get("source_type") == "nexar"
        assert arrow_results[0].get("is_affinity") is not True

        # The non-duplicate affinity vendors should still be present
        affinity_results = [s for s in sightings if s.get("is_affinity")]
        affinity_names = {ar["vendor_name"] for ar in affinity_results}
        assert "Vendor Alpha" in affinity_names
        assert "Vendor Beta" in affinity_names
        assert "Arrow" not in affinity_names


# ── Test: unified scoring consistency ────────────────────────────────────


class TestUnifiedScoringConsistency:
    """Verify that score_unified returns consistent fields for all source types."""

    REQUIRED_KEYS = {"score", "source_badge", "confidence_pct", "confidence_color", "components"}

    def test_live_stock_scoring(self):
        """Live API results produce all required scoring fields."""
        result = score_unified(
            source_type="nexar",
            vendor_score=80.0,
            is_authorized=True,
            unit_price=0.50,
            qty_available=1000,
            has_price=True,
            has_qty=True,
        )
        assert self.REQUIRED_KEYS.issubset(result.keys()), f"Missing keys: {self.REQUIRED_KEYS - result.keys()}"
        assert result["source_badge"] == "Live Stock"
        assert 70 <= result["confidence_pct"] <= 95
        assert result["confidence_color"] in {"green", "amber", "red"}

    def test_historical_scoring(self):
        """Historical results produce all required scoring fields."""
        result = score_unified(
            source_type="historical",
            age_hours=720.0,  # ~30 days
            repeat_sighting_count=3,
        )
        assert self.REQUIRED_KEYS.issubset(result.keys())
        assert result["source_badge"] == "Historical"
        assert 0 <= result["confidence_pct"] <= 100
        assert result["confidence_color"] in {"green", "amber", "red"}

    def test_vendor_affinity_scoring(self):
        """Vendor affinity results produce all required scoring fields."""
        result = score_unified(
            source_type="vendor_affinity",
            claude_confidence=0.65,
        )
        assert self.REQUIRED_KEYS.issubset(result.keys())
        assert result["source_badge"] == "Vendor Match"
        assert result["confidence_pct"] == 65
        assert result["confidence_color"] == "amber"

    def test_ai_live_web_scoring(self):
        """AI research results produce all required scoring fields and are capped at
        60."""
        result = score_unified(
            source_type="ai_live_web",
            claude_confidence=0.90,
        )
        assert self.REQUIRED_KEYS.issubset(result.keys())
        assert result["source_badge"] == "AI Found"
        assert result["confidence_pct"] <= 60, "AI results should be capped at 60"
        assert result["confidence_color"] in {"green", "amber", "red"}

    def test_all_types_have_same_keys(self):
        """All four source types return dictionaries with identical key sets."""
        live = score_unified(source_type="nexar", has_price=True, has_qty=True)
        historical = score_unified(source_type="historical", age_hours=100.0)
        affinity = score_unified(source_type="vendor_affinity", claude_confidence=0.5)
        ai = score_unified(source_type="ai_live_web", claude_confidence=0.5)

        assert live.keys() == historical.keys() == affinity.keys() == ai.keys(), (
            "All source types must return the same top-level keys"
        )

    def test_confidence_color_boundaries(self):
        """Verify confidence_color thresholds: >=75 green, >=50 amber, <50 red."""
        assert confidence_color(75) == "green"
        assert confidence_color(100) == "green"
        assert confidence_color(74) == "amber"
        assert confidence_color(50) == "amber"
        assert confidence_color(49) == "red"
        assert confidence_color(0) == "red"
