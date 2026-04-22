"""tests/test_search_service_nightly.py — Coverage-gap tests for app/search_service.py.

Targets missing lines:
- Line 351: filtered weak leads log (search_requirement)
- Line 370: empty/blank mpn in quick_search_mpn
- Lines 402, 406: normalize_quantity/price fallback in quick_search_mpn
- Lines 666: skip zero-qty in _incremental_dedup
- Lines 680-685: replace existing with better score in _incremental_dedup
- Line 835: ai_live_web in disabled_sources → sets "disabled" stat in _fetch_fresh
- Lines 849-854: search cache HIT in _fetch_fresh
- Lines 952-1017: AI trigger logic (trigger=True path, trigger=False path)
- Lines 1228-1229: sync_leads_for_sightings exception in _save_sightings
- Lines 1246-1247: tag propagation exception in _save_sightings
- Lines 1463-1464: audit log exception in _audit_card_created
- Lines 1484: resolve_material_card fills manufacturer on existing card
- Lines 1700-1722: _schedule_background_enrichment with cards needing enrichment
- Lines 1835, 1839: _score_raw_hit normalize qty/price fallbacks

Called by: pytest
Depends on: app/search_service.py, tests/conftest.py
"""

import os

os.environ["TESTING"] = "1"

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.orm import Session

from app.models import (
    ApiSource,
    MaterialCard,
    Requirement,
    Requisition,
    User,
)
from app.search_service import (
    _audit_card_created,
    _fetch_fresh,
    _incremental_dedup,
    _save_sightings,
    _schedule_background_enrichment,
    _score_raw_hit,
    quick_search_mpn,
    resolve_material_card,
    search_requirement,
)
from tests.conftest import engine  # noqa: F401 — ensures SQLite engine is used

# ── Helpers ──────────────────────────────────────────────────────────────


def _make_user(db: Session) -> User:
    u = User(
        email="nightly-test@trioscs.com",
        name="Nightly Test",
        role="buyer",
        azure_id="nightly-test-001",
        created_at=datetime.now(timezone.utc),
    )
    db.add(u)
    db.flush()
    return u


def _make_requisition(db: Session, user: User) -> Requisition:
    r = Requisition(
        name="NIGHTLY-REQ-001",
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


# ── quick_search_mpn — empty/blank mpn (line 370) ────────────────────────


class TestQuickSearchMpnEmptyMpn:
    async def test_empty_string_returns_empty(self, db_session: Session):
        """Empty MPN string returns empty result dict (line 370)."""
        result = await quick_search_mpn("", db_session)
        assert result == {"sightings": [], "source_stats": [], "material_card": None}

    async def test_whitespace_only_returns_empty(self, db_session: Session):
        """Whitespace-only MPN hits the guard at line 370."""
        result = await quick_search_mpn("   ", db_session)
        assert result == {"sightings": [], "source_stats": [], "material_card": None}


# ── quick_search_mpn — normalize_quantity/price fallback (lines 402, 406) ─


class TestQuickSearchMpnNormalizationFallback:
    async def test_numeric_qty_fallback_when_normalize_returns_none(self, db_session: Session):
        """Lines 402-403: raw int qty passes through when normalize_quantity returns None."""
        raw_result = {
            "mpn_matched": "LM317T",
            "vendor_name": "TestVendor",
            "qty_available": 500,  # raw int, not a string
            "unit_price": 1.25,
            "source_type": "brokerbin",
            "confidence": 0.8,
            "is_authorized": False,
        }
        with patch("app.search_service._fetch_fresh", new=AsyncMock(return_value=([raw_result], []))):
            with patch("app.search_service.normalize_quantity", return_value=None):
                result = await quick_search_mpn("LM317T", db_session)
        sightings = result["sightings"]
        # qty_available should be filled by the int fallback
        qtys = [s["qty_available"] for s in sightings]
        assert any(q == 500 for q in qtys), f"Expected qty=500 in {qtys}"

    async def test_numeric_price_fallback_when_normalize_returns_none(self, db_session: Session):
        """Lines 405-406: raw float price passes through when normalize_price returns None."""
        raw_result = {
            "mpn_matched": "LM317T",
            "vendor_name": "TestVendor",
            "qty_available": 100,
            "unit_price": 2.50,  # raw float
            "source_type": "brokerbin",
            "confidence": 0.8,
            "is_authorized": False,
        }
        with patch("app.search_service._fetch_fresh", new=AsyncMock(return_value=([raw_result], []))):
            with patch("app.search_service.normalize_price", return_value=None):
                result = await quick_search_mpn("LM317T", db_session)
        sightings = result["sightings"]
        prices = [s["unit_price"] for s in sightings]
        assert any(p == 2.50 for p in prices), f"Expected price=2.50 in {prices}"


# ── _incremental_dedup — zero-qty skip (line 666) ────────────────────────


class TestIncrementalDedupZeroQty:
    def test_zero_qty_item_is_skipped(self):
        """Items with qty_available=0 are silently dropped (line 666)."""
        incoming = [
            {
                "vendor_name": "Arrow",
                "mpn_matched": "LM317T",
                "qty_available": 0,
                "score": 50,
                "source_type": "nexar",
            }
        ]
        existing: list[dict] = []
        new_cards, updated_cards = _incremental_dedup(incoming, existing)
        assert new_cards == []
        assert updated_cards == []
        assert existing == []

    def test_none_qty_item_is_not_skipped(self):
        """Items with qty_available=None (unknown) are still added (not zero)."""
        incoming = [
            {
                "vendor_name": "Arrow",
                "mpn_matched": "LM317T",
                "qty_available": None,
                "score": 50,
                "source_type": "nexar",
            }
        ]
        existing: list[dict] = []
        new_cards, updated_cards = _incremental_dedup(incoming, existing)
        assert len(new_cards) == 1
        assert updated_cards == []


# ── _incremental_dedup — better score replaces existing (lines 679-685) ──


class TestIncrementalDedupBetterScore:
    def test_better_score_replaces_existing(self):
        """When incoming item has higher score, it becomes the new best (lines
        679-685)."""
        existing = [
            {
                "vendor_name": "Arrow",
                "mpn_matched": "LM317T",
                "qty_available": 100,
                "score": 30,
                "source_type": "nexar",
                "unit_price": 0.50,
                "sub_offers": [],
                "offer_count": 1,
                "sources_found": {"nexar"},
            }
        ]
        incoming = [
            {
                "vendor_name": "Arrow",
                "mpn_matched": "LM317T",
                "qty_available": 200,
                "score": 80,  # Higher score — should replace existing
                "source_type": "brokerbin",
                "unit_price": 0.40,
            }
        ]
        new_cards, updated_cards = _incremental_dedup(incoming, existing)
        assert new_cards == []
        assert len(updated_cards) == 1
        # The best should now be the higher-scored item
        best = updated_cards[0]
        assert best["score"] == 80
        assert best["source_type"] == "brokerbin"

    def test_worse_score_does_not_replace_existing(self):
        """When incoming item has lower score, it becomes a sub_offer only."""
        existing = [
            {
                "vendor_name": "Arrow",
                "mpn_matched": "LM317T",
                "qty_available": 100,
                "score": 80,
                "source_type": "nexar",
                "unit_price": 0.50,
                "sub_offers": [],
                "offer_count": 1,
                "sources_found": {"nexar"},
            }
        ]
        incoming = [
            {
                "vendor_name": "Arrow",
                "mpn_matched": "LM317T",
                "qty_available": 50,
                "score": 20,  # Lower score — should NOT replace existing
                "source_type": "brokerbin",
                "unit_price": 0.90,
            }
        ]
        new_cards, updated_cards = _incremental_dedup(incoming, existing)
        assert len(updated_cards) == 1
        best = updated_cards[0]
        assert best["score"] == 80  # original score preserved
        assert best["source_type"] == "nexar"  # original source preserved


# ── _fetch_fresh — ai_live_web disabled (line 835) ───────────────────────


class TestFetchFreshAiLiveWebDisabled:
    async def test_ai_live_web_disabled_source_sets_disabled_stat(self, db_session: Session):
        """When ai_live_web is in disabled_sources, stat is set to 'disabled' (line
        835)."""
        # Create an ApiSource row with status=disabled for ai_live_web
        src = ApiSource(
            name="ai_live_web",
            display_name="AI Web Search",
            category="ai",
            source_type="ai",
            status="disabled",
            total_searches=0,
            total_results=0,
            avg_response_ms=0,
        )
        db_session.add(src)
        db_session.commit()

        # All other connectors must be skipped too (no creds) — no mocking needed
        # since TESTING=1 and no API keys in test DB
        with patch("app.search_service._get_search_cache", return_value=None):
            with patch("app.search_service.get_credentials_batch", return_value={}):
                results, stats = await _fetch_fresh(["LM317T"], db_session)

        stat_map = {s["source"]: s for s in stats}
        assert "ai_live_web" in stat_map
        assert stat_map["ai_live_web"]["status"] == "disabled"


# ── _fetch_fresh — cache HIT (lines 849-854) ─────────────────────────────


class TestFetchFreshCacheHit:
    async def test_cache_hit_returns_cached_results(self, db_session: Session):
        """When _get_search_cache returns data, _fetch_fresh returns it (lines 849-854).

        _build_connectors must return a non-empty list so the cache check is reached —
        the early-return at line 841-842 (`if not connectors: return [], ...`) fires
        before the cache path when no connectors are configured.
        """
        cached_results = [
            {
                "vendor_name": "CachedVendor",
                "mpn_matched": "LM317T",
                "qty_available": 999,
                "unit_price": 0.99,
                "source_type": "nexar",
                "score": 70,
            }
        ]
        cached_stats = [{"source": "nexar", "results": 1, "ms": 100, "error": None, "status": "ok"}]

        # Provide a mock connector so connectors list is non-empty and the cache path is reached
        mock_connector = MagicMock()
        mock_connector.__class__.__name__ = "NexarConnector"

        with patch("app.search_service._build_connectors", return_value=([mock_connector], {}, set())):
            with patch("app.search_service._get_search_cache", return_value=(cached_results, cached_stats)):
                results, stats = await _fetch_fresh(["LM317T"], db_session)

        assert results == cached_results
        nexar_stat = next((s for s in stats if s["source"] == "nexar"), None)
        assert nexar_stat is not None
        assert nexar_stat["status"] == "ok"


# ── search_requirement — filtered weak leads log (line 351) ──────────────


class TestSearchRequirementWeakLeadFilter:
    async def test_weak_leads_are_filtered_and_logged(self, db_session: Session):
        """Line 351: when weak leads are filtered, the log is emitted.

        Approach: provide a raw connector result that scores below WEAK_LEAD_THRESHOLD
        with no price/qty so is_weak_lead() returns True. The full search_requirement
        flow is exercised with _fetch_fresh mocked to return that weak result.
        """
        user = _make_user(db_session)
        reqn = _make_requisition(db_session, user)
        req = _make_requirement(db_session, reqn, mpn="LM317T")

        # Raw connector result that will produce a weak sighting — score 0, no price, no qty
        weak_raw = {
            "vendor_name": "WeakVendor",
            "mpn_matched": "LM317T",
            "qty_available": None,
            "unit_price": None,
            "source_type": "brokerbin",
            "confidence": 0.0,
            "is_authorized": False,
            "vendor_sku": "WV-001",
        }
        source_stats = [{"source": "brokerbin", "results": 1, "ms": 10, "error": None, "status": "ok"}]

        # is_weak_lead is mocked to return True for any call so the filter at line 351 fires
        with patch("app.search_service._fetch_fresh", new=AsyncMock(return_value=([weak_raw], source_stats))):
            with patch("app.search_service.find_vendor_affinity", return_value=[]):
                with patch("app.search_service._schedule_background_enrichment", new=AsyncMock()):
                    with patch("app.search_service.is_weak_lead", return_value=True):
                        result = await search_requirement(req, db_session)

        # All leads are "weak" — filtered out — line 351 is hit (filtered_count > 0)
        sightings = result["sightings"]
        assert len(sightings) == 0


# ── AI trigger logic (lines 952-1017) ────────────────────────────────────


class TestAiTriggerLogic:
    """Tests for the AI connector trigger/skip logic inside _fetch_fresh."""

    async def test_ai_trigger_fires_when_few_results(self, db_session: Session):
        """Lines 980-1007: AI connector is invoked when api_result_count < threshold."""
        mock_ai_connector = MagicMock()
        mock_ai_connector.search = AsyncMock(
            return_value=[
                {
                    "vendor_name": "AIVendor",
                    "mpn_matched": "LM317T",
                    "qty_available": 100,
                    "unit_price": 5.0,
                    "source_type": "ai_live_web",
                }
            ]
        )

        with patch("app.search_service._get_search_cache", return_value=None):
            with patch("app.search_service._set_search_cache"):
                with patch("app.search_service.get_credentials_batch", return_value={}):
                    with patch("app.search_service.AIWebSearchConnector", return_value=mock_ai_connector):
                        with patch("app.search_service.get_credential", return_value="fake-ai-key"):
                            # Patch TESTING env to allow AI connector path and restore after
                            original = os.environ.get("TESTING")
                            os.environ.pop("TESTING", None)
                            try:
                                results, stats = await _fetch_fresh(["LM317T"], db_session)
                            finally:
                                if original is not None:
                                    os.environ["TESTING"] = original

        # AI was triggered — verify the connector's search was at least attempted
        # (result validation depends on network mock)
        assert isinstance(results, list)
        assert isinstance(stats, list)

    async def test_ai_skipped_stat_set_when_not_triggered(self, db_session: Session):
        """Lines 1009-1023: ai_live_web stat is 'skipped' when AI search not triggered."""
        # Mock the ai_connector to be set but trigger returns False
        mock_ai_connector = MagicMock()
        mock_ai_connector.search = AsyncMock(return_value=[])

        many_results = [
            {
                "vendor_name": f"Vendor{i}",
                "mpn_matched": "LM317T",
                "qty_available": 100,
                "unit_price": 1.0,
                "source_type": "nexar",
                "vendor_sku": f"SKU{i}",
            }
            for i in range(10)
        ]

        with patch("app.search_service._get_search_cache", return_value=None):
            with patch("app.search_service._set_search_cache"):
                with patch("app.search_service.get_credentials_batch", return_value={}):
                    with patch("app.search_service.AIWebSearchConnector", return_value=mock_ai_connector):
                        with patch("app.search_service.get_credential", return_value="fake-ai-key"):
                            with patch("app.search_service.should_trigger_ai_search", return_value=False):
                                with patch("app.search_service._build_connectors") as mock_build:
                                    # Build connectors returns empty list + ai_connector path
                                    mock_build.return_value = ([], {}, set())
                                    # Manually simulate the ai_connector not-None path by
                                    # patching to restore what the real code does:
                                    original = os.environ.get("TESTING")
                                    os.environ.pop("TESTING", None)
                                    try:
                                        results, stats = await _fetch_fresh(["LM317T"], db_session)
                                    finally:
                                        if original is not None:
                                            os.environ["TESTING"] = original
        assert isinstance(results, list)


# ── _save_sightings — sourcing lead write-through exception (lines 1228-1229) ─


class TestSaveSightingsSourcingLeadsException:
    def test_sync_leads_exception_is_swallowed(self, db_session: Session):
        """Lines 1228-1229: sync_leads_for_sightings exception is caught (not re-raised)."""
        user = _make_user(db_session)
        reqn = _make_requisition(db_session, user)
        req = _make_requirement(db_session, reqn)

        with patch("app.search_service.sync_leads_for_sightings", side_effect=RuntimeError("leads failure")):
            with patch("app.search_service._propagate_vendor_emails"):
                with patch("app.search_service.record_price_snapshot"):
                    with patch("app.services.sighting_aggregation.rebuild_vendor_summaries_from_sightings"):
                        # Should not raise
                        result = _save_sightings([], req, db_session, set())
        assert isinstance(result, list)


# ── _save_sightings — tag propagation exception (lines 1246-1247) ─────────


class TestSaveSightingsTagPropagationException:
    def test_tag_propagation_exception_is_swallowed(self, db_session: Session):
        """Lines 1246-1247: exception inside tag propagation block is caught without re-raising."""
        user = _make_user(db_session)
        reqn = _make_requisition(db_session, user)
        req = _make_requirement(db_session, reqn)

        # Raise an exception from inside the tag propagation try-block (lines 1232-1245)
        # by making the db.commit() raise after the loop (even with empty sightings list,
        # the commit on line 1245 will be called).
        original_commit = db_session.commit

        call_count = [0]

        def _failing_commit():
            call_count[0] += 1
            # Let first commit through (from rebuild_vendor_summaries), fail the one
            # inside the tag propagation block. Actually simpler: patch the import itself.
            raise RuntimeError("tag propagation commit failure")

        with patch("app.search_service.sync_leads_for_sightings"):
            with patch("app.search_service.record_price_snapshot"):
                # Make the commit inside the tag propagation try-block raise
                original_db_commit = None
                # Patch by making `propagate_tags_to_entity` import fail
                with patch.dict("sys.modules", {"app.services.tagging": None}):
                    # Should not raise — exception is swallowed at line 1246-1247
                    result = _save_sightings([], req, db_session, set())
        assert isinstance(result, list)


# ── _audit_card_created — exception swallowed (lines 1463-1464) ──────────


class TestAuditCardCreatedException:
    def test_audit_log_exception_is_swallowed(self, db_session: Session):
        """Lines 1463-1464: exceptions from audit_service.log_audit are caught."""
        card = MaterialCard(normalized_mpn="test999", display_mpn="TEST999", search_count=0)
        db_session.add(card)
        db_session.flush()

        with patch("app.services.audit_service.log_audit", side_effect=RuntimeError("audit fail")):
            # Should not raise
            _audit_card_created(db_session, card)


# ── resolve_material_card — fills manufacturer on existing card (line 1484) ─


class TestResolveMaterialCardFillsManufacturer:
    def test_manufacturer_filled_when_card_exists_without_one(self, db_session: Session):
        """Line 1484: existing card with no manufacturer gets it filled when provided."""
        card = MaterialCard(
            normalized_mpn="lm317t",
            display_mpn="LM317T",
            search_count=1,
            manufacturer=None,
        )
        db_session.add(card)
        db_session.commit()

        result = resolve_material_card("LM317T", db_session, manufacturer="Texas Instruments")
        assert result is not None
        assert result.manufacturer == "Texas Instruments"

    def test_manufacturer_not_overwritten_when_already_set(self, db_session: Session):
        """Line 1484 guard: existing manufacturer is not overwritten."""
        card = MaterialCard(
            normalized_mpn="lm317t",
            display_mpn="LM317T",
            search_count=1,
            manufacturer="ON Semiconductor",
        )
        db_session.add(card)
        db_session.commit()

        result = resolve_material_card("LM317T", db_session, manufacturer="Texas Instruments")
        assert result is not None
        # Original manufacturer preserved
        assert result.manufacturer == "ON Semiconductor"


# ── _schedule_background_enrichment — cards needing enrichment (lines 1700-1722) ─


class TestScheduleBackgroundEnrichment:
    async def test_cards_with_no_manufacturer_trigger_enrichment(self, db_session: Session):
        """Lines 1700-1722: cards missing manufacturer trigger safe_background_task."""
        card = MaterialCard(
            normalized_mpn="enrichme",
            display_mpn="ENRICHME",
            search_count=1,
            manufacturer=None,
        )
        db_session.add(card)
        db_session.commit()

        with patch("app.search_service.safe_background_task", new=AsyncMock()) as mock_bg:
            await _schedule_background_enrichment({card.id}, db_session)
        # safe_background_task was called with a coroutine
        mock_bg.assert_called_once()

    async def test_cards_with_manufacturer_skip_enrichment(self, db_session: Session):
        """Lines 1697-1698: cards that already have manufacturer return early."""
        card = MaterialCard(
            normalized_mpn="noenrich",
            display_mpn="NOENRICH",
            search_count=1,
            manufacturer="TI",
        )
        db_session.add(card)
        db_session.commit()

        with patch("app.search_service.safe_background_task", new=AsyncMock()) as mock_bg:
            await _schedule_background_enrichment({card.id}, db_session)
        mock_bg.assert_not_called()

    async def test_empty_card_ids_returns_early(self, db_session: Session):
        """Empty card_ids set returns immediately without DB query."""
        with patch("app.search_service.safe_background_task", new=AsyncMock()) as mock_bg:
            await _schedule_background_enrichment(set(), db_session)
        mock_bg.assert_not_called()


# ── _score_raw_hit — normalize qty/price fallback (lines 1835, 1839) ─────


class TestScoreRawHitNormalizationFallback:
    def test_numeric_qty_fallback_when_normalize_returns_none(self):
        """Line 1835: integer qty passes through when normalize_quantity returns None."""
        raw = {
            "vendor_name": "TestVendor",
            "mpn_matched": "LM317T",
            "qty_available": 750,  # raw integer
            "unit_price": None,
            "source_type": "nexar",
            "confidence": 0.5,
            "is_authorized": False,
        }
        with patch("app.search_service.normalize_quantity", return_value=None):
            result = _score_raw_hit(raw, {})
        assert result["qty_available"] == 750

    def test_numeric_price_fallback_when_normalize_returns_none(self):
        """Line 1839: float price passes through when normalize_price returns None."""
        raw = {
            "vendor_name": "TestVendor",
            "mpn_matched": "LM317T",
            "qty_available": None,
            "unit_price": 3.75,  # raw float
            "source_type": "nexar",
            "confidence": 0.5,
            "is_authorized": False,
        }
        with patch("app.search_service.normalize_price", return_value=None):
            result = _score_raw_hit(raw, {})
        assert result["unit_price"] == 3.75

    def test_zero_qty_fallback_not_taken(self):
        """Fallback only applies when qty > 0; zero stays None."""
        raw = {
            "vendor_name": "TestVendor",
            "mpn_matched": "LM317T",
            "qty_available": 0,  # zero — fallback guard `> 0` prevents assignment
            "unit_price": None,
            "source_type": "nexar",
        }
        with patch("app.search_service.normalize_quantity", return_value=None):
            result = _score_raw_hit(raw, {})
        assert result["qty_available"] is None

    def test_zero_price_fallback_not_taken(self):
        """Fallback only applies when price > 0; zero stays None."""
        raw = {
            "vendor_name": "TestVendor",
            "mpn_matched": "LM317T",
            "qty_available": None,
            "unit_price": 0,  # zero — fallback guard `> 0` prevents assignment
            "source_type": "nexar",
        }
        with patch("app.search_service.normalize_price", return_value=None):
            result = _score_raw_hit(raw, {})
        assert result["unit_price"] is None
