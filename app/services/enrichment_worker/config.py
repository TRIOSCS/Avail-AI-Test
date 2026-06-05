"""Configuration for the enrichment worker.

Reads from environment variables with spec §5.5 defaults. Supports direct kwargs
construction for test isolation (no env mutation needed in tests).
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class EnrichmentWorkerConfig:
    """Enrichment worker tunables.

    Defaults match spec §5.5. Construct with ``EnrichmentWorkerConfig.from_env()``
    in production; pass explicit kwargs in tests.
    """

    batch_size: int = 5
    daily_cap: int = 200
    web_daily_cap: int = 80
    loop_sleep_seconds: int = 30
    # Keeps a newly-added part's worst-case wait ~1 min when the queue is
    # otherwise empty; select_batch is a cheap indexed query, so polling this
    # often is negligible.
    idle_sleep_seconds: int = 60
    not_found_retry_hours: int = 22
    circuit_breaker_errors: int = 5

    @classmethod
    def from_env(cls) -> "EnrichmentWorkerConfig":
        """Build config from environment variables, falling back to defaults."""
        return cls(
            batch_size=int(os.environ.get("ENRICHMENT_BATCH_SIZE", 5)),
            daily_cap=int(os.environ.get("ENRICHMENT_DAILY_CAP", 200)),
            web_daily_cap=int(os.environ.get("ENRICHMENT_WEB_DAILY_CAP", 80)),
            loop_sleep_seconds=int(os.environ.get("ENRICHMENT_LOOP_SLEEP_SECONDS", 30)),
            idle_sleep_seconds=int(os.environ.get("ENRICHMENT_IDLE_SLEEP_SECONDS", 60)),
            not_found_retry_hours=int(os.environ.get("ENRICHMENT_NOT_FOUND_RETRY_HOURS", 22)),
            circuit_breaker_errors=int(os.environ.get("ENRICHMENT_CIRCUIT_BREAKER_ERRORS", 5)),
        )
