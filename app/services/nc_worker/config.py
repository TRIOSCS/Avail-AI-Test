"""NetComponents worker configuration.

All NC worker settings in one place, loaded from environment variables
with sensible defaults. Rate limits are conservative to avoid detection.

Called by: all nc_worker modules
Depends on: environment variables (NC_USERNAME, NC_PASSWORD, etc.)
"""

import os


class NcConfig:
    """Configuration for the NetComponents search worker."""

    def __init__(self):
        self.NC_USERNAME = os.environ.get("NC_USERNAME", "")
        self.NC_PASSWORD = os.environ.get("NC_PASSWORD", "")
        self.NC_MAX_DAILY_SEARCHES = int(os.environ.get("NC_MAX_DAILY_SEARCHES", "75"))
        self.NC_MAX_HOURLY_SEARCHES = int(os.environ.get("NC_MAX_HOURLY_SEARCHES", "12"))
        self.NC_MIN_DELAY_SECONDS = int(os.environ.get("NC_MIN_DELAY_SECONDS", "120"))
        self.NC_MAX_DELAY_SECONDS = int(os.environ.get("NC_MAX_DELAY_SECONDS", "420"))
        self.NC_TYPICAL_DELAY_SECONDS = int(os.environ.get("NC_TYPICAL_DELAY_SECONDS", "240"))
        self.NC_DEDUP_WINDOW_DAYS = int(os.environ.get("NC_DEDUP_WINDOW_DAYS", "7"))
        self.NC_BUSINESS_HOURS_START = int(os.environ.get("NC_BUSINESS_HOURS_START", "8"))
        self.NC_BUSINESS_HOURS_END = int(os.environ.get("NC_BUSINESS_HOURS_END", "18"))
        self.NC_BROWSER_PROFILE_DIR = os.environ.get(
            "NC_BROWSER_PROFILE_DIR", "/home/avail/nc_browser_profile"
        )
