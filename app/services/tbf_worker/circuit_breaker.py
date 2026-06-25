"""Circuit breaker — stops searches on signs of blocking or errors.

Thin subclass of CircuitBreakerBase that adds the TBF-specific
check_page_health() method for browser DOM inspection.

TBF is a Vue SPA. The only site-specific signal verified live is the
session-expired state: the "Sign out" control (the logged-in marker) is absent.
We share the single positive marker with session_manager (LOGGED_IN_MARKER) so
the two can never disagree about how "logged in" is detected. No captcha /
maintenance / rate-limit markers were observed, so we do not invent them — the
base empty-streak / error trips cover the rest.

Called by: worker loop (after each search)
Depends on: search_worker_base.circuit_breaker.CircuitBreakerBase, session_manager
"""

from loguru import logger

from ..search_worker_base.circuit_breaker import CircuitBreakerBase
from .session_manager import LOGGED_IN_MARKER


class CircuitBreaker(CircuitBreakerBase):
    """TBF-specific circuit breaker with browser page health checking."""

    async def check_page_health(self, page) -> str:
        """Check page content for red flags indicating detection.

        Returns a status string:
        - "HEALTHY" — all good
        - "SESSION_EXPIRED" — logged-out: the "Sign out" marker is absent
          (normal, handled by session_manager)
        - Other values trip or accumulate toward tripping the breaker.
        """
        try:
            url = page.url
            # Liveness probe: a responsive page returns body text; a hung/broken
            # page raises and accumulates toward a trip.
            await page.evaluate("() => document.body.innerText")
        except Exception as e:
            self.consecutive_failures += 1
            if self.consecutive_failures >= 3:
                self._trip(f"3 consecutive page health check failures: {e}")
            return "CHECK_FAILED"

        # Immediate trip: unexpected redirect off the thebrokersite domain.
        if "thebrokersite.com" not in url:
            self._trip(f"Unexpected redirect to: {url}")
            return "UNEXPECTED_REDIRECT"

        # Session expired: the "Sign out" marker is absent (logged out). The
        # session_manager re-authenticates and the worker re-queues. Fail-safe:
        # a locator error reads as expired (re-login) rather than HEALTHY.
        try:
            logged_in = await page.locator(LOGGED_IN_MARKER).count() > 0
        except Exception:
            logged_in = False
        if not logged_in:
            return "SESSION_EXPIRED"

        # No captcha / rate-limit / maintenance markers were observed on TBF, so
        # we don't invent them — the base empty-streak / error trips cover the
        # rest. All clear — reset the failure counter.
        self.consecutive_failures = 0
        logger.debug("TBF circuit breaker: health check OK")
        return "HEALTHY"
