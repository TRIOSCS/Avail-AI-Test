"""The Broker Forum (TBF) worker configuration.

All TBF worker settings in one place, loaded from environment variables
with sensible defaults. Rate limits are conservative to avoid detection.
Attribute names mirror IcsConfig so the shared loop/scheduler treat TBF
identically.

Called by: all tbf_worker modules
Depends on: environment variables (TBF_USERNAME, TBF_PASSWORD, etc.)
"""

import os


class TbfConfig:
    """Configuration for The Broker Forum search worker."""

    def __init__(self):
        # Member login (no account number — TBF authenticates on username/password).
        self.TBF_USERNAME = os.environ.get("TBF_USERNAME", "")
        self.TBF_PASSWORD = os.environ.get("TBF_PASSWORD", "")
        self.TBF_MAX_DAILY_SEARCHES = int(os.environ.get("TBF_MAX_DAILY_SEARCHES", "50"))
        self.TBF_MAX_HOURLY_SEARCHES = int(os.environ.get("TBF_MAX_HOURLY_SEARCHES", "10"))
        self.TBF_MIN_DELAY_SECONDS = int(os.environ.get("TBF_MIN_DELAY_SECONDS", "180"))
        self.TBF_MAX_DELAY_SECONDS = int(os.environ.get("TBF_MAX_DELAY_SECONDS", "600"))
        self.TBF_TYPICAL_DELAY_SECONDS = int(os.environ.get("TBF_TYPICAL_DELAY_SECONDS", "300"))
        self.TBF_DEDUP_WINDOW_DAYS = int(os.environ.get("TBF_DEDUP_WINDOW_DAYS", "7"))
        self.TBF_BUSINESS_HOURS_START = int(os.environ.get("TBF_BUSINESS_HOURS_START", "8"))
        self.TBF_BUSINESS_HOURS_END = int(os.environ.get("TBF_BUSINESS_HOURS_END", "18"))
        self.TBF_BROWSER_PROFILE_DIR = os.environ.get("TBF_BROWSER_PROFILE_DIR", "/root/tbf_browser_profile")
        # Hard cap on a single search (incl. human-behavior delays) so a wedged page
        # can't stall the loop/heartbeat.
        self.TBF_SEARCH_TIMEOUT_SECONDS = int(os.environ.get("TBF_SEARCH_TIMEOUT_SECONDS", "150"))
        # Circuit-breaker self-heal cooldown: auto-reset this long after a trip.
        self.TBF_BREAKER_COOLDOWN_MINUTES = int(os.environ.get("TBF_BREAKER_COOLDOWN_MINUTES", "30"))
