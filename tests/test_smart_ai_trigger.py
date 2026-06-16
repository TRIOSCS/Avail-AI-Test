"""Tests for the smart AI trigger logic in search_service.

What: Tests should_trigger_ai_search() conditions and AI connector gating
Called by: pytest
Depends on: app.search_service.should_trigger_ai_search
"""

import pytest

from app.search_service import should_trigger_ai_search

# ── Unit tests for should_trigger_ai_search() ─────────────────────


class TestShouldTriggerAISearch:
    """Pure logic tests — no DB or async needed."""

    @pytest.mark.parametrize(
        (
            "api_result_count",
            "has_price_below_target",
            "is_obsolete",
            "months_since_last_sighting",
            "manual_trigger",
            "expected",
        ),
        [
            # <5 API results should trigger AI search.
            pytest.param(3, True, False, 1.0, False, True, id="few_results"),
            # 10 results with good prices, not obsolete, recent sightings -> no trigger.
            pytest.param(10, True, False, 1.0, False, False, id="many_results"),
            # All prices above target should trigger AI search.
            pytest.param(10, False, False, 1.0, False, True, id="no_price_below_target"),
            # Some prices below target with enough results -> no trigger.
            pytest.param(10, True, False, 1.0, False, False, id="has_price_below_target"),
            # Obsolete part should always trigger AI search.
            pytest.param(10, True, True, 1.0, False, True, id="obsolete"),
            # >6 months since last sighting should trigger AI search.
            pytest.param(10, True, False, 7.0, False, True, id="stale_sightings"),
            # Manual trigger always returns True regardless of other conditions.
            pytest.param(100, True, False, 0.1, True, True, id="manual"),
            # None months_since_last_sighting (no history) with enough results -> no trigger.
            pytest.param(10, True, False, None, False, False, id="no_sighting_history"),
            # Exactly 5 results (not <5) with good conditions -> no trigger.
            pytest.param(5, True, False, 1.0, False, False, id="exactly_5_results"),
            # Exactly 6 months since last sighting -> trigger (>=6).
            pytest.param(10, True, False, 6.0, False, True, id="exactly_6_months"),
            # 15 results with prices below target -> AI not triggered.
            pytest.param(15, True, False, 2.0, False, False, id="ai_skipped_when_rich_results"),
        ],
    )
    def test_should_trigger_ai_search(
        self,
        api_result_count,
        has_price_below_target,
        is_obsolete,
        months_since_last_sighting,
        manual_trigger,
        expected,
    ):
        assert (
            should_trigger_ai_search(
                api_result_count=api_result_count,
                has_price_below_target=has_price_below_target,
                is_obsolete=is_obsolete,
                months_since_last_sighting=months_since_last_sighting,
                manual_trigger=manual_trigger,
            )
            is expected
        )
