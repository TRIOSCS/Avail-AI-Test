"""ICsource worker configuration.

All ICS worker settings in one place, loaded from environment variables
with sensible defaults. Rate limits are conservative to avoid detection.

Called by: all ics_worker modules
Depends on: environment variables (ICS_USERNAME, ICS_PASSWORD, etc.)
"""

import os


class IcsConfig:
    """Configuration for the ICsource search worker."""

    def __init__(self):
        self.ICS_USERNAME = os.environ.get("ICS_USERNAME", "")
        self.ICS_PASSWORD = os.environ.get("ICS_PASSWORD", "")
        self.ICS_MAX_DAILY_SEARCHES = int(os.environ.get("ICS_MAX_DAILY_SEARCHES", "50"))
        self.ICS_MAX_HOURLY_SEARCHES = int(os.environ.get("ICS_MAX_HOURLY_SEARCHES", "10"))
        self.ICS_MIN_DELAY_SECONDS = int(os.environ.get("ICS_MIN_DELAY_SECONDS", "150"))
        self.ICS_MAX_DELAY_SECONDS = int(os.environ.get("ICS_MAX_DELAY_SECONDS", "420"))
        self.ICS_TYPICAL_DELAY_SECONDS = int(os.environ.get("ICS_TYPICAL_DELAY_SECONDS", "270"))
        self.ICS_DEDUP_WINDOW_DAYS = int(os.environ.get("ICS_DEDUP_WINDOW_DAYS", "7"))
        self.ICS_BUSINESS_HOURS_START = int(os.environ.get("ICS_BUSINESS_HOURS_START", "8"))
        self.ICS_BUSINESS_HOURS_END = int(os.environ.get("ICS_BUSINESS_HOURS_END", "18"))
        self.ICS_BROWSER_PROFILE_DIR = os.environ.get(
            "ICS_BROWSER_PROFILE_DIR", "/root/ics_browser_profile"
        )
