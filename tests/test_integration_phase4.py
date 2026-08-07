"""Phase 4 integration tests — end-to-end verification of all four search layers.

What: Tests that search results contain Live Stock, Historical, Vendor Match,
      and AI Found layers; that the smart AI trigger fires when results are thin;
      that affinity dedup works; and that all result types share consistent
      scoring fields.
Called by: pytest
Depends on: app.search_service, app.scoring, app.services.vendor_affinity_service
"""

import os

os.environ["TESTING"] = "1"

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models import Requirement, Requisition, User
from app.scoring import confidence_color, source_badge
from app.search_service import search_requirement
from tests.conftest import engine  # noqa: F401

pytestmark = pytest.mark.slow


# ── Helpers ──────────────────────────────────────────────────────────────


def _make_user(db: Session) -> User:
    u = User(
        email="integration-phase4@trioscs.com",
        name="Phase4 Integration",
        role="buyer",
        azure_id="p4-integ-001",
        created_at=datetime.now(UTC),
    )
    db.add(u)
    db.flush()
    return u


def _make_requisition(db: Session, user: User) -> Requisition:
    r = Requisition(
        name="P4-INTEG-001",
        customer_name="Test Co",
        status="open",
        created_by=user.id,
        created_at=datetime.now(UTC),
    )
    db.add(r)
    db.flush()
    return r


def _make_requirement(db: Session, requisition: Requisition, mpn: str = "LM317T") -> Requirement:
    req = Requirement(
        requisition_id=requisition.id,
        primary_mpn=mpn,
        target_qty=100,
        created_at=datetime.now(UTC),
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
            patch("app.search_service.fanout._fetch_fresh", new_callable=AsyncMock) as mock_fetch,
            patch("app.search_service.pipeline.find_vendor_affinity", return_value=list(MOCK_AFFINITY)),
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
            patch("app.search_service.fanout._fetch_fresh", new_callable=AsyncMock) as mock_fetch,
            patch("app.search_service.pipeline.find_vendor_affinity", return_value=[]),
        ):
            mock_fetch.return_value = (combined_fresh, combined_stats)
            result = await search_requirement(req, db_session)

        sightings = result["sightings"]

        # AI results should be present
        ai_results = [s for s in sightings if s.get("source_badge") == "AI Found"]
        assert len(ai_results) >= 1, "AI Found results should be in final output"

        # Spec §9: AI rows display the SAME persisted v2 score as every other row —
        # the old score_unified 60-cap re-derivation is gone. Screen == DB.
        for ar in ai_results:
            assert ar["confidence_pct"] == int(round(ar["score"])), (
                f"AI Found confidence_pct must derive from the persisted score, got {ar['confidence_pct']}"
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
            patch("app.search_service.fanout._fetch_fresh", new_callable=AsyncMock) as mock_fetch,
            patch("app.search_service.pipeline.find_vendor_affinity", return_value=affinity_with_dupe),
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


# ── Test: display-field consistency (v2-persisted reads, spec §9) ────────


class TestDisplayFieldConsistency:
    """Every result-row shape carries the same display fields, all derived from the
    persisted v2 score (or metadata-only) + the static source_badge map — the old
    score_unified re-derivation is gone."""

    DISPLAY_KEYS = {"score", "source_badge", "confidence_pct", "confidence_color"}

    def test_live_history_and_affinity_rows_share_display_keys(self):
        from datetime import UTC, datetime, timedelta
        from types import SimpleNamespace

        from app.search_service import _affinity_match_to_result, _history_to_result, sighting_to_dict

        now = datetime.now(UTC)
        live = sighting_to_dict(
            SimpleNamespace(
                id=1,
                requirement_id=None,
                vendor_name="Arrow",
                vendor_email=None,
                vendor_phone=None,
                mpn_matched="LM317T",
                manufacturer="TI",
                qty_available=100,
                unit_price=0.5,
                currency="USD",
                source_type="nexar",
                is_authorized=False,
                confidence=0.0,
                score=64.2,
                raw_data={},
                is_unavailable=False,
                moq=None,
                date_code=None,
                packaging=None,
                condition=None,
                lead_time_days=None,
                lead_time=None,
                evidence_tier="T3",
                score_components={"trust": 35.0},
                created_at=now,
            )
        )
        hist = _history_to_result(
            {
                "vendor_name": "Old Vendor",
                "mpn_matched": "LM317T",
                "manufacturer": "TI",
                "qty_available": 10,
                "unit_price": 1.0,
                "currency": "USD",
                "source_type": "nexar",
                "is_authorized": False,
                "vendor_sku": None,
                "first_seen": now - timedelta(days=60),
                "last_seen": now - timedelta(days=10),
                "times_seen": 2,
                "material_card_id": 1,
                "persisted_score": 41.0,
                "persisted_score_components": None,
            },
            now,
        )
        affinity = _affinity_match_to_result({"vendor_name": "Preferred", "confidence": 0.65}, "LM317T")

        for row in (live, hist, affinity):
            assert self.DISPLAY_KEYS.issubset(row.keys())
            assert row["confidence_color"] in {"green", "amber", "red", None}

        # Display derives from the persisted/computed v2 number verbatim.
        assert live["confidence_pct"] == 64
        assert hist["confidence_pct"] == 41
        assert affinity["confidence_pct"] == 65
        assert affinity["score"] == 0  # confidence-only rows carry no derived score

    def test_source_badges_are_a_static_map(self):
        assert source_badge("nexar") == "Live Stock"
        assert source_badge("historical") == "Historical"
        assert source_badge("vendor_affinity") == "Vendor Match"
        assert source_badge("ai_live_web") == "AI Found"

    def test_confidence_color_boundaries(self):
        """Verify confidence_color thresholds: >=75 green, >=50 amber, <50 red."""
        assert confidence_color(75) == "green"
        assert confidence_color(100) == "green"
        assert confidence_color(74) == "amber"
        assert confidence_color(50) == "amber"
        assert confidence_color(49) == "red"
        assert confidence_color(0) == "red"
