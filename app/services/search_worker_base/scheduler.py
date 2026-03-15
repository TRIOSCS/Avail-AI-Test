"""Search scheduler — timing, delays, and break management.

Controls the pacing of searches to mimic human behavior:
log-normal delay distribution, periodic breaks, business hours enforcement.
Parameterized by attribute prefix so both ICS and NC workers share one implementation.

Called by: worker loop
Depends on: config
"""

import math
import random
from datetime import datetime

from loguru import logger

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo  # pragma: no cover

EASTERN = ZoneInfo("America/New_York")


class SearchScheduler:
    """Manages search timing to simulate natural human browsing patterns."""

    def __init__(self, config, prefix: str):
        self.config = config
        self.prefix = prefix
        self.searches_since_break = 0
        self.break_threshold = random.randint(8, 15)

    def _attr(self, suffix: str):
        """Get a prefixed config attribute."""
        return getattr(self.config, f"{self.prefix}_{suffix}")

    def is_business_hours(self) -> bool:
        """Check if current time (Eastern) is within work window.

        Window: Sunday 6 PM ET through Friday 5 PM ET.
        Off: Friday 5 PM -> Sunday 6 PM (Saturday all day).
        """
        import os

        if os.environ.get("FORCE_BUSINESS_HOURS"):
            return True
        now = datetime.now(EASTERN)
        wd = now.weekday()  # Mon=0 ... Sun=6
        hour = now.hour
        # Saturday (5) — always off
        if wd == 5:
            return False
        # Sunday (6) — only on at 6 PM+
        if wd == 6:
            return hour >= 18
        # Friday (4) — only on until 5 PM
        if wd == 4:
            return hour < 17
        # Mon-Thu (0-3) — always on
        return True

    def next_delay(self) -> float:
        """Generate a realistic delay between searches using log-normal distribution.

        Most delays cluster around the typical_delay. Occasional longer pauses simulate
        checking email, coffee, etc.
        """
        mu = math.log(self._attr("TYPICAL_DELAY_SECONDS"))
        sigma = 0.4
        delay = random.lognormvariate(mu, sigma)
        delay = max(self._attr("MIN_DELAY_SECONDS"), min(self._attr("MAX_DELAY_SECONDS"), delay))
        self.searches_since_break += 1
        return delay

    def time_for_break(self) -> bool:
        """Return True when enough searches have been done to warrant a break."""
        return self.searches_since_break >= self.break_threshold

    def get_break_duration(self) -> float:
        """Return a random break duration between 5-25 minutes (in seconds)."""
        return random.uniform(5 * 60, 25 * 60)

    def reset_break_counter(self):
        """Reset the break counter and pick a new random threshold."""
        self.searches_since_break = 0
        self.break_threshold = random.randint(8, 15)
        logger.debug("Scheduler: break counter reset, next break at {} searches", self.break_threshold)
