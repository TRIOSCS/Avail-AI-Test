"""Circuit breaker — stops searches on signs of blocking or errors.

Thin subclass of CircuitBreakerBase that adds the TBF-specific
check_page_health() method for browser DOM inspection. The session-expired
and anti-scrape markers are site-specific and require a logged-in capture, so
they are stubbed: until Phase 2 encodes them, check_page_health defaults to
the base empty-streak/error trip behavior and never invents markers.

Called by: worker loop (after each search)
Depends on: search_worker_base.circuit_breaker.CircuitBreakerBase
"""

from loguru import logger

from ..search_worker_base.circuit_breaker import CircuitBreakerBase


class CircuitBreaker(CircuitBreakerBase):
    """TBF-specific circuit breaker with browser page health checking."""

    async def check_page_health(self, page) -> str:
        """Check page content for red flags indicating detection.

        Returns a status string:
        - "HEALTHY" — all good
        - "SESSION_EXPIRED" — login page detected (normal, not an error)
        - Other values trip or accumulate toward tripping the breaker.

        PHASE 1: the TBF-specific session-expired marker and anti-scrape
        signals are not yet known (need a logged-in capture). We keep only the
        transport-level safe defaults (page-read failure streak, off-domain
        redirect) so the breaker still self-heals; site-specific markers are
        encoded in Phase 2.
        """
        try:
            url = page.url
            await page.evaluate("() => document.body.innerText.toLowerCase().substring(0, 3000)")
        except Exception as e:
            self.consecutive_failures += 1
            if self.consecutive_failures >= 3:
                self._trip(f"3 consecutive page health check failures: {e}")
            return "CHECK_FAILED"

        # Immediate trip: unexpected redirect off the thebrokersite domain.
        if "thebrokersite.com" not in url:
            self._trip(f"Unexpected redirect to: {url}")
            return "UNEXPECTED_REDIRECT"

        # TODO(phase2): real selector from logged-in capture — detect the
        # login/session-expired page (return "SESSION_EXPIRED") and the
        # captcha / rate-limit / access-denied anti-scrape markers (trip the
        # breaker) from the authenticated page content captured on the host.

        # All clear — reset failure counters. Empty-result shadow-block trips
        # are still driven by the base record_empty_results() from the loop.
        self.consecutive_failures = 0
        logger.debug("TBF circuit breaker: phase-1 health check — site markers not yet encoded")
        return "HEALTHY"
