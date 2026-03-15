"""Circuit breaker — stops searches on signs of blocking or errors.

Monitors HTTP responses for captchas, redirects, rate limiting, and
other signs that NC has detected automated access. Trips immediately
on high-severity signals, accumulates for lower-severity ones.

Called by: worker loop (after each search)
Depends on: nothing (HTTP response analysis)
"""

from loguru import logger


class CircuitBreaker:
    """Monitors for signs of blocking and stops all searches if triggered."""

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

    def check_response_health(self, status_code: int, html: str, url: str) -> str:
        """Check HTTP response for red flags indicating detection.

        Returns a status string:
        - "HEALTHY" — all good
        - "SESSION_EXPIRED" — login page detected (normal, not an error)
        - Other values trip or accumulate toward tripping the breaker.
        """
        # Login redirect = session expired (normal, handled by session_manager)
        if "/account/login" in url.lower():
            return "SESSION_EXPIRED"

        # HTTP error codes
        if status_code == 429:
            self._trip("HTTP 429 — rate limited by NetComponents")
            return "RATE_LIMITED"
        if status_code == 403:
            self._trip("HTTP 403 — access denied")
            return "ACCESS_DENIED"
        if status_code >= 500:
            self.consecutive_failures += 1
            if self.consecutive_failures >= 3:
                self._trip(f"3 consecutive server errors (last: {status_code})")
            return "SERVER_ERROR"

        # Content-based detection (check lowercase first 3000 chars)
        content = html[:3000].lower() if html else ""

        # Captcha detection
        captcha_signals = ["captcha", "verify you are human", "are you a robot", "recaptcha"]
        if any(signal in content for signal in captcha_signals):
            self.captcha_count += 1
            logger.warning("Captcha detected (count={})", self.captcha_count)
            if self.captcha_count >= 2:
                self._trip(f"Captcha detected {self.captcha_count} times")
            return "CAPTCHA_WARNING"

        # Rate limiting in content
        if "too many requests" in content or "rate limit" in content:
            self._trip("Rate limited by NetComponents (content)")
            return "RATE_LIMITED"

        # Access denied in content
        if "access denied" in content or "unusual activity" in content:
            self._trip("Access denied or unusual activity detected")
            return "ACCESS_DENIED"

        # All clear — reset failure counters
        self.consecutive_failures = 0
        return "HEALTHY"

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
