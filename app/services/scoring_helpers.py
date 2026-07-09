"""Shared scoring helpers — date range utilities for performance scoring.

Provides common helpers used by both avail_score_service and
multiplier_score_service to avoid duplicating month-range logic.

Called by: avail_score_service.py, multiplier_score_service.py
Depends on: datetime (stdlib only)
"""

from datetime import UTC, date, datetime


def month_range(month: date) -> tuple[datetime, datetime]:
    """Return (start_dt, end_dt) as UTC-aware datetimes for the given month.

    start_dt is midnight on the 1st of the month. end_dt is midnight on the 1st of the
    next month.
    """
    month_start = month.replace(day=1)
    if month_start.month == 12:
        month_end = month_start.replace(year=month_start.year + 1, month=1)
    else:
        month_end = month_start.replace(month=month_start.month + 1)
    midnight = datetime.min.time()
    start_dt = datetime.combine(month_start, midnight, tzinfo=UTC)
    end_dt = datetime.combine(month_end, midnight, tzinfo=UTC)
    return start_dt, end_dt
