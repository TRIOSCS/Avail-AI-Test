"""Tests for scripts/enrich_batch.py — Batch API enrichment logic.

Tests prompt building, request construction, and result application
without hitting real APIs.

Called by: pytest
Depends on: scripts/enrich_batch.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.enrich_batch import (
    VALID_CATEGORIES,
    _build_prompt,
    _build_batch_requests,
)


# ── VALID_CATEGORIES tests ────────────────────────────────────────────


class TestValidCategories:
    def test_contains_granular_categories(self):
        assert "dram" in VALID_CATEGORIES
        assert "cpu" in VALID_CATEGORIES
        assert "capacitors" in VALID_CATEGORIES
        assert "motherboards" in VALID_CATEGORIES

    def test_no_coarse_categories(self):
        assert "memory" not in VALID_CATEGORIES
        assert "processors" not in VALID_CATEGORIES
        assert "servers" not in VALID_CATEGORIES
        assert "storage" not in VALID_CATEGORIES

    def test_includes_other(self):
        assert "other" in VALID_CATEGORIES

    def test_has_45_categories(self):
        # 45 granular categories from the faceted search design spec
        assert len(VALID_CATEGORIES) >= 45


# ── _build_prompt tests ──────────────────────────────────────────────


class TestBuildPrompt:
    def test_basic_prompt(self):
        cards = [{"display_mpn": "STM32F103C8T6", "manufacturer": "STMicroelectronics", "description": None}]
        prompt = _build_prompt(cards)
        assert "STM32F103C8T6" in prompt
        assert "STMicroelectronics" in prompt
        assert "Classify" in prompt

    def test_includes_context(self):
        cards = [{"display_mpn": "LM317T", "manufacturer": "TI", "description": "Voltage regulator IC"}]
        prompt = _build_prompt(cards)
        assert "Voltage regulator IC" in prompt
        assert "Context:" in prompt

    def test_no_manufacturer(self):
        cards = [{"display_mpn": "ABC123", "manufacturer": None, "description": None}]
        prompt = _build_prompt(cards)
        assert "ABC123" in prompt
        assert "Manufacturer" not in prompt

    def test_multiple_cards(self):
        cards = [
            {"display_mpn": "MPN1", "manufacturer": None, "description": None},
            {"display_mpn": "MPN2", "manufacturer": None, "description": None},
        ]
        prompt = _build_prompt(cards)
        assert "MPN1" in prompt
        assert "MPN2" in prompt

    def test_categories_in_prompt(self):
        cards = [{"display_mpn": "X", "manufacturer": None, "description": None}]
        prompt = _build_prompt(cards)
        assert "dram" in prompt
        assert "capacitors" in prompt


# ── _build_batch_requests tests ───────────────────────────────────────


class TestBuildBatchRequests:
    def test_single_batch(self):
        cards = [{"id": i, "display_mpn": f"MPN{i}", "manufacturer": None, "description": None} for i in range(10)]
        requests = _build_batch_requests(cards)
        assert len(requests) == 1
        assert requests[0]["model_tier"] == "smart"

    def test_multiple_batches(self):
        cards = [{"id": i, "display_mpn": f"MPN{i}", "manufacturer": None, "description": None} for i in range(120)]
        requests = _build_batch_requests(cards)
        # 120 cards / 50 per batch = 3 batches
        assert len(requests) == 3

    def test_custom_ids_unique(self):
        cards = [{"id": i, "display_mpn": f"MPN{i}", "manufacturer": None, "description": None} for i in range(120)]
        requests = _build_batch_requests(cards)
        custom_ids = [r["custom_id"] for r in requests]
        assert len(custom_ids) == len(set(custom_ids))

    def test_request_has_required_fields(self):
        cards = [{"id": 1, "display_mpn": "TEST", "manufacturer": None, "description": None}]
        requests = _build_batch_requests(cards)
        req = requests[0]
        assert "custom_id" in req
        assert "prompt" in req
        assert "schema" in req
        assert "system" in req
        assert "model_tier" in req
        assert "max_tokens" in req
