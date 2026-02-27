"""Tests for NC Phase 6: Worker Loop, Scheduler, Circuit Breaker.

Called by: pytest
Depends on: conftest.py, nc_worker modules
"""

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.nc_worker.circuit_breaker import CircuitBreaker
from app.services.nc_worker.config import NcConfig
from app.services.nc_worker.scheduler import SearchScheduler


# ── Scheduler Tests ──────────────────────────────────────────────────


def test_scheduler_is_business_hours_weekday():
    """Business hours check works for weekday within range."""
    cfg = NcConfig()
    sched = SearchScheduler(cfg)

    # Mock a Tuesday at 10 AM Eastern
    with patch("app.services.nc_worker.scheduler.datetime") as mock_dt:
        mock_now = MagicMock()
        mock_now.weekday.return_value = 1  # Tuesday
        mock_now.hour = 10
        mock_dt.now.return_value = mock_now
        assert sched.is_business_hours() is True


def test_scheduler_is_business_hours_weekend():
    """Business hours are False on weekends."""
    cfg = NcConfig()
    sched = SearchScheduler(cfg)

    with patch("app.services.nc_worker.scheduler.datetime") as mock_dt:
        mock_now = MagicMock()
        mock_now.weekday.return_value = 5  # Saturday
        mock_now.hour = 10
        mock_dt.now.return_value = mock_now
        assert sched.is_business_hours() is False


def test_scheduler_is_business_hours_outside():
    """Business hours are False outside the configured range."""
    cfg = NcConfig()
    sched = SearchScheduler(cfg)

    with patch("app.services.nc_worker.scheduler.datetime") as mock_dt:
        mock_now = MagicMock()
        mock_now.weekday.return_value = 2  # Wednesday
        mock_now.hour = 22  # 10 PM
        mock_dt.now.return_value = mock_now
        assert sched.is_business_hours() is False


def test_scheduler_next_delay_within_bounds():
    """next_delay stays within configured min/max over 100 iterations."""
    cfg = NcConfig()
    sched = SearchScheduler(cfg)
    for _ in range(100):
        delay = sched.next_delay()
        assert cfg.NC_MIN_DELAY_SECONDS <= delay <= cfg.NC_MAX_DELAY_SECONDS


def test_scheduler_next_delay_distribution():
    """Delays cluster around the typical value (log-normal distribution)."""
    cfg = NcConfig()
    sched = SearchScheduler(cfg)
    delays = [sched.next_delay() for _ in range(100)]
    avg = sum(delays) / len(delays)
    # Average should be roughly near the typical delay (240s)
    assert 150 < avg < 350


def test_scheduler_break_threshold():
    """time_for_break triggers after threshold searches."""
    cfg = NcConfig()
    sched = SearchScheduler(cfg)
    sched.break_threshold = 3  # Force small threshold for testing

    assert not sched.time_for_break()  # 0 searches
    sched.searches_since_break = 2
    assert not sched.time_for_break()  # 2 < 3
    sched.searches_since_break = 3
    assert sched.time_for_break()  # 3 >= 3


def test_scheduler_get_break_duration():
    """Break duration is between 5-25 minutes."""
    cfg = NcConfig()
    sched = SearchScheduler(cfg)
    for _ in range(50):
        dur = sched.get_break_duration()
        assert 5 * 60 <= dur <= 25 * 60


def test_scheduler_reset_break_counter():
    """reset_break_counter resets count and picks new threshold."""
    cfg = NcConfig()
    sched = SearchScheduler(cfg)
    sched.searches_since_break = 10
    old_threshold = sched.break_threshold
    sched.reset_break_counter()
    assert sched.searches_since_break == 0
    assert 8 <= sched.break_threshold <= 15


# ── Circuit Breaker Tests ────────────────────────────────────────────


def test_breaker_healthy():
    """Healthy page returns HEALTHY and resets failure counters."""
    breaker = CircuitBreaker()
    breaker.consecutive_failures = 2

    page = MagicMock()
    page.url = "https://www.netcomponents.com/search/result"
    page.evaluate = AsyncMock(return_value="search results for stm32f103")

    loop = asyncio.new_event_loop()
    result = loop.run_until_complete(breaker.check_page_health(page))
    loop.close()

    assert result == "HEALTHY"
    assert breaker.consecutive_failures == 0
    assert not breaker.is_open


def test_breaker_captcha_detection():
    """Captcha detected once = warning, twice = trip."""
    breaker = CircuitBreaker()

    page = MagicMock()
    page.url = "https://www.netcomponents.com/verify"
    page.evaluate = AsyncMock(return_value="please complete the captcha to continue")

    loop = asyncio.new_event_loop()

    result = loop.run_until_complete(breaker.check_page_health(page))
    assert result == "CAPTCHA_WARNING"
    assert not breaker.is_open  # First time, just warning

    result = loop.run_until_complete(breaker.check_page_health(page))
    assert result == "CAPTCHA_WARNING"
    assert breaker.is_open  # Second time, tripped

    loop.close()


def test_breaker_unexpected_redirect():
    """Redirect to non-NC domain trips immediately."""
    breaker = CircuitBreaker()

    page = MagicMock()
    page.url = "https://malicious-site.com/phishing"
    page.evaluate = AsyncMock(return_value="something")

    loop = asyncio.new_event_loop()
    result = loop.run_until_complete(breaker.check_page_health(page))
    loop.close()

    assert result == "UNEXPECTED_REDIRECT"
    assert breaker.is_open


def test_breaker_rate_limited():
    """Rate limiting message trips the breaker."""
    breaker = CircuitBreaker()

    page = MagicMock()
    page.url = "https://www.netcomponents.com/error"
    page.evaluate = AsyncMock(return_value="too many requests. please try again later.")

    loop = asyncio.new_event_loop()
    result = loop.run_until_complete(breaker.check_page_health(page))
    loop.close()

    assert result == "RATE_LIMITED"
    assert breaker.is_open


def test_breaker_session_expired():
    """Login page URL = SESSION_EXPIRED (normal, not a trip)."""
    breaker = CircuitBreaker()

    page = MagicMock()
    page.url = "https://www.netcomponents.com/account/login"
    page.evaluate = AsyncMock(return_value="please log in")

    loop = asyncio.new_event_loop()
    result = loop.run_until_complete(breaker.check_page_health(page))
    loop.close()

    assert result == "SESSION_EXPIRED"
    assert not breaker.is_open  # Not a trip


def test_breaker_empty_results_streak():
    """10 consecutive empty results trips the breaker."""
    breaker = CircuitBreaker()
    for _ in range(9):
        breaker.record_empty_results()
        assert not breaker.is_open
    breaker.record_empty_results()  # 10th
    assert breaker.is_open


def test_breaker_results_reset_streak():
    """Getting results resets the empty streak."""
    breaker = CircuitBreaker()
    for _ in range(5):
        breaker.record_empty_results()
    breaker.record_results()
    assert breaker.empty_results_streak == 0


def test_breaker_should_stop():
    """should_stop reflects is_open state."""
    breaker = CircuitBreaker()
    assert not breaker.should_stop()
    breaker.is_open = True
    assert breaker.should_stop()


def test_breaker_get_trip_info():
    """get_trip_info returns complete state dict."""
    breaker = CircuitBreaker()
    breaker.captcha_count = 1
    info = breaker.get_trip_info()
    assert info["captcha_count"] == 1
    assert info["is_open"] is False


def test_breaker_reset():
    """Manual reset clears all state."""
    breaker = CircuitBreaker()
    breaker.is_open = True
    breaker.trip_reason = "test"
    breaker.captcha_count = 5
    breaker.reset()
    assert not breaker.is_open
    assert breaker.trip_reason == ""
    assert breaker.captcha_count == 0
