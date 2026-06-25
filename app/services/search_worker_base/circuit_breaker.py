"""Circuit breaker — stops searches on signs of blocking or errors.

Shared base class containing the state machine logic (trip, reset,
counters, empty results tracking) used by all search workers. Each
worker subclass adds its own health check method:
- ICS: async check_page_health(page) — browser DOM inspection
- NC: check_response_health(status_code, html, url) — HTTP response

Called by: worker loop (after each search)
Depends on: nothing (state machine)
"""

import time

from loguru import logger


class CircuitBreakerBase:
    """Base circuit breaker with shared state machine logic.

    Subclasses should add a health check method appropriate for their transport (browser
    page, HTTP response, etc.).

    Self-healing: once tripped, ``should_stop()`` auto-resets after ``cooldown_seconds``
    so a transient block (captcha, rate-limit) doesn't wedge the worker until a restart.
    """

    def __init__(self, cooldown_seconds: float = 1800):
        self.consecutive_failures = 0
        self.captcha_count = 0
        self.empty_results_streak = 0
        self.is_open = False
        self.trip_reason = ""
        self._cooldown_seconds = cooldown_seconds
        self._tripped_at: float | None = None

    def _trip(self, reason: str):
        """Trip the circuit breaker — all searches stop until the cooldown elapses."""
        self.is_open = True
        self.trip_reason = reason
        self._tripped_at = time.monotonic()
        logger.critical("CIRCUIT BREAKER TRIPPED: {} (auto-reset in {:.0f}m)", reason, self._cooldown_seconds / 60)

    def _reset(self):
        """Clear breaker state (called after the cooldown elapses)."""
        self.is_open = False
        self.trip_reason = ""
        self.consecutive_failures = 0
        self.captcha_count = 0
        self.empty_results_streak = 0
        self._tripped_at = None

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
        """Return True if the breaker is open and the cooldown has not yet elapsed.

        Auto-resets (self-heals) once ``cooldown_seconds`` have passed since the trip.
        """
        if not self.is_open:
            return False
        if self._tripped_at is not None and time.monotonic() - self._tripped_at >= self._cooldown_seconds:
            logger.info("CIRCUIT BREAKER: cooldown elapsed — auto-resetting")
            self._reset()
            return False
        return True

    def get_trip_info(self) -> dict:
        """Return circuit breaker state for monitoring."""
        return {
            "is_open": self.is_open,
            "trip_reason": self.trip_reason,
            "captcha_count": self.captcha_count,
            "consecutive_failures": self.consecutive_failures,
            "empty_results_streak": self.empty_results_streak,
        }
