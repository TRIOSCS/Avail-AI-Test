"""Tests for NC Phase 6: Worker Loop, Scheduler, Circuit Breaker.

Called by: pytest
Depends on: conftest.py, nc_worker modules
"""

from unittest.mock import MagicMock, patch

import pytest

from app.services.nc_worker.circuit_breaker import CircuitBreaker
from app.services.nc_worker.config import NcConfig
from app.services.nc_worker.scheduler import SearchScheduler

# ── Scheduler Tests ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("weekday", "hour", "expected"),
    [
        pytest.param(1, 10, True, id="weekday_within_range"),  # Tuesday 10 AM
        pytest.param(5, 10, False, id="weekend"),  # Saturday 10 AM
        pytest.param(4, 22, False, id="outside_hours"),  # Friday 10 PM (after 5 PM cutoff)
    ],
)
def test_scheduler_is_business_hours(weekday, hour, expected):
    """Business hours check honors weekday and configured hour range."""
    sched = SearchScheduler(NcConfig())

    with patch("app.services.nc_worker.scheduler.datetime") as mock_dt:
        mock_now = MagicMock()
        mock_now.weekday.return_value = weekday
        mock_now.hour = hour
        mock_dt.now.return_value = mock_now
        assert sched.is_business_hours() is expected


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

    result = breaker.check_response_health(
        200,
        "search results for stm32f103",
        "https://www.netcomponents.com/search/result",
    )

    assert result == "HEALTHY"
    assert breaker.consecutive_failures == 0
    assert not breaker.is_open


def test_breaker_captcha_detection():
    """Captcha detected once = warning, twice = trip."""
    breaker = CircuitBreaker()

    result = breaker.check_response_health(
        200,
        "please complete the captcha to continue",
        "https://www.netcomponents.com/verify",
    )
    assert result == "CAPTCHA_WARNING"
    assert not breaker.is_open  # First time, just warning

    result = breaker.check_response_health(
        200,
        "please complete the captcha to continue",
        "https://www.netcomponents.com/verify",
    )
    assert result == "CAPTCHA_WARNING"
    assert breaker.is_open  # Second time, tripped


@pytest.mark.parametrize(
    ("body", "url", "expected_result", "expected_open"),
    [
        pytest.param(
            "too many requests. please try again later.",
            "https://www.netcomponents.com/error",
            "RATE_LIMITED",
            True,
            id="rate_limited_trips",
        ),
        pytest.param(
            "please log in",
            "https://www.netcomponents.com/account/login",
            "SESSION_EXPIRED",
            False,
            id="session_expired_no_trip",
        ),
    ],
)
def test_breaker_check_response_health(body, url, expected_result, expected_open):
    """Rate limiting trips the breaker; a login page is a normal session expiry (no
    trip)."""
    breaker = CircuitBreaker()

    result = breaker.check_response_health(200, body, url)

    assert result == expected_result
    assert bool(breaker.is_open) is expected_open


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
