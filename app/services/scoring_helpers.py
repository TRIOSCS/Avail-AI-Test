"""Shared scoring helpers — date range utilities for performance scoring.

Provides common helpers used by both avail_score_service and
multiplier_score_service to avoid duplicating month-range logic.

Called by: avail_score_service.py, multiplier_score_service.py
Depends on: datetime (stdlib only)
"""

from datetime import date, datetime, timezone


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
    start_dt = datetime(month_start.year, month_start.month, month_start.day, tzinfo=timezone.utc)
    end_dt = datetime(month_end.year, month_end.month, month_end.day, tzinfo=timezone.utc)
    return start_dt, end_dt
