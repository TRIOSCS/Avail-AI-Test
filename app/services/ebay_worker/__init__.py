"""EBay automated search worker.

Queue-driven poller for eBay's Browse API (item_summary/search). Searches are queued
automatically when a requirement with an MPN is added to AVAIL, exactly like the
ICS/NC/TBF browser workers — but this one talks to an HTTP API, so it needs no browser,
no Xvfb, no session manager, and no AI commodity gate.

Re-exports the public surface used by callers outside the package.
"""

from .config import EbayConfig
from .queue_manager import enqueue_for_ebay_search
from .sighting_writer import save_ebay_sightings

__all__ = ["EbayConfig", "enqueue_for_ebay_search", "save_ebay_sightings"]
