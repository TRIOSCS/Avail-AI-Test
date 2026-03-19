"""Circuit breaker — stops searches on signs of blocking or errors.

Thin subclass of CircuitBreakerBase that adds the NC-specific
check_response_health() method for HTTP response inspection.

Called by: worker loop (after each search)
Depends on: search_worker_base.circuit_breaker.CircuitBreakerBase
"""

from loguru import logger

from ..search_worker_base.circuit_breaker import CircuitBreakerBase


class CircuitBreaker(CircuitBreakerBase):
    """NC-specific circuit breaker with HTTP response health checking."""

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
