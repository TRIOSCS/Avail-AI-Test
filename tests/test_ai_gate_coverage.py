"""Tests for app/services/search_worker_base/ai_gate.py — comprehensive coverage.

Covers AIGate classification, cache hits, API failure fallback, batch processing.

Called by: pytest
Depends on: conftest fixtures, AIGate class
"""

import os

os.environ["TESTING"] = "1"

import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.orm import Session

from app.services.search_worker_base.ai_gate import AIGate, _build_schema, _build_system_prompt

# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_queue_item(mpn: str, manufacturer: str = "TI", description: str = "") -> MagicMock:
    item = MagicMock()
    item.mpn = mpn
    item.normalized_mpn = mpn.lower().replace(" ", "")
    item.manufacturer = manufacturer
    item.description = description
    item.status = "pending"
    item.commodity_class = None
    item.gate_decision = None
    item.gate_reason = None
    item.updated_at = None
    item.created_at = datetime.now(timezone.utc)
    return item


# ── Unit tests ────────────────────────────────────────────────────────────────


class TestBuildHelpers:
    def test_build_system_prompt_contains_marketplace(self):
        prompt = _build_system_prompt("ICsource", "search_ics")
        assert "ICsource" in prompt
        assert "search_ics" in prompt

    def test_build_schema_contains_search_field(self):
        schema = _build_schema("search_nc")
        props = schema["properties"]["classifications"]["items"]["properties"]
        assert "search_nc" in props
        assert props["search_nc"]["type"] == "boolean"

    def test_build_schema_required_fields(self):
        schema = _build_schema("search_test")
        required = schema["properties"]["classifications"]["items"]["required"]
        assert "mpn" in required
        assert "search_test" in required
        assert "commodity" in required
        assert "reason" in required


class TestAIGateInit:
    def test_init_stores_config(self):
        gate = AIGate(
            queue_model=MagicMock(),
            marketplace_name="TestMarket",
            search_field="search_test",
            log_prefix="TEST",
        )
        assert gate.marketplace_name == "TestMarket"
        assert gate.search_field == "search_test"
        assert gate.log_prefix == "TEST"
        assert gate._last_api_failure == 0.0
        assert gate._classification_cache == {}


class TestClassifyPartsBatch:
    async def test_empty_parts_returns_empty_list(self):
        gate = AIGate(MagicMock(), "Market", "search_test")
        result = await gate.classify_parts_batch([])
        assert result == []

    async def test_successful_classification(self):
        gate = AIGate(MagicMock(), "ICsource", "search_ics")
        mock_result = {
            "classifications": [
                {"mpn": "LM317T", "search_ics": True, "commodity": "semiconductor", "reason": "voltage regulator IC"}
            ]
        }

        async def _mock_structured(*a, **kw):
            return mock_result

        with patch("app.utils.llm_router.routed_structured", new=_mock_structured):
            result = await gate.classify_parts_batch([{"mpn": "LM317T", "manufacturer": "TI", "description": ""}])

        assert result is not None
        assert len(result) == 1
        assert result[0]["mpn"] == "LM317T"
        assert result[0]["search_ics"] is True

    async def test_api_failure_returns_none(self):
        gate = AIGate(MagicMock(), "ICsource", "search_ics")

        async def _fail(*a, **kw):
            raise RuntimeError("API down")

        with patch("app.utils.llm_router.routed_structured", new=_fail):
            result = await gate.classify_parts_batch([{"mpn": "LM317T", "manufacturer": "TI", "description": ""}])

        assert result is None

    async def test_unexpected_response_format_returns_none(self):
        gate = AIGate(MagicMock(), "ICsource", "search_ics")

        async def _bad(*a, **kw):
            return {"unexpected": "format"}

        with patch("app.utils.llm_router.routed_structured", new=_bad):
            result = await gate.classify_parts_batch([{"mpn": "LM317T", "manufacturer": "TI", "description": ""}])

        assert result is None

    async def test_none_response_returns_none(self):
        gate = AIGate(MagicMock(), "ICsource", "search_ics")

        async def _none(*a, **kw):
            return None

        with patch("app.utils.llm_router.routed_structured", new=_none):
            result = await gate.classify_parts_batch([{"mpn": "LM317T", "manufacturer": "TI", "description": ""}])

        assert result is None


class TestProcessAIGate:
    async def test_no_pending_items_skips(self, db_session: Session):
        MockModel = MagicMock()
        MockModel.status = MagicMock()
        MockModel.created_at = MagicMock()

        # query returns empty list
        db_session_mock = MagicMock()
        db_session_mock.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = []

        gate = AIGate(MockModel, "ICsource", "search_ics")
        # Should not raise
        await gate.process_ai_gate(db_session_mock)

    async def test_cooldown_prevents_processing(self, db_session: Session):
        gate = AIGate(MagicMock(), "ICsource", "search_ics")
        gate._last_api_failure = time.monotonic()  # Set failure just now

        db_mock = MagicMock()
        await gate.process_ai_gate(db_mock)
        # DB should not be queried during cooldown
        db_mock.query.assert_not_called()

    async def test_cache_hit_skips_api_call(self, db_session: Session):
        MockModel = MagicMock()

        item = _make_queue_item("LM317T")
        item.normalized_mpn = "lm317t"
        item.manufacturer = "TI"

        db_mock = MagicMock()
        db_mock.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = [
            item
        ]

        gate = AIGate(MockModel, "ICsource", "search_ics")
        # Pre-populate cache
        gate._classification_cache[("lm317t", "ti")] = ("semiconductor", "search", "IC voltage regulator")

        with patch.object(gate, "classify_parts_batch", new=AsyncMock()) as mock_classify:
            await gate.process_ai_gate(db_mock)
            # classify_parts_batch should NOT be called for cached items
            mock_classify.assert_not_called()

        assert item.status == "queued"
        assert "[cached]" in item.gate_reason

    async def test_cache_hit_skip_decision(self, db_session: Session):
        MockModel = MagicMock()

        item = _make_queue_item("CRCW0402100KFKED")
        item.normalized_mpn = "crcw0402100kfked"
        item.manufacturer = "Vishay"

        db_mock = MagicMock()
        db_mock.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = [
            item
        ]

        gate = AIGate(MockModel, "ICsource", "search_ics")
        gate._classification_cache[("crcw0402100kfked", "vishay")] = ("passive", "skip", "standard resistor")

        await gate.process_ai_gate(db_mock)
        assert item.status == "gated_out"

    async def test_api_failure_defaults_to_search(self, db_session: Session):
        MockModel = MagicMock()

        item = _make_queue_item("LM317T")

        db_mock = MagicMock()
        db_mock.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = [
            item
        ]

        gate = AIGate(MockModel, "ICsource", "search_ics")

        with patch.object(gate, "classify_parts_batch", new=AsyncMock(return_value=None)):
            await gate.process_ai_gate(db_mock)

        # Fail-open: defaults to queued
        assert item.status == "queued"
        assert gate._last_api_failure > 0

    async def test_successful_classification_updates_item(self, db_session: Session):
        MockModel = MagicMock()

        item = _make_queue_item("LM317T")

        db_mock = MagicMock()
        db_mock.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = [
            item
        ]

        gate = AIGate(MockModel, "ICsource", "search_ics")
        classification = [
            {"mpn": "LM317T", "search_ics": True, "commodity": "semiconductor", "reason": "voltage regulator"}
        ]

        with patch.object(gate, "classify_parts_batch", new=AsyncMock(return_value=classification)):
            await gate.process_ai_gate(db_mock)

        assert item.status == "queued"
        assert item.commodity_class == "semiconductor"
        assert item.gate_decision == "search"
        # Should be cached
        assert ("lm317t", "ti") in gate._classification_cache

    async def test_gated_out_item(self, db_session: Session):
        MockModel = MagicMock()

        item = _make_queue_item("CRCW0402100K")

        db_mock = MagicMock()
        db_mock.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = [
            item
        ]

        gate = AIGate(MockModel, "ICsource", "search_ics")
        classification = [
            {"mpn": "CRCW0402100K", "search_ics": False, "commodity": "passive", "reason": "standard resistor"}
        ]

        with patch.object(gate, "classify_parts_batch", new=AsyncMock(return_value=classification)):
            await gate.process_ai_gate(db_mock)

        assert item.status == "gated_out"
        assert item.gate_decision == "skip"

    async def test_missing_mpn_in_result_leaves_pending(self, db_session: Session):
        MockModel = MagicMock()

        item = _make_queue_item("MISSING_MPN")

        db_mock = MagicMock()
        db_mock.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = [
            item
        ]

        gate = AIGate(MockModel, "ICsource", "search_ics")
        # Return empty classifications (no match for MISSING_MPN)
        classification = []  # Result map will be empty

        with patch.object(gate, "classify_parts_batch", new=AsyncMock(return_value=classification)):
            await gate.process_ai_gate(db_mock)

        # Status should remain "pending" since no classification returned
        assert item.status == "pending"

    async def test_mixed_cache_and_uncached(self, db_session: Session):
        MockModel = MagicMock()

        cached_item = _make_queue_item("LM317T")
        cached_item.normalized_mpn = "lm317t"
        cached_item.manufacturer = "TI"

        uncached_item = _make_queue_item("STM32F4")
        uncached_item.normalized_mpn = "stm32f4"
        uncached_item.manufacturer = "STMicro"

        db_mock = MagicMock()
        db_mock.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = [
            cached_item,
            uncached_item,
        ]

        gate = AIGate(MockModel, "ICsource", "search_ics")
        gate._classification_cache[("lm317t", "ti")] = ("semiconductor", "search", "voltage regulator IC")

        uncached_result = [{"mpn": "STM32F4", "search_ics": True, "commodity": "semiconductor", "reason": "MCU"}]

        with patch.object(gate, "classify_parts_batch", new=AsyncMock(return_value=uncached_result)):
            await gate.process_ai_gate(db_mock)

        assert cached_item.status == "queued"
        assert uncached_item.status == "queued"
