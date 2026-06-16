"""tests/test_search_worker_utils.py — Coverage gap tests for search_worker_base.

Targets:
- scheduler.py: is_business_hours() branches for all weekdays (lines 48-61)
- human_behavior.py: human_type loop body (line 41), human_click with bounding box (lines 48-54)

Called by: pytest
Depends on: app/services/search_worker_base/scheduler.py, human_behavior.py
"""

import os

os.environ["TESTING"] = "1"

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestSearchSchedulerIsBusinessHours:
    """SearchScheduler.is_business_hours() covers all weekday branches (lines 48-61)."""

    def _make_scheduler(self):
        from types import SimpleNamespace

        from app.services.search_worker_base.config import build_worker_config
        from app.services.search_worker_base.scheduler import SearchScheduler

        config_dict = build_worker_config("ICS")
        config = SimpleNamespace(**config_dict)
        return SearchScheduler(config, "ICS")

    @pytest.mark.parametrize(
        ("weekday", "hour", "expected"),
        [
            pytest.param(5, 12, False, id="saturday_always_off"),
            pytest.param(6, 10, False, id="sunday_before_6pm_off"),
            pytest.param(6, 18, True, id="sunday_after_6pm_on"),
            pytest.param(4, 16, True, id="friday_before_5pm_on"),
            pytest.param(4, 17, False, id="friday_at_5pm_off"),
            pytest.param(0, 9, True, id="monday_always_on"),
            pytest.param(1, 14, True, id="tuesday_always_on"),
            pytest.param(2, 11, True, id="wednesday_always_on"),
            pytest.param(3, 15, True, id="thursday_always_on"),
        ],
    )
    def test_is_business_hours_by_weekday(self, weekday, hour, expected):
        """is_business_hours() resolves each weekday/hour combination correctly."""
        scheduler = self._make_scheduler()
        with patch("app.services.search_worker_base.scheduler.datetime") as mock_dt:
            mock_dt.now.return_value = MagicMock(weekday=lambda: weekday, hour=hour)
            result = scheduler.is_business_hours()
        assert result is expected

    def test_force_business_hours_env_var(self):
        """FORCE_BUSINESS_HOURS=1 always returns True."""
        scheduler = self._make_scheduler()
        with patch.dict(os.environ, {"FORCE_BUSINESS_HOURS": "1"}):
            result = scheduler.is_business_hours()
        assert result is True

    def test_next_delay_increments_searches_since_break(self):
        """next_delay() increments searches_since_break counter."""
        scheduler = self._make_scheduler()
        initial = scheduler.searches_since_break
        scheduler.next_delay()
        assert scheduler.searches_since_break == initial + 1

    def test_time_for_break_when_threshold_reached(self):
        """time_for_break() returns True when threshold is exceeded."""
        scheduler = self._make_scheduler()
        scheduler.searches_since_break = 100  # Way over any threshold
        assert scheduler.time_for_break() is True

    def test_time_for_break_not_reached(self):
        """time_for_break() returns False before threshold."""
        scheduler = self._make_scheduler()
        scheduler.searches_since_break = 0
        assert scheduler.time_for_break() is False

    def test_get_break_duration_range(self):
        """get_break_duration() returns value between 5-25 minutes."""
        scheduler = self._make_scheduler()
        duration = scheduler.get_break_duration()
        assert 5 * 60 <= duration <= 25 * 60

    def test_reset_break_counter(self):
        """reset_break_counter() resets counter and picks new threshold."""
        scheduler = self._make_scheduler()
        scheduler.searches_since_break = 50
        scheduler.reset_break_counter()
        assert scheduler.searches_since_break == 0
        assert 8 <= scheduler.break_threshold <= 15


class TestHumanBehaviorHumanType:
    """HumanBehavior.human_type() typing loop (line 41)."""

    @pytest.mark.parametrize(
        ("text", "expected_calls"),
        [
            pytest.param("AB", 2, id="types_each_character"),
            pytest.param("", 0, id="empty_string"),
            pytest.param("X", 1, id="single_char"),
        ],
    )
    async def test_human_type_calls_keyboard_per_character(self, text, expected_calls):
        """human_type() calls page.keyboard.type once per character."""
        from app.services.search_worker_base.human_behavior import HumanBehavior

        page = MagicMock()
        page.keyboard = MagicMock()
        page.keyboard.type = AsyncMock()

        locator = MagicMock()
        locator.click = AsyncMock()

        with patch("asyncio.sleep", new=AsyncMock()):
            await HumanBehavior.human_type(page, locator, text)

        assert page.keyboard.type.call_count == expected_calls


class TestHumanBehaviorHumanClick:
    """HumanBehavior.human_click() with and without bounding box (lines 48-54)."""

    async def test_human_click_with_bounding_box(self):
        """human_click() uses mouse.click at randomized position within box."""
        from app.services.search_worker_base.human_behavior import HumanBehavior

        page = MagicMock()
        page.mouse = MagicMock()
        page.mouse.click = AsyncMock()

        locator = MagicMock()
        locator.bounding_box = AsyncMock(return_value={"x": 100, "y": 200, "width": 50, "height": 30})

        await HumanBehavior.human_click(page, locator)

        page.mouse.click.assert_called_once()
        # Click position should be within the box
        call_args = page.mouse.click.call_args[0]
        x, y = call_args[0], call_args[1]
        assert 100 <= x <= 150
        assert 200 <= y <= 230

    async def test_human_click_without_bounding_box_falls_back(self):
        """human_click() falls back to locator.click when no bounding box."""
        from app.services.search_worker_base.human_behavior import HumanBehavior

        page = MagicMock()
        page.mouse = MagicMock()
        page.mouse.click = AsyncMock()

        locator = MagicMock()
        locator.bounding_box = AsyncMock(return_value=None)
        locator.click = AsyncMock()

        await HumanBehavior.human_click(page, locator)

        locator.click.assert_called_once()
        page.mouse.click.assert_not_called()

    async def test_random_delay_within_bounds(self):
        """random_delay() sleeps for a duration within [min_sec, max_sec]."""
        from app.services.search_worker_base.human_behavior import HumanBehavior

        sleep_calls = []

        async def mock_sleep(duration):
            sleep_calls.append(duration)

        with patch("asyncio.sleep", new=mock_sleep):
            await HumanBehavior.random_delay(0.5, 1.5)

        assert len(sleep_calls) == 1
        assert 0.5 <= sleep_calls[0] <= 1.5
