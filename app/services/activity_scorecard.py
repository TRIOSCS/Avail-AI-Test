"""Activity Scorecard — per-user activity metrics + a weighted score, ranked.

Computes a leaderboard of per-user sales/sourcing activity over a time range
(this_week / this_month / this_quarter / all_time, default this_month):

  - calls            — ActivityLog rows on the PHONE channel (made + received)
  - talk_time_seconds — SUM(duration_seconds) over those PHONE rows
  - emails_sent      — EMAIL channel, OUTBOUND direction
  - ims_sent         — TEAMS/WECHAT channel, OUTBOUND direction
  - accounts_added   — Company.created_by_id count per user
  - contacts_added   — SiteContact.created_by_id count per user

The score is a weighted sum of the six metrics (see WEIGHTS below — the single,
easy-to-tune knob). Rows are returned ranked by score descending.

Called by: routers/htmx_views.py (settings/scorecard tab).
Depends on: models (ActivityLog, Company, SiteContact, User), constants
  (Channel, Direction), database session.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import case
from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session

from ..constants import Channel, Direction
from ..models import ActivityLog, Company, SiteContact, User

# ── Scoring weights — THE tuning knob ─────────────────────────────────────────
# A user's score is the weighted sum of their metrics. Each weight is "points per
# unit" of that metric. Talk time is scored per 5 minutes (300s) of conversation,
# everything else per event. Edit these to re-balance the leaderboard — nothing
# else needs to change.
TALK_TIME_BUCKET_SECONDS = 300  # 5 minutes = 1 talk-time point


@dataclass(frozen=True)
class ScoreWeights:
    """Points awarded per unit of each activity metric."""

    call: float = 1.0
    talk_time_per_bucket: float = 1.0  # per TALK_TIME_BUCKET_SECONDS (5 min)
    email: float = 1.0
    im: float = 0.5
    account_added: float = 5.0
    contact_added: float = 2.0


WEIGHTS = ScoreWeights()

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
    """One user's activity metrics + computed score (rank assigned after sort)."""

    user_id: int
    user: User
    calls: int
    talk_time_seconds: int
    emails_sent: int
    ims_sent: int
    accounts_added: int
    contacts_added: int
    score: float
    rank: int = 0  # 1-based, assigned by compute_scorecard after ranking

    @property
    def talk_time_hms(self) -> str:
        """Talk time formatted as ``h:mm`` (e.g. 1:05, 0:00)."""
        return format_talk_time(self.talk_time_seconds)


def format_talk_time(total_seconds: int | None) -> str:
    """Format a seconds total as ``h:mm`` (hours uncapped, minutes zero-padded)."""
    secs = int(total_seconds or 0)
    hours, remainder = divmod(secs, 3600)
    minutes = remainder // 60
    return f"{hours}:{minutes:02d}"


def compute_score(
    *,
    calls: int,
    talk_time_seconds: int,
    emails_sent: int,
    ims_sent: int,
    accounts_added: int,
    contacts_added: int,
    weights: ScoreWeights = WEIGHTS,
) -> float:
    """Weighted-sum score for one user's metrics. Rounded to 1 decimal place.

    Talk time contributes ``weights.talk_time_per_bucket`` points per
    ``TALK_TIME_BUCKET_SECONDS`` (5 min) of conversation, pro-rated (not stepped),
    so a 7.5-minute call is worth 1.5 talk-time points.
    """
    talk_buckets = (talk_time_seconds or 0) / TALK_TIME_BUCKET_SECONDS
    score = (
        calls * weights.call
        + talk_buckets * weights.talk_time_per_bucket
        + emails_sent * weights.email
        + ims_sent * weights.im
        + accounts_added * weights.account_added
        + contacts_added * weights.contact_added
    )
    return round(score, 1)


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
    """One GROUP BY query over activity_log → {user_id: {calls, talk, emails, ims}}.

    Aggregates all four ActivityLog-derived metrics in a single pass using conditional
    aggregates (FILTER-style CASE) so there is no per-user loop and no N+1. NULL user_id
    rows (system/unattributed activity) are excluded.
    """
    is_phone = ActivityLog.channel == Channel.PHONE
    is_email_out = (ActivityLog.channel == Channel.EMAIL) & (ActivityLog.direction == Direction.OUTBOUND)
    is_im_out = ActivityLog.channel.in_((Channel.TEAMS, Channel.WECHAT)) & (ActivityLog.direction == Direction.OUTBOUND)

    calls_expr = sqlfunc.sum(case((is_phone, 1), else_=0))
    talk_expr = sqlfunc.sum(case((is_phone, sqlfunc.coalesce(ActivityLog.duration_seconds, 0)), else_=0))
    emails_expr = sqlfunc.sum(case((is_email_out, 1), else_=0))
    ims_expr = sqlfunc.sum(case((is_im_out, 1), else_=0))

    query = db.query(
        ActivityLog.user_id,
        calls_expr.label("calls"),
        talk_expr.label("talk"),
        emails_expr.label("emails"),
        ims_expr.label("ims"),
    ).filter(ActivityLog.user_id.isnot(None))
    if start is not None:
        query = query.filter(ActivityLog.created_at >= start)
    query = query.group_by(ActivityLog.user_id)

    return {
        row.user_id: {
            "calls": int(row.calls or 0),
            "talk": int(row.talk or 0),
            "emails": int(row.emails or 0),
            "ims": int(row.ims or 0),
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
    weights: ScoreWeights = WEIGHTS,
    now: datetime | None = None,
) -> list[ScorecardRow]:
    """Per-user activity scorecard, ranked by score descending.

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
        calls = metrics.get("calls", 0)
        talk = metrics.get("talk", 0)
        emails = metrics.get("emails", 0)
        ims = metrics.get("ims", 0)
        accounts_added = accounts.get(uid, 0)
        contacts_added = contacts.get(uid, 0)
        score = compute_score(
            calls=calls,
            talk_time_seconds=talk,
            emails_sent=emails,
            ims_sent=ims,
            accounts_added=accounts_added,
            contacts_added=contacts_added,
            weights=weights,
        )
        rows.append(
            ScorecardRow(
                user_id=uid,
                user=user,
                calls=calls,
                talk_time_seconds=talk,
                emails_sent=emails,
                ims_sent=ims,
                accounts_added=accounts_added,
                contacts_added=contacts_added,
                score=score,
            )
        )

    rows.sort(key=lambda r: (-r.score, (r.user.name or r.user.email or "").lower(), r.user_id))
    return [_with_rank(row, i + 1) for i, row in enumerate(rows)]


def _with_rank(row: ScorecardRow, rank: int) -> ScorecardRow:
    """Return a copy of ``row`` with its 1-based ``rank`` field set."""
    return ScorecardRow(
        user_id=row.user_id,
        user=row.user,
        calls=row.calls,
        talk_time_seconds=row.talk_time_seconds,
        emails_sent=row.emails_sent,
        ims_sent=row.ims_sent,
        accounts_added=row.accounts_added,
        contacts_added=row.contacts_added,
        score=row.score,
        rank=rank,
    )


def scoring_formula_parts(weights: ScoreWeights = WEIGHTS) -> list[dict[str, str]]:
    """Human-readable breakdown of the weights for transparent UI display.

    Each entry is ``{"label": ..., "weight": ...}`` describing one term of the
    weighted sum, so the template can render the formula without hard-coding the
    numbers (they stay in sync with WEIGHTS automatically).
    """
    bucket_min = TALK_TIME_BUCKET_SECONDS // 60
    return [
        {"label": "Call", "weight": f"{_fmt(weights.call)} pt each"},
        {"label": "Talk time", "weight": f"{_fmt(weights.talk_time_per_bucket)} pt / {bucket_min} min"},
        {"label": "Email sent", "weight": f"{_fmt(weights.email)} pt each"},
        {"label": "IM sent", "weight": f"{_fmt(weights.im)} pt each"},
        {"label": "Account added", "weight": f"{_fmt(weights.account_added)} pts each"},
        {"label": "Contact added", "weight": f"{_fmt(weights.contact_added)} pts each"},
    ]


def _fmt(value: float) -> str:
    """Format a weight without a trailing ``.0`` (1.0 → "1", 0.5 → "0.5")."""
    return str(int(value)) if value == int(value) else str(value)
