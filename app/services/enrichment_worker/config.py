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
    not_catalogued_retry_days: int = 30
    circuit_breaker_errors: int = 5
    # OEM web-resolution pass (Pass A): at most this many resolve_oem_spare calls per
    # batch, and at most oem_resolve_daily_cap per day. The daily sub-cap is counted
    # INSIDE web_daily_cap (every resolve bills the web counter too), not in addition.
    oem_resolve_per_batch: int = 2
    oem_resolve_daily_cap: int = 40

    @classmethod
    def from_env(cls) -> "EnrichmentWorkerConfig":
        """Build config from environment variables, falling back to defaults."""

        def env_int(key: str, default: int) -> int:
            return int(os.environ.get(key, default))

        return cls(
            batch_size=env_int("ENRICHMENT_BATCH_SIZE", 5),
            daily_cap=env_int("ENRICHMENT_DAILY_CAP", 200),
            web_daily_cap=env_int("ENRICHMENT_WEB_DAILY_CAP", 80),
            loop_sleep_seconds=env_int("ENRICHMENT_LOOP_SLEEP_SECONDS", 30),
            idle_sleep_seconds=env_int("ENRICHMENT_IDLE_SLEEP_SECONDS", 60),
            not_found_retry_hours=env_int("ENRICHMENT_NOT_FOUND_RETRY_HOURS", 22),
            not_catalogued_retry_days=env_int("ENRICHMENT_NOT_CATALOGUED_RETRY_DAYS", 30),
            circuit_breaker_errors=env_int("ENRICHMENT_CIRCUIT_BREAKER_ERRORS", 5),
            oem_resolve_per_batch=env_int("ENRICHMENT_OEM_RESOLVE_PER_BATCH", 2),
            oem_resolve_daily_cap=env_int("ENRICHMENT_OEM_RESOLVE_DAILY_CAP", 40),
        )
