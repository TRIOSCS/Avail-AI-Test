"""EBay worker configuration.

All eBay worker settings in one place, loaded from environment variables with
sensible defaults. The two fields the shared factory already owns
(MIN_DELAY_SECONDS, DEDUP_WINDOW_DAYS) come from
search_worker_base.config.build_worker_config; the browser-only fields it also
carries (BROWSER_PROFILE_DIR, MAX/TYPICAL delay) are deliberately not copied
onto this config — the eBay worker drives an HTTP API, not a browser, and
paces on a flat minimum delay plus a daily call budget.

Credentials are NOT read here. Unlike the browser workers (host-only
.env credentials), eBay reuses the EBAY_CLIENT_ID / EBAY_CLIENT_SECRET pair
that Settings -> Connectors already stores encrypted in the DB; the worker
reads them DB-first with an env fallback via credential_service (see
worker.py::_load_credentials).

Called by: all ebay_worker modules
Depends on: environment variables (EBAY_*), search_worker_base.config
"""

import os

from ..search_worker_base.config import build_worker_config

# eBay's Browse API caps a single item_summary/search page at 200 results.
MAX_PAGE_LIMIT = 200


class EbayConfig:
    """Configuration for the eBay Browse API search worker."""

    def __init__(self):
        shared = build_worker_config("EBAY", defaults={"MIN_DELAY_SECONDS": "3"})
        # Minimum seconds between Browse API calls (flat pacing, no jitter/breaks).
        self.EBAY_MIN_DELAY_SECONDS = shared["EBAY_MIN_DELAY_SECONDS"]
        # Cross-requirement dedup window, same semantics as the browser workers.
        self.EBAY_DEDUP_WINDOW_DAYS = shared["EBAY_DEDUP_WINDOW_DAYS"]

        # Marketplace the search runs against (X-EBAY-C-MARKETPLACE-ID header).
        self.EBAY_MARKETPLACE_ID = os.environ.get("EBAY_MARKETPLACE_ID", "EBAY_US")
        # Comma-separated category ids. Empty (the default) = search ALL of eBay:
        # brokered/surplus board-level parts are listed under many categories and
        # a category filter silently hides them.
        self.EBAY_CATEGORY_IDS = os.environ.get("EBAY_CATEGORY_IDS", "")
        # Results per Browse API page (hard-capped at the API's own 200 maximum).
        self.EBAY_PAGE_LIMIT = min(int(os.environ.get("EBAY_PAGE_LIMIT", "50")), MAX_PAGE_LIMIT)
        # How many pages to walk per MPN before moving on.
        self.EBAY_MAX_PAGES = int(os.environ.get("EBAY_MAX_PAGES", "2"))
        # Browse API calls the worker may spend per UTC day.
        self.EBAY_DAILY_CALL_BUDGET = int(os.environ.get("EBAY_DAILY_CALL_BUDGET", "4000"))
        # Hard cap on one search (all its pages) so a stalled API can't wedge the loop.
        self.EBAY_SEARCH_TIMEOUT_SECONDS = int(os.environ.get("EBAY_SEARCH_TIMEOUT_SECONDS", "30"))
        # Auctions are excluded by default — a live auction price is not a
        # quotable offer. When true, the buyingOptions filter is dropped.
        self.EBAY_INCLUDE_AUCTIONS = os.environ.get("EBAY_INCLUDE_AUCTIONS", "").lower() in ("1", "true", "yes")
        # Sleep between polls when the queue is empty.
        self.EBAY_POLL_IDLE_SECONDS = int(os.environ.get("EBAY_POLL_IDLE_SECONDS", "30"))
        # Circuit-breaker self-heal cooldown: auto-reset this long after a trip.
        self.EBAY_BREAKER_COOLDOWN_MINUTES = int(os.environ.get("EBAY_BREAKER_COOLDOWN_MINUTES", "30"))

    @property
    def category_id_list(self) -> list[str]:
        """EBAY_CATEGORY_IDS split into a clean list ([] when unset)."""
        return [c.strip() for c in self.EBAY_CATEGORY_IDS.split(",") if c.strip()]
