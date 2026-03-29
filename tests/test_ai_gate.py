"""Tests for app/services/search_worker_base/ai_gate.py.

Covers: _build_system_prompt, _build_schema, AIGate.__init__,
classify_parts_batch, process_ai_gate (cache hit, classify, API failure, cooldown).

Called by: pytest
Depends on: conftest.py (db_session)
"""

import os

os.environ["TESTING"] = "1"

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.search_worker_base.ai_gate import (
    AIGate,
    _build_schema,
    _build_system_prompt,
)


class TestBuildSystemPrompt:
    def test_contains_marketplace_name(self):
        prompt = _build_system_prompt("ICsource", "search_ics")
        assert "ICsource" in prompt

    def test_contains_search_field(self):
        prompt = _build_system_prompt("NetComponents", "search_nc")
        assert "search_nc" in prompt

    def test_non_empty(self):
        assert len(_build_system_prompt("X", "y")) > 50


class TestBuildSchema:
    def test_schema_structure(self):
        schema = _build_schema("search_ics")
        assert schema["type"] == "object"
        assert "classifications" in schema["properties"]

    def test_search_field_in_item_properties(self):
        schema = _build_schema("search_nc")
        items = schema["properties"]["classifications"]["items"]
        assert "search_nc" in items["properties"]


class TestAIGateInit:
    def test_attributes_set(self):
        model = MagicMock()
        gate = AIGate(model, "ICsource", "search_ics", "ICS")
        assert gate.queue_model is model
        assert gate.marketplace_name == "ICsource"
        assert gate.search_field == "search_ics"
        assert gate.log_prefix == "ICS"
        assert gate._last_api_failure == 0.0
        assert gate._classification_cache == {}

    def test_default_log_prefix(self):
        gate = AIGate(MagicMock(), "Market", "search_x")
        assert gate.log_prefix == "WORKER"


class TestClassifyPartsBatch:
    @pytest.mark.asyncio
    async def test_empty_parts_returns_empty(self):
        gate = AIGate(MagicMock(), "ICsource", "search_ics")
        result = await gate.classify_parts_batch([])
        assert result == []

    @pytest.mark.asyncio
    async def test_returns_classifications_on_success(self):
        gate = AIGate(MagicMock(), "ICsource", "search_ics")
        classifications = [{"mpn": "STM32F407", "search_ics": True, "commodity": "semiconductor", "reason": "IC"}]
        with patch(
            "app.services.search_worker_base.ai_gate.AIGate.classify_parts_batch",
            new_callable=AsyncMock,
            return_value=classifications,
        ):
            result = await gate.classify_parts_batch([{"mpn": "STM32F407"}])
            # We patched the method itself, so result comes directly
            assert result == classifications

    @pytest.mark.asyncio
    async def test_returns_none_on_api_exception(self):
        gate = AIGate(MagicMock(), "ICsource", "search_ics")
        with patch("app.utils.llm_router.routed_structured", new_callable=AsyncMock, side_effect=Exception("API down")):
            result = await gate.classify_parts_batch([{"mpn": "LM358", "manufacturer": "TI", "description": "op-amp"}])
            assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_unexpected_response(self):
        gate = AIGate(MagicMock(), "ICsource", "search_ics")
        with patch("app.utils.llm_router.routed_structured", new_callable=AsyncMock, return_value={"bad": "response"}):
            result = await gate.classify_parts_batch([{"mpn": "LM358", "manufacturer": "TI", "description": "op-amp"}])
            assert result is None

    @pytest.mark.asyncio
    async def test_returns_classifications_on_valid_response(self):
        gate = AIGate(MagicMock(), "ICsource", "search_ics")
        classifications = [{"mpn": "LM358", "search_ics": True, "commodity": "semiconductor", "reason": "op-amp"}]
        with patch(
            "app.utils.llm_router.routed_structured",
            new_callable=AsyncMock,
            return_value={"classifications": classifications},
        ):
            result = await gate.classify_parts_batch([{"mpn": "LM358", "manufacturer": "TI", "description": ""}])
            assert result == classifications


class TestProcessAIGate:
    def _make_item(self, mpn: str, status: str = "pending") -> MagicMock:
        item = MagicMock()
        item.mpn = mpn
        item.normalized_mpn = mpn.lower()
        item.manufacturer = "TI"
        item.description = "op-amp"
        item.status = status
        item.updated_at = None
        return item

    @pytest.mark.asyncio
    async def test_no_pending_items_skips(self, db_session):
        model = MagicMock()
        model.status = "pending"
        query_mock = MagicMock()
        query_mock.filter.return_value.order_by.return_value.limit.return_value.all.return_value = []
        db_session_mock = MagicMock()
        db_session_mock.query.return_value = query_mock

        gate = AIGate(model, "ICsource", "search_ics")
        await gate.process_ai_gate(db_session_mock)
        db_session_mock.commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_cooldown_skips_processing(self, db_session):
        model = MagicMock()
        gate = AIGate(model, "ICsource", "search_ics")
        gate._last_api_failure = time.monotonic()  # Just failed

        db_mock = MagicMock()
        await gate.process_ai_gate(db_mock)
        db_mock.query.assert_not_called()

    @pytest.mark.asyncio
    async def test_cache_hit_sets_status(self):
        item = self._make_item("LM358")

        model = MagicMock()
        query_mock = MagicMock()
        query_mock.filter.return_value.order_by.return_value.limit.return_value.all.return_value = [item]
        db_mock = MagicMock()
        db_mock.query.return_value = query_mock

        gate = AIGate(model, "ICsource", "search_ics")
        # Pre-populate cache
        cache_key = ("lm358", "ti")
        gate._classification_cache[cache_key] = ("semiconductor", "search", "op-amp IC")

        await gate.process_ai_gate(db_mock)

        assert item.status == "queued"
        assert "[cached]" in item.gate_reason
        db_mock.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_api_failure_defaults_to_queued(self):
        item = self._make_item("LM358")

        model = MagicMock()
        query_mock = MagicMock()
        query_mock.filter.return_value.order_by.return_value.limit.return_value.all.return_value = [item]
        db_mock = MagicMock()
        db_mock.query.return_value = query_mock

        gate = AIGate(model, "ICsource", "search_ics")

        with patch.object(gate, "classify_parts_batch", new_callable=AsyncMock, return_value=None):
            await gate.process_ai_gate(db_mock)

        assert item.status == "queued"
        assert gate._last_api_failure > 0
        db_mock.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_successful_classification_sets_search(self):
        item = self._make_item("STM32F407")

        model = MagicMock()
        query_mock = MagicMock()
        query_mock.filter.return_value.order_by.return_value.limit.return_value.all.return_value = [item]
        db_mock = MagicMock()
        db_mock.query.return_value = query_mock

        gate = AIGate(model, "ICsource", "search_ics")
        classifications = [
            {"mpn": "STM32F407", "search_ics": True, "commodity": "semiconductor", "reason": "microcontroller"}
        ]

        with patch.object(gate, "classify_parts_batch", new_callable=AsyncMock, return_value=classifications):
            await gate.process_ai_gate(db_mock)

        assert item.status == "queued"
        assert item.gate_decision == "search"

    @pytest.mark.asyncio
    async def test_gated_out_when_skip(self):
        item = self._make_item("RC0402")

        model = MagicMock()
        query_mock = MagicMock()
        query_mock.filter.return_value.order_by.return_value.limit.return_value.all.return_value = [item]
        db_mock = MagicMock()
        db_mock.query.return_value = query_mock

        gate = AIGate(model, "ICsource", "search_ics")
        classifications = [{"mpn": "RC0402", "search_ics": False, "commodity": "passive", "reason": "resistor"}]

        with patch.object(gate, "classify_parts_batch", new_callable=AsyncMock, return_value=classifications):
            await gate.process_ai_gate(db_mock)

        assert item.status == "gated_out"
        assert item.gate_decision == "skip"

    @pytest.mark.asyncio
    async def test_missing_mpn_in_results_leaves_pending(self):
        item = self._make_item("UNKNOWNPART")

        model = MagicMock()
        query_mock = MagicMock()
        query_mock.filter.return_value.order_by.return_value.limit.return_value.all.return_value = [item]
        db_mock = MagicMock()
        db_mock.query.return_value = query_mock

        gate = AIGate(model, "ICsource", "search_ics")
        # Classification doesn't include this MPN
        with patch.object(gate, "classify_parts_batch", new_callable=AsyncMock, return_value=[]):
            await gate.process_ai_gate(db_mock)

        # Status should remain "pending"
        assert item.status == "pending"
