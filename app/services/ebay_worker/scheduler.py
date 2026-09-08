"""EBay worker pacing — flat delay plus a daily Browse API call budget.

This deliberately does NOT reuse search_worker_base.scheduler.SearchScheduler.
That scheduler exists to make a browser look human: log-normal delays, random
coffee breaks, a Sunday-evening-to-Friday-afternoon business-hours window.
None of it applies to a server-to-server API poller — eBay does not care what
hour it is, and a random break would only slow the queue down. What DOES bound
an API poller is its rate-limit allowance, so the pacing here is exactly two
rules:

1. Wait at least EBAY_MIN_DELAY_SECONDS between calls.
2. Stop for the day once EBAY_DAILY_CALL_BUDGET calls have been spent, and
   resume at midnight UTC.

The budget counter is per UTC day and lives on the ebay_worker_status
singleton (calls_today / budget_day), so a worker restart mid-day does not
hand itself a fresh allowance.

Called by: worker loop
Depends on: config
"""

from datetime import UTC, date, datetime, timedelta

SECONDS_PER_DAY = 24 * 60 * 60

# Slack added to the whole-search deadline on top of the per-page timeouts, so a
# search that is merely slow is never cancelled by its own outer guard.
SEARCH_DEADLINE_SLACK_SECONDS = 5

# How long to stand down after eBay answered 429 twice in a row. Long enough to
# clear a throttle window, short enough that the queue keeps moving the same day.
RATE_LIMIT_BACKOFF_SECONDS = 300


def utc_today(now: datetime | None = None) -> date:
    """Today's date in UTC — the budget day boundary."""
    return (now or datetime.now(UTC)).date()


def seconds_until_next_utc_day(now: datetime | None = None) -> float:
    """Seconds from ``now`` until the next midnight UTC (always > 0)."""
    now = now or datetime.now(UTC)
    tomorrow = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), tzinfo=UTC)
    return max(1.0, (tomorrow - now).total_seconds())


def rollover_calls(calls_today: int | None, budget_day: date | None, today: date) -> int:
    """Calls already spent on ``today``.

    Returns 0 whenever the stored counter belongs to a previous UTC day (or was never
    set) — that is the midnight reset, applied lazily on the first tick of a new day
    rather than by a scheduled job.
    """
    if budget_day != today:
        return 0
    return calls_today or 0


class EbayScheduler:
    """Pacing decisions for the eBay poller (pure — no IO, no sleeping)."""

    def __init__(self, config):
        self.config = config

    def next_delay(self) -> float:
        """Seconds to wait before the next Browse API call."""
        return float(self.config.EBAY_MIN_DELAY_SECONDS)

    def idle_delay(self) -> float:
        """Seconds to wait when the queue is empty."""
        return float(self.config.EBAY_POLL_IDLE_SECONDS)

    def search_deadline(self) -> float:
        """Hard cap on ONE search — all of its pages — for the outer wait_for.

        EBAY_SEARCH_TIMEOUT_SECONDS is the per-REQUEST timeout handed to httpx. Using
        that same number as the whole-search deadline would cancel a healthy 2-page
        search whenever page 1 ran slow, throwing away the results already fetched, so
        the deadline is the per-request budget times the page count plus a little slack.
        """
        pages = max(1, int(self.config.EBAY_MAX_PAGES))
        return float(int(self.config.EBAY_SEARCH_TIMEOUT_SECONDS) * pages + SEARCH_DEADLINE_SLACK_SECONDS)

    def rate_limit_backoff(self) -> float:
        """Seconds to stand down after a persistent 429."""
        return float(RATE_LIMIT_BACKOFF_SECONDS)

    def budget_remaining(self, calls_today: int) -> int:
        """Calls left in today's budget (never negative)."""
        # int(): `config` is deliberately untyped (the worker configs share no base
        # class), so mypy sees the attribute as Any.
        return max(0, int(self.config.EBAY_DAILY_CALL_BUDGET) - calls_today)

    def budget_exhausted(self, calls_today: int) -> bool:
        """True when today's Browse API call budget is spent."""
        return self.budget_remaining(calls_today) <= 0

    def can_afford_search(self, calls_today: int) -> bool:
        """True when the whole next search fits inside today's remaining budget.

        One search spends up to EBAY_MAX_PAGES calls, so gating on "any budget left" let
        the last search of the day overshoot by up to MAX_PAGES-1 calls.
        """
        return self.budget_remaining(calls_today) >= max(1, int(self.config.EBAY_MAX_PAGES))

    def sleep_until_budget_resets(self, now: datetime | None = None) -> float:
        """Seconds to sleep when the budget is spent — until midnight UTC."""
        return seconds_until_next_utc_day(now)
