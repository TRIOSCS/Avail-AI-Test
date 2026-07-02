"""tests/test_tbf_ai_gate.py — Coverage for app/services/tbf_worker/ai_gate.py.

Covers: classify_parts_batch, process_ai_gate (cache hits, API success,
API failure / fail-open, cooldown), and clear_classification_cache.

Called by: pytest
Depends on: unittest.mock (no real DB or Anthropic API calls)
"""

import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ["TESTING"] = "1"


def _make_queue_item(mpn="LM317T", normalized_mpn="lm317t", manufacturer="TI", status="pending"):
    item = MagicMock()
    item.mpn = mpn
    item.normalized_mpn = normalized_mpn
    item.manufacturer = manufacturer
    item.description = "Adjustable LDO"
    item.status = status
    item.commodity_class = None
    item.gate_decision = None
    item.gate_reason = None
    item.updated_at = None
    return item


def _mock_db(pending_items):
    db = MagicMock()
    (
        db.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value
    ) = pending_items
    return db


# ── classify_parts_batch ──────────────────────────────────────────────────────


class TestClassifyPartsBatch:
    @pytest.mark.asyncio
    async def test_empty_list_returns_empty(self):
        from app.services.tbf_worker.ai_gate import classify_parts_batch

        result = await classify_parts_batch([])
        assert result == []

    @pytest.mark.asyncio
    async def test_success_returns_classifications(self):
        from app.services.tbf_worker.ai_gate import classify_parts_batch

        fake_result = {
            "classifications": [
                {"mpn": "LM317T", "search_broker": True, "commodity": "semiconductor", "reason": "LDO regulator"}
            ]
        }
        with patch(
            "app.utils.llm_router.routed_structured",
            new=AsyncMock(return_value=fake_result),
        ):
            result = await classify_parts_batch([{"mpn": "LM317T", "manufacturer": "TI", "description": "LDO"}])

        assert len(result) == 1
        assert result[0]["mpn"] == "LM317T"
        assert result[0]["search_broker"] is True

    @pytest.mark.asyncio
    async def test_api_exception_returns_none(self):
        from app.services.tbf_worker.ai_gate import classify_parts_batch

        with patch(
            "app.utils.llm_router.routed_structured",
            new=AsyncMock(side_effect=RuntimeError("API down")),
        ):
            result = await classify_parts_batch([{"mpn": "X1", "manufacturer": "", "description": ""}])

        assert result is None

    @pytest.mark.asyncio
    async def test_unexpected_response_format_returns_none(self):
        from app.services.tbf_worker.ai_gate import classify_parts_batch

        with patch(
            "app.utils.llm_router.routed_structured",
            new=AsyncMock(return_value={"unexpected": "format"}),
        ):
            result = await classify_parts_batch([{"mpn": "X1", "manufacturer": "", "description": ""}])

        assert result is None

    @pytest.mark.asyncio
    async def test_none_response_returns_none(self):
        from app.services.tbf_worker.ai_gate import classify_parts_batch

        with patch(
            "app.utils.llm_router.routed_structured",
            new=AsyncMock(return_value=None),
        ):
            result = await classify_parts_batch([{"mpn": "X1", "manufacturer": "", "description": ""}])

        assert result is None


# ── process_ai_gate ───────────────────────────────────────────────────────────


class TestProcessAiGate:
    @pytest.mark.asyncio
    async def test_no_pending_items_returns_early(self):
        from app.services.tbf_worker.ai_gate import process_ai_gate

        db = _mock_db([])
        await process_ai_gate(db)
        db.commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_cooldown_skips_processing(self):
        import app.services.tbf_worker.ai_gate as gate

        original = gate._last_api_failure
        try:
            gate._last_api_failure = time.monotonic()  # just failed
            db = _mock_db([_make_queue_item()])
            await gate.process_ai_gate(db)
            # Should not have queried the DB (returned early in cooldown)
            db.query.assert_not_called()
        finally:
            gate._last_api_failure = original

    @pytest.mark.asyncio
    async def test_cache_hit_uses_cached_classification(self):
        import app.services.tbf_worker.ai_gate as gate
        from app.services.tbf_worker.ai_gate import clear_classification_cache, process_ai_gate

        clear_classification_cache()
        # Pre-populate cache
        with gate._cache_lock:
            gate._classification_cache[("lm317t", "ti")] = ("semiconductor", "search", "cached LDO")

        item = _make_queue_item("LM317T", "lm317t", "TI")
        db = _mock_db([item])

        with patch(
            "app.services.tbf_worker.ai_gate.classify_parts_batch",
            new=AsyncMock(),
        ) as mock_classify:
            await process_ai_gate(db)

        mock_classify.assert_not_called()
        assert item.status == "queued"
        assert "cached" in item.gate_reason
        clear_classification_cache()

    @pytest.mark.asyncio
    async def test_api_success_queues_search_items(self):
        from app.services.tbf_worker.ai_gate import clear_classification_cache, process_ai_gate

        clear_classification_cache()
        item = _make_queue_item("LM317T", "lm317t", "TI")
        db = _mock_db([item])

        classifications = [
            {"mpn": "LM317T", "search_broker": True, "commodity": "semiconductor", "reason": "IC regulator"}
        ]
        with patch(
            "app.services.tbf_worker.ai_gate.classify_parts_batch",
            new=AsyncMock(return_value=classifications),
        ):
            await process_ai_gate(db)

        assert item.status == "queued"
        assert item.commodity_class == "semiconductor"
        assert item.gate_decision == "search"
        db.commit.assert_called_once()
        clear_classification_cache()

    @pytest.mark.asyncio
    async def test_api_success_gates_out_commodity_items(self):
        from app.services.tbf_worker.ai_gate import clear_classification_cache, process_ai_gate

        clear_classification_cache()
        item = _make_queue_item("RC0402", "rc0402", "Yageo")
        db = _mock_db([item])

        classifications = [
            {"mpn": "RC0402", "search_broker": False, "commodity": "passive", "reason": "Standard resistor"}
        ]
        with patch(
            "app.services.tbf_worker.ai_gate.classify_parts_batch",
            new=AsyncMock(return_value=classifications),
        ):
            await process_ai_gate(db)

        assert item.status == "gated_out"
        assert item.gate_decision == "skip"
        clear_classification_cache()

    @pytest.mark.asyncio
    async def test_api_failure_fails_open_to_queued(self):
        import app.services.tbf_worker.ai_gate as gate
        from app.services.tbf_worker.ai_gate import clear_classification_cache, process_ai_gate

        clear_classification_cache()
        original_failure = gate._last_api_failure
        try:
            item = _make_queue_item("LM317T", "lm317t", "TI")
            db = _mock_db([item])

            with patch(
                "app.services.tbf_worker.ai_gate.classify_parts_batch",
                new=AsyncMock(return_value=None),
            ):
                await process_ai_gate(db)

            assert item.status == "queued"
            assert "unavailable" in item.gate_reason.lower()
            assert gate._last_api_failure > 0
        finally:
            gate._last_api_failure = original_failure
        clear_classification_cache()

    @pytest.mark.asyncio
    async def test_missing_mpn_in_response_fails_open_to_queued(self):
        # Regression: a genuinely omitted MPN must NOT be left 'pending' — the
        # pending fetch would re-select the same poison row every cycle and
        # starve the gate. Fail open to 'queued' instead.
        from app.services.tbf_worker.ai_gate import clear_classification_cache, process_ai_gate

        clear_classification_cache()
        item = _make_queue_item("UNKNOWN", "unknown", "")
        item.status = "pending"
        db = _mock_db([item])

        with patch(
            "app.services.tbf_worker.ai_gate.classify_parts_batch",
            new=AsyncMock(return_value=[]),  # empty classifications — MPN not returned
        ):
            await process_ai_gate(db)

        assert item.status == "queued"
        assert item.commodity_class == "unknown"
        assert item.gate_decision == "search"
        assert "no classification" in item.gate_reason.lower()
        clear_classification_cache()

    @pytest.mark.asyncio
    async def test_case_whitespace_shifted_mpn_still_matches(self):
        # Regression: the model echoes the MPN with different case/whitespace
        # (or punctuation). Normalized lookup on BOTH sides must still match so
        # the item is classified, not left 'pending'.
        from app.services.tbf_worker.ai_gate import clear_classification_cache, process_ai_gate

        clear_classification_cache()
        shifted = _make_queue_item("LM317T", "lm317t", "TI")
        shifted.status = "pending"
        omitted = _make_queue_item("STM32F407", "stm32f407", "ST")
        omitted.status = "pending"
        db = _mock_db([shifted, omitted])

        classifications = [
            # Model echoes the mpn case/whitespace-shifted for the first item…
            {"mpn": " lm317t ", "search_broker": True, "commodity": "semiconductor", "reason": "LDO"},
            # …and OMITS the second item entirely.
        ]
        with patch(
            "app.services.tbf_worker.ai_gate.classify_parts_batch",
            new=AsyncMock(return_value=classifications),
        ):
            await process_ai_gate(db)

        # Shifted item matched via normalization → classified, not pending.
        assert shifted.status == "queued"
        assert shifted.gate_decision == "search"
        assert shifted.commodity_class == "semiconductor"
        # Omitted item failed open → queued, not pending.
        assert omitted.status == "queued"
        assert omitted.gate_decision == "search"
        assert omitted.commodity_class == "unknown"
        db.commit.assert_called_once()
        clear_classification_cache()


# ── clear_classification_cache ────────────────────────────────────────────────


def test_clear_classification_cache_empties_cache():
    import app.services.tbf_worker.ai_gate as gate

    with gate._cache_lock:
        gate._classification_cache[("test", "mfr")] = ("passive", "skip", "test")

    from app.services.tbf_worker.ai_gate import clear_classification_cache

    clear_classification_cache()
    with gate._cache_lock:
        assert len(gate._classification_cache) == 0
