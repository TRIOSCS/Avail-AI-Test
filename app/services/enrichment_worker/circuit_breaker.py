"""Circuit breaker for the enrichment worker.

Trips after N consecutive Claude errors; resets automatically after a 1h cooldown,
allowing the worker to resume without a restart.

Inherits state-machine infrastructure (consecutive_failures, is_open, trip_reason,
should_stop, get_trip_info, _trip) from CircuitBreakerBase.
"""

from __future__ import annotations

import time

from loguru import logger

from app.services.search_worker_base.circuit_breaker import CircuitBreakerBase

from .config import EnrichmentWorkerConfig

_COOLDOWN_SECONDS = 3600  # 1 hour


class EnrichmentCircuitBreaker(CircuitBreakerBase):
    """Circuit breaker for enrichment-worker Claude calls.

    Trips after ``config.circuit_breaker_errors`` consecutive errors.
    Resets automatically after a 1h cooldown so the worker is self-healing.
    """

    def __init__(self, config: EnrichmentWorkerConfig) -> None:
        super().__init__()
        self._threshold = config.circuit_breaker_errors
        self._tripped_at: float | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record_claude_error(self) -> None:
        """Record a Claude error.

        Trips the breaker if the threshold is reached.
        """
        self.consecutive_failures += 1
        if self.consecutive_failures >= self._threshold:
            self._trip(f"Claude enrichment: {self.consecutive_failures} consecutive errors")
            self._tripped_at = time.monotonic()

    def record_claude_success(self) -> None:
        """Record a successful Claude call — resets the consecutive-error counter."""
        self.consecutive_failures = 0
        # Do not auto-clear is_open here; cooldown handles that.
        logger.debug("ENRICH_BREAKER: success recorded, error counter reset")

    def should_stop(self) -> bool:
        """Return True if the breaker is open and the cooldown has not expired."""
        if not self.is_open:
            return False
        # Auto-reset after cooldown
        if self._tripped_at is not None and time.monotonic() - self._tripped_at >= _COOLDOWN_SECONDS:
            logger.info("ENRICH_BREAKER: 1h cooldown elapsed — resetting breaker")
            self._reset()
            return False
        return True

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _reset(self) -> None:
        """Reset breaker state after cooldown."""
        self.is_open = False
        self.trip_reason = ""
        self.consecutive_failures = 0
        self._tripped_at = None
