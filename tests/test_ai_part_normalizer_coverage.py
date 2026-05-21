"""test_ai_part_normalizer_coverage.py — Coverage tests for uncovered branches.

Covers ClaudeUnavailableError, ClaudeError, dict response, all-cached path,
_validate_result edge cases, and clear_cache in app/services/ai_part_normalizer.py.

Called by: pytest
Depends on: app/services/ai_part_normalizer.py
"""

import os

os.environ["TESTING"] = "1"

from unittest.mock import AsyncMock, patch

import pytest

from app.utils.claude_errors import ClaudeError, ClaudeUnavailableError


class TestCallNormalizerExceptions:
    """Tests for exception paths in _call_normalizer."""

    @pytest.mark.asyncio
    async def test_claude_unavailable_returns_none(self):
        """ClaudeUnavailableError → returns None."""
        from app.services.ai_part_normalizer import _call_normalizer

        with patch(
            "app.services.ai_part_normalizer.claude_json",
            new_callable=AsyncMock,
            side_effect=ClaudeUnavailableError("not configured"),
        ):
            result = await _call_normalizer(["LM317T"])

        assert result is None

    @pytest.mark.asyncio
    async def test_claude_error_returns_none(self):
        """ClaudeError → returns None."""
        from app.services.ai_part_normalizer import _call_normalizer

        with patch(
            "app.services.ai_part_normalizer.claude_json",
            new_callable=AsyncMock,
            side_effect=ClaudeError("API failed"),
        ):
            result = await _call_normalizer(["LM317T"])

        assert result is None


class TestCallNormalizerDictResponse:
    """Tests for dict response format in _call_normalizer."""

    @pytest.mark.asyncio
    async def test_dict_with_parts_key_returns_list(self):
        """Dict response with 'parts' key → returns the list."""
        from app.services.ai_part_normalizer import _call_normalizer

        parts_list = [{"original": "LM317T", "normalized": "LM317T", "confidence": 0.95}]

        with patch(
            "app.services.ai_part_normalizer.claude_json",
            new_callable=AsyncMock,
            return_value={"parts": parts_list},
        ):
            result = await _call_normalizer(["LM317T"])

        assert result == parts_list

    @pytest.mark.asyncio
    async def test_dict_with_results_key_returns_list(self):
        """Dict response with 'results' key → returns the list."""
        from app.services.ai_part_normalizer import _call_normalizer

        results_list = [{"original": "LM317T", "normalized": "LM317T", "confidence": 0.90}]

        with patch(
            "app.services.ai_part_normalizer.claude_json",
            new_callable=AsyncMock,
            return_value={"results": results_list},
        ):
            result = await _call_normalizer(["LM317T"])

        assert result == results_list

    @pytest.mark.asyncio
    async def test_dict_without_matching_key_returns_none(self):
        """Dict response with no known key → logs warning and returns None."""
        from app.services.ai_part_normalizer import _call_normalizer

        with patch(
            "app.services.ai_part_normalizer.claude_json",
            new_callable=AsyncMock,
            return_value={"unexpected_key": [{"foo": "bar"}]},
        ):
            result = await _call_normalizer(["LM317T"])

        assert result is None


class TestNormalizePartsAllCached:
    """Tests for the all-cached fast-path in normalize_parts."""

    @pytest.mark.asyncio
    async def test_all_inputs_cached_returns_immediately(self):
        """When all parts are in cache, returns results without calling Claude."""
        import app.services.ai_part_normalizer as mod

        cached_entry = {
            "original": "LM317T",
            "normalized": "LM317T",
            "manufacturer": "Texas Instruments",
            "base_part": "LM317",
            "package_code": "T",
            "is_alias": False,
            "confidence": 0.95,
        }
        # Seed the cache
        mod._cache["LM317T"] = cached_entry

        with patch(
            "app.services.ai_part_normalizer.claude_json",
            new_callable=AsyncMock,
        ) as mock_claude:
            result = await mod.normalize_parts(["LM317T"])

        mock_claude.assert_not_called()
        assert result == [cached_entry]

        # Clean up cache entry we added
        mod._cache.pop("LM317T", None)


class TestValidateResult:
    """Tests for _validate_result edge cases."""

    def test_non_dict_parsed_calls_fallback(self):
        """Non-dict parsed value → returns fallback result."""
        from app.services.ai_part_normalizer import _validate_result

        result = _validate_result("LM317T", "not-a-dict")
        assert result["original"] == "LM317T"
        assert result["confidence"] == 0.0  # fallback sets 0.0

    def test_invalid_confidence_type_uses_default(self):
        """TypeError from float() conversion → uses 0.5 default."""
        from app.services.ai_part_normalizer import _validate_result

        # confidence as a non-numeric object that raises TypeError on float()
        parsed = {
            "original": "LM317T",
            "normalized": "LM317T",
            "confidence": [1, 2, 3],  # list — float() raises TypeError
        }
        result = _validate_result("LM317T", parsed)
        # 0.5 < CONFIDENCE_THRESHOLD (0.7) → falls back
        assert result["original"] == "LM317T"
        assert result["confidence"] == 0.5


class TestClearCache:
    """Tests for the clear_cache function."""

    def test_clear_cache_returns_count(self):
        """clear_cache() returns the number of entries cleared."""
        import app.services.ai_part_normalizer as mod

        mod._cache["TEST_PART_A"] = {"original": "TEST_PART_A"}
        mod._cache["TEST_PART_B"] = {"original": "TEST_PART_B"}
        count = mod.clear_cache()
        assert count >= 2
        assert mod._cache == {}

    def test_clear_cache_empty_cache_returns_zero(self):
        """clear_cache() on already-empty cache returns 0."""
        import app.services.ai_part_normalizer as mod

        mod._cache.clear()
        count = mod.clear_cache()
        assert count == 0
        assert mod._cache == {}
