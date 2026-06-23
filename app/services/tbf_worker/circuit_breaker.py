"""Circuit breaker — stops searches on signs of blocking or errors.

Thin subclass of CircuitBreakerBase that adds the TBF-specific
check_page_health() method for browser DOM inspection.

TBF is a Vue SPA. The only site-specific signal verified live is the
session-expired state: the "TBS Member" member badge is absent AND a Sign-In
modal / login form is present. No captcha / maintenance / rate-limit markers
were observed, so we do not invent them — the base empty-streak / error trips
cover the rest.

Called by: worker loop (after each search)
Depends on: search_worker_base.circuit_breaker.CircuitBreakerBase
"""

from loguru import logger

from ..search_worker_base.circuit_breaker import CircuitBreakerBase

# Logged-in marker text (lowercased for innerText comparison).
_MEMBER_MARKER = "tbs member"

# Sign-In modal / login-form signals (lowercased).
_LOGIN_SIGNALS = ("sign in",)


class CircuitBreaker(CircuitBreakerBase):
    """TBF-specific circuit breaker with browser page health checking."""

    async def check_page_health(self, page) -> str:
        """Check page content for red flags indicating detection.

        Returns a status string:
        - "HEALTHY" — all good
        - "SESSION_EXPIRED" — logged-out: "TBS Member" marker absent AND a
          Sign-In modal/login form present (normal, handled by session_manager)
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

        # Immediate trip: unexpected redirect off the thebrokersite domain.
        if "thebrokersite.com" not in url:
            self._trip(f"Unexpected redirect to: {url}")
            return "UNEXPECTED_REDIRECT"

        # Session expired: the authenticated "TBS Member" badge is gone AND a
        # Sign-In modal / login form is on the page. The session_manager will
        # re-log-in and the worker re-queues the item.
        member_present = _MEMBER_MARKER in content
        login_present = any(signal in content for signal in _LOGIN_SIGNALS)
        if not member_present and login_present:
            return "SESSION_EXPIRED"

        # No captcha / rate-limit / maintenance markers were observed on TBF, so
        # we don't invent them — the base empty-streak / error trips cover the
        # rest. All clear — reset the failure counter.
        self.consecutive_failures = 0
        logger.debug("TBF circuit breaker: health check OK")
        return "HEALTHY"
