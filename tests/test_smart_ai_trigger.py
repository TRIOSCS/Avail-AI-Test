"""
Tests for the smart AI trigger logic in search_service.
What: Tests should_trigger_ai_search() conditions and AI connector gating
Called by: pytest
Depends on: app.search_service.should_trigger_ai_search
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.search_service import should_trigger_ai_search


# ── Unit tests for should_trigger_ai_search() ─────────────────────


class TestShouldTriggerAISearch:
    """Pure logic tests — no DB or async needed."""

    def test_trigger_few_results(self):
        """<5 API results should trigger AI search."""
        assert should_trigger_ai_search(
            api_result_count=3,
            has_price_below_target=True,
            is_obsolete=False,
            months_since_last_sighting=1.0,
        ) is True

    def test_trigger_many_results(self):
        """10 results with good prices, not obsolete, recent sightings -> no trigger."""
        assert should_trigger_ai_search(
            api_result_count=10,
            has_price_below_target=True,
            is_obsolete=False,
            months_since_last_sighting=1.0,
        ) is False

    def test_trigger_no_price_below_target(self):
        """All prices above target should trigger AI search."""
        assert should_trigger_ai_search(
            api_result_count=10,
            has_price_below_target=False,
            is_obsolete=False,
            months_since_last_sighting=1.0,
        ) is True

    def test_trigger_has_price_below_target(self):
        """Some prices below target with enough results -> no trigger."""
        assert should_trigger_ai_search(
            api_result_count=10,
            has_price_below_target=True,
            is_obsolete=False,
            months_since_last_sighting=1.0,
        ) is False

    def test_trigger_obsolete(self):
        """Obsolete part should always trigger AI search."""
        assert should_trigger_ai_search(
            api_result_count=10,
            has_price_below_target=True,
            is_obsolete=True,
            months_since_last_sighting=1.0,
        ) is True

    def test_trigger_stale_sightings(self):
        """>6 months since last sighting should trigger AI search."""
        assert should_trigger_ai_search(
            api_result_count=10,
            has_price_below_target=True,
            is_obsolete=False,
            months_since_last_sighting=7.0,
        ) is True

    def test_trigger_manual(self):
        """Manual trigger always returns True regardless of other conditions."""
        assert should_trigger_ai_search(
            api_result_count=100,
            has_price_below_target=True,
            is_obsolete=False,
            months_since_last_sighting=0.1,
            manual_trigger=True,
        ) is True

    def test_trigger_no_sighting_history(self):
        """None months_since_last_sighting (no history) with enough results -> no trigger."""
        assert should_trigger_ai_search(
            api_result_count=10,
            has_price_below_target=True,
            is_obsolete=False,
            months_since_last_sighting=None,
        ) is False

    def test_trigger_exactly_5_results(self):
        """Exactly 5 results (not <5) with good conditions -> no trigger."""
        assert should_trigger_ai_search(
            api_result_count=5,
            has_price_below_target=True,
            is_obsolete=False,
            months_since_last_sighting=1.0,
        ) is False

    def test_trigger_exactly_6_months(self):
        """Exactly 6 months since last sighting -> trigger (>=6)."""
        assert should_trigger_ai_search(
            api_result_count=10,
            has_price_below_target=True,
            is_obsolete=False,
            months_since_last_sighting=6.0,
        ) is True

    def test_ai_skipped_when_rich_results(self):
        """15 results with prices below target -> AI not triggered."""
        assert should_trigger_ai_search(
            api_result_count=15,
            has_price_below_target=True,
            is_obsolete=False,
            months_since_last_sighting=2.0,
        ) is False
