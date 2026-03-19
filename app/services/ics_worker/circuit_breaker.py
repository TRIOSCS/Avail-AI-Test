"""Circuit breaker — stops searches on signs of blocking or errors.

Thin subclass of CircuitBreakerBase that adds the ICS-specific
check_page_health() method for browser DOM inspection.

Called by: worker loop (after each search)
Depends on: search_worker_base.circuit_breaker.CircuitBreakerBase
"""

from loguru import logger

from ..search_worker_base.circuit_breaker import CircuitBreakerBase


class CircuitBreaker(CircuitBreakerBase):
    """ICS-specific circuit breaker with browser page health checking."""

    async def check_page_health(self, page) -> str:
        """Check page content for red flags indicating detection.

        Returns a status string:
        - "HEALTHY" — all good
        - "SESSION_EXPIRED" — login page detected (normal, not an error)
        - Other values trip or accumulate toward tripping the breaker.
        """
        try:
            url = page.url
            content = await page.evaluate("() => document.body.innerText.toLowerCase().substring(0, 3000)")
        except Exception as e:
            self.consecutive_failures += 1
            if self.consecutive_failures >= 3:
                self._trip(f"3 consecutive page health check failures: {e}")
            return "CHECK_FAILED"

        # Immediate trip: unexpected redirect off ICsource domain
        if "icsource.com" not in url:
            self._trip(f"Unexpected redirect to: {url}")
            return "UNEXPECTED_REDIRECT"

        # Login page = session expired (normal, handled by session_manager)
        if "login.aspx" in url.lower() or "login" in url.lower().split("/")[-1]:
            return "SESSION_EXPIRED"

        # Captcha detection
        captcha_signals = ["captcha", "verify you are human", "are you a robot", "recaptcha"]
        if any(signal in content for signal in captcha_signals):
            self.captcha_count += 1
            logger.warning("Captcha detected (count={})", self.captcha_count)
            if self.captcha_count >= 2:
                self._trip(f"Captcha detected {self.captcha_count} times")
            return "CAPTCHA_WARNING"

        # Rate limiting
        if "too many requests" in content or "rate limit" in content:
            self._trip("Rate limited by ICsource")
            return "RATE_LIMITED"

        # Access denied
        if "access denied" in content or "blocked" in content or "unusual activity" in content:
            self._trip("Access denied or unusual activity detected")
            return "ACCESS_DENIED"

        # All clear — reset failure counters
        self.consecutive_failures = 0
        return "HEALTHY"
