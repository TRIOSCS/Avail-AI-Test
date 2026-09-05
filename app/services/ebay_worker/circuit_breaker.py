"""Circuit breaker — stops eBay searches on repeated API failure.

Thin subclass of CircuitBreakerBase. The browser workers inspect a DOM for
captcha/session markers; an API poller has no page to inspect, so this one
reads the HTTP outcome instead: repeated transport/5xx failures trip it, an
auth failure (401/403 that survived the token re-mint) trips it immediately
(the credentials are wrong — hammering eBay will not fix that), and the base
class's empty-result streak still guards against a silently-broken query.

Called by: worker loop (after each search)
Depends on: search_worker_base.circuit_breaker.CircuitBreakerBase
"""

from loguru import logger

from ..search_worker_base.circuit_breaker import CircuitBreakerBase

# Consecutive transport/5xx failures before the breaker opens.
MAX_CONSECUTIVE_FAILURES = 3


class CircuitBreaker(CircuitBreakerBase):
    """EBay-specific circuit breaker keyed on HTTP outcomes."""

    def record_api_failure(self, error: Exception, status_code: int | None = None) -> str:
        """Record one failed Browse API call and return the resulting status.

        Returns "AUTH_FAILED" (tripped immediately), "TRIPPED" (failure streak reached
        the limit) or "FAILED" (counted, still closed).
        """
        if status_code in (401, 403):
            self._trip(f"eBay auth failure {status_code}: {error}")
            return "AUTH_FAILED"
        self.consecutive_failures += 1
        if self.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            self._trip(f"{MAX_CONSECUTIVE_FAILURES} consecutive eBay API failures: {error}")
            return "TRIPPED"
        return "FAILED"

    def record_api_success(self) -> None:
        """Clear the consecutive-failure counter after a healthy call."""
        self.consecutive_failures = 0
        logger.debug("EBAY circuit breaker: API call OK")
