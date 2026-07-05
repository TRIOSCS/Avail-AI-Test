"""Activity Scorecard — per-user raw activity counts, ranked by total.

Computes a leaderboard of per-user sourcing/sales activity over a time range
(this_week / this_month / this_quarter / all_time, default this_month) as plain
raw counts — no weighting, no blended score:

  - calls    — ActivityLog rows on the PHONE channel (made + received)
  - emails   — EMAIL channel, OUTBOUND direction
  - accounts — Company.created_by_id count per user (companies the user created)
  - contacts — SiteContact.created_by_id count per user (contacts the user created)
  - total    — calls + emails + accounts + contacts (simple sum of the four)

Rows are returned ranked by ``total`` descending.

Called by: routers/htmx/settings.py (settings/scorecard tab).
Depends on: models (ActivityLog, Company, SiteContact, User), constants
  (Channel, Direction), database session.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import case
from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session

from ..constants import Channel, Direction
from ..models import ActivityLog, Company, SiteContact, User

# Valid time-range keys (the selector vocabulary). Default is this_month.
TIME_RANGES: tuple[str, ...] = ("this_week", "this_month", "this_quarter", "all_time")
DEFAULT_TIME_RANGE = "this_month"

# Human labels for the selector / header (single source of truth for the UI copy).
TIME_RANGE_LABELS: dict[str, str] = {
    "this_week": "This week",
    "this_month": "This month",
    "this_quarter": "This quarter",
    "all_time": "All time",
}


@dataclass(frozen=True)
class ScorecardRow:
    """One user's raw activity counts (rank assigned after sort)."""

    user_id: int
    user: User
    calls: int
    emails: int
    accounts: int
    contacts: int
    rank: int = 0  # 1-based, assigned by compute_scorecard after ranking

    @property
    def total(self) -> int:
        """Sum of the four raw counts — the leaderboard ranking key."""
        return self.calls + self.emails + self.accounts + self.contacts


def range_start(time_range: str, *, now: datetime | None = None) -> datetime | None:
    """Resolve a time-range key to its inclusive UTC start (None = all_time).

    Weeks start Monday. Months/quarters start on the 1st of the period. The value
    is a timezone-aware UTC datetime so it compares cleanly against the stored
    ``created_at`` column (also UTC-aware).
    """
    if time_range == "all_time":
        return None
    now = now or datetime.now(timezone.utc)
    today = now.date()
    if time_range == "this_week":
        start_date = today - timedelta(days=today.weekday())
    elif time_range == "this_quarter":
        quarter_first_month = 3 * ((today.month - 1) // 3) + 1
        start_date = date(today.year, quarter_first_month, 1)
    else:  # this_month (default) — unknown keys fall back to month
        start_date = date(today.year, today.month, 1)
    return datetime(start_date.year, start_date.month, start_date.day, tzinfo=timezone.utc)


def _activity_metrics_by_user(db: Session, start: datetime | None) -> dict[int, dict[str, int]]:
    """One GROUP BY query over activity_log → {user_id: {calls, emails}}.

    Aggregates the two ActivityLog-derived counts in a single pass using conditional
    aggregates (FILTER-style CASE) so there is no per-user loop and no N+1. NULL user_id
    rows (system/unattributed activity) are excluded.
    """
    is_phone = ActivityLog.channel == Channel.PHONE
    is_email_out = (ActivityLog.channel == Channel.EMAIL) & (ActivityLog.direction == Direction.OUTBOUND)

    calls_expr = sqlfunc.sum(case((is_phone, 1), else_=0))
    emails_expr = sqlfunc.sum(case((is_email_out, 1), else_=0))

    query = db.query(
        ActivityLog.user_id,
        calls_expr.label("calls"),
        emails_expr.label("emails"),
    ).filter(ActivityLog.user_id.isnot(None))
    if start is not None:
        query = query.filter(ActivityLog.created_at >= start)
    query = query.group_by(ActivityLog.user_id)

    return {
        row.user_id: {
            "calls": int(row.calls or 0),
            "emails": int(row.emails or 0),
        }
        for row in query.all()
    }


def _created_counts(db: Session, model, created_col, start: datetime | None) -> dict[int, int]:
    """GROUP BY a model's creator column → {user_id: count} (one query, no loop)."""
    query = db.query(created_col, sqlfunc.count(model.id)).filter(created_col.isnot(None))
    if start is not None:
        query = query.filter(model.created_at >= start)
    return {uid: int(count) for uid, count in query.group_by(created_col).all()}


def compute_scorecard(
    db: Session,
    time_range: str = DEFAULT_TIME_RANGE,
    *,
    now: datetime | None = None,
) -> list[ScorecardRow]:
    """Per-user activity scorecard, ranked by total count descending.

    Total is the simple sum of calls, emails, accounts, and contacts.
    Aggregates efficiently: one GROUP BY over activity_log, one over companies, one
    over site_contacts, and one User lookup — four queries total, independent of the
    number of users. Users with zero activity in the range are omitted (the
    leaderboard shows only contributors). Ties break on user name then id so the
    order is deterministic.
    """
    if time_range not in TIME_RANGES:
        time_range = DEFAULT_TIME_RANGE
    start = range_start(time_range, now=now)

    activity = _activity_metrics_by_user(db, start)
    accounts = _created_counts(db, Company, Company.created_by_id, start)
    contacts = _created_counts(db, SiteContact, SiteContact.created_by_id, start)

    user_ids = set(activity) | set(accounts) | set(contacts)
    if not user_ids:
        return []

    users = {u.id: u for u in db.query(User).filter(User.id.in_(user_ids)).all()}

    rows: list[ScorecardRow] = []
    for uid in user_ids:
        user = users.get(uid)
        if user is None:  # user deleted but activity rows remain — skip orphans
            continue
        metrics = activity.get(uid, {})
        rows.append(
            ScorecardRow(
                user_id=uid,
                user=user,
                calls=metrics.get("calls", 0),
                emails=metrics.get("emails", 0),
                accounts=accounts.get(uid, 0),
                contacts=contacts.get(uid, 0),
            )
        )

    rows.sort(key=lambda r: (-r.total, (r.user.name or r.user.email or "").lower(), r.user_id))
    return [replace(row, rank=i + 1) for i, row in enumerate(rows)]
