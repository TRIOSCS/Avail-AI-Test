"""Circuit breaker — stops searches on signs of blocking or errors.

Shared base class containing the state machine logic (trip, reset,
counters, empty results tracking) used by all search workers. Each
worker subclass adds its own health check method:
- ICS: async check_page_health(page) — browser DOM inspection
- NC: check_response_health(status_code, html, url) — HTTP response

Called by: worker loop (after each search)
Depends on: nothing (state machine)
"""

from loguru import logger


class CircuitBreakerBase:
    """Base circuit breaker with shared state machine logic.

    Subclasses should add a health check method appropriate for their transport (browser
    page, HTTP response, etc.).
    """

    def __init__(self):
        self.consecutive_failures = 0
        self.captcha_count = 0
        self.empty_results_streak = 0
        self.is_open = False
        self.trip_reason = ""

    def _trip(self, reason: str):
        """Trip the circuit breaker — all searches stop."""
        self.is_open = True
        self.trip_reason = reason
        logger.critical("CIRCUIT BREAKER TRIPPED: {}", reason)

    def record_empty_results(self):
        """Track consecutive empty results — many in a row may indicate shadow-
        blocking."""
        self.empty_results_streak += 1
        if self.empty_results_streak >= 10:
            self._trip("10 consecutive empty results — possible shadow-block")

    def record_results(self):
        """Reset empty results streak on successful result."""
        self.empty_results_streak = 0

    def should_stop(self) -> bool:
        """Return True if the circuit breaker is open (all searches should stop)."""
        return self.is_open

    def get_trip_info(self) -> dict:
        """Return circuit breaker state for monitoring."""
        return {
            "is_open": self.is_open,
            "trip_reason": self.trip_reason,
            "captcha_count": self.captcha_count,
            "consecutive_failures": self.consecutive_failures,
            "empty_results_streak": self.empty_results_streak,
        }

    def reset(self):
        """Manually reset the circuit breaker (admin action)."""
        self.is_open = False
        self.trip_reason = ""
        self.captcha_count = 0
        self.consecutive_failures = 0
        self.empty_results_streak = 0
        logger.info("Circuit breaker manually reset")
