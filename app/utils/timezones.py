"""Per-user timezone display layer — convert stored-UTC datetimes to a viewer's zone.

Storage stays UTC (the ``UTCDateTime`` convention is unchanged). This module is the
single mechanism for rendering those UTC instants in a specific user's IANA timezone:

  - ``is_valid_timezone`` / ``resolve_zoneinfo`` — validate + resolve an IANA name.
  - ``company_zoneinfo`` — the business-DEFAULT zone (``settings.company_timezone``),
    the fallback whenever a viewer's zone is unknown; per-user display_timezone wins.
  - ``local_day_sentinel`` — day-boundary math for the calendar-date ``due_at``
    sentinel convention (the meant date at UTC midnight): the CURRENT VIEWER's
    calendar day N days out, matching ``_task_due_state``'s bucketing zone.
  - ``current_display_zoneinfo`` — the CURRENT request's viewer zone, read from the
    ``current_user_display_tz_var`` contextvar (set by the async AuditUserMiddleware —
    a sync dependency would lose the ContextVar in the threadpool), falling back to
    the company zone when unknown.
  - ``to_display_tz`` / ``format_localtime`` / ``format_localdate`` — convert/format a
    UTC datetime, defaulting to the current viewer zone but accepting an explicit zone
    (for server-side use like emails, where there is no request contextvar).

``as_utc`` is the canonical naive→UTC coercion for stored datetimes (SQLite
round-trips and legacy rows come back naive): it tags naive values as UTC, passes
aware values and None through unchanged. Services/jobs import it instead of
declaring private copies.

Called by: app/template_env.py (the ``localtime``/``localdate`` Jinja filters and
    ``_task_due_state``), app/routers/htmx/settings.py (the timezone endpoint),
    app/main.py AuditUserMiddleware (populating the contextvar),
    app/services/task_service.py (``local_day_sentinel``), services/jobs (``as_utc``).
    Reusable by services/emails.
Depends on: stdlib zoneinfo + loguru + app/request_context.py; app/config.py
    (lazy import inside company_zoneinfo, avoiding an import cycle).
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from functools import lru_cache
from typing import overload
from zoneinfo import ZoneInfo, available_timezones

from loguru import logger

from ..request_context import current_user_display_tz_var

# COMPANY_TIMEZONE values already warned about (warn once per bad value, not per call).
_warned_company_tz: set[str | None] = set()

# Last-resort zone when even config.company_timezone is unset/invalid, and the pinned
# business zone for a few server-side renders (quote_send, prepayment_notifications).
# America/New_York is the business operating zone — the SAME zone the background workers
# hard-code and the buyplan auto-complete default (config.buyplan_auto_complete_tz) use.
# Runtime fallbacks resolve through company_zoneinfo() (config-driven) first; this
# constant only backstops an invalid COMPANY_TIMEZONE env value.
DEFAULT_DISPLAY_TZ = "America/New_York"

_DEFAULT_TIME_FMT = "%b %d, %Y %H:%M"
_DEFAULT_DATE_FMT = "%b %d, %Y"


@lru_cache(maxsize=1)
def _valid_names() -> frozenset[str]:
    """Cached snapshot of the IANA zone database keys (available_timezones scans
    dirs)."""
    return frozenset(available_timezones())


def is_valid_timezone(name: str | None) -> bool:
    """True only for a real IANA zone name (e.g. 'Asia/Tokyo').

    None/blank/Windows → False.
    """
    if not name or not isinstance(name, str):
        return False
    return name in _valid_names()


def company_zoneinfo() -> ZoneInfo:
    """The business-DEFAULT zone (``settings.company_timezone``), config-driven.

    Decision O residual: the ONE company-configurable default — the zone every
    timezone fallback resolves to when a viewer's display_timezone is unknown
    (NULL / invalid / no request context, e.g. background jobs). A set per-user
    display_timezone always wins (``current_display_zoneinfo``). Read at call time
    so env/monkeypatched settings take effect; an invalid name falls back to
    DEFAULT_DISPLAY_TZ. ZoneInfo caches instances by key, so this is cheap.
    """
    from ..config import settings

    name = settings.company_timezone
    if name and name in _valid_names():
        return ZoneInfo(name)
    if name not in _warned_company_tz:  # warn ONCE per bad value, not per call
        _warned_company_tz.add(name)
        logger.warning(
            "COMPANY_TIMEZONE {!r} is not a valid IANA zone — falling back to {}",
            name,
            DEFAULT_DISPLAY_TZ,
        )
    return ZoneInfo(DEFAULT_DISPLAY_TZ)


def local_day_sentinel(days_ahead: int = 0, now: datetime | None = None) -> datetime:
    """UTC-midnight sentinel for the CURRENT VIEWER's calendar day *days_ahead* days
    out.

    Task ``due_at`` values are calendar-date sentinels: the DATE half is the day the
    user means, stored at 00:00 UTC (``_parse_task_due_date``), and consumers read it
    back with ``due.date()`` — NEVER a zone conversion (a local-midnight instant would
    roll the date for east-of-UTC zones). This helper produces that sentinel for
    "today"/"tomorrow"/"N days out" with TODAY judged in the SAME zone
    ``_task_due_state`` buckets by — ``current_display_zoneinfo()``: the viewer's
    display_timezone, falling back to the company zone (no request context, e.g.
    background jobs, resolves to the company zone too). At 8:30pm Eastern (01:30 UTC
    next day) ``local_day_sentinel(1)`` is the next EASTERN day, where raw UTC math
    would skip a day; a Tokyo-configured viewer gets THEIR tomorrow. *now* defaults
    to the current instant; naive values are treated as stored-UTC (``as_utc``).
    """
    instant = as_utc(now) if now is not None else datetime.now(UTC)
    day = instant.astimezone(current_display_zoneinfo()).date() + timedelta(days=days_ahead)
    return datetime.combine(day, time.min, tzinfo=UTC)


def resolve_zoneinfo(name: str | None) -> ZoneInfo:
    """Return a ZoneInfo for *name*, or the company zone (``company_zoneinfo``) when it
    is not valid.

    ZoneInfo caches instances by key internally, so repeated calls are cheap.
    """
    if name and is_valid_timezone(name):
        return ZoneInfo(name)
    return company_zoneinfo()


def current_display_zoneinfo() -> ZoneInfo:
    """Resolve the CURRENT request viewer's zone from the contextvar (default
    fallback)."""
    return resolve_zoneinfo(current_user_display_tz_var.get())


@overload
def as_utc(dt: datetime) -> datetime: ...


@overload
def as_utc(dt: None) -> None: ...


def as_utc(dt: datetime | None) -> datetime | None:
    """Coerce a naive datetime to UTC-aware; aware datetimes and None pass through.

    Storage is UTC by convention — naive values are TAGGED as UTC, never converted.
    """
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def to_display_tz(dt: datetime | None, tz: ZoneInfo | str | None = None) -> datetime | None:
    """Convert a stored-UTC datetime into *tz* (default: the current viewer's zone).

    Naive datetimes are assumed UTC (the storage convention). Returns None for None.
    """
    if dt is None:
        return None
    if isinstance(tz, str) or tz is None:
        zone = resolve_zoneinfo(tz) if tz is not None else current_display_zoneinfo()
    else:
        zone = tz
    return as_utc(dt).astimezone(zone)


def format_localtime(
    dt: datetime | None,
    fmt: str = _DEFAULT_TIME_FMT,
    tz: ZoneInfo | str | None = None,
    default: str = "—",
) -> str:
    """Render *dt* (stored UTC) in the viewer's zone with *fmt*.

    None/invalid → *default*.
    """
    local = to_display_tz(dt, tz)
    if local is None:
        return default
    try:
        return local.strftime(fmt)
    except (AttributeError, TypeError, ValueError):
        return default


def format_localdate(
    dt: datetime | None,
    fmt: str = _DEFAULT_DATE_FMT,
    tz: ZoneInfo | str | None = None,
    default: str = "—",
) -> str:
    """Render *dt* (stored UTC) as a date in the viewer's zone.

    None/invalid → *default*.
    """
    return format_localtime(dt, fmt, tz, default)


@lru_cache(maxsize=1)
def grouped_timezones() -> list[tuple[str, list[str]]]:
    """IANA zones grouped by region for a profile ``<select>`` (``<optgroup>`` per
    region).

    Region = the segment before the first '/'. Single-segment zones (UTC, GMT) land in a
    trailing 'Other' group. Regions and members are each sorted for a stable select
    order.
    """
    groups: dict[str, list[str]] = {}
    for name in _valid_names():
        region, _, _rest = name.partition("/")
        key = region if _rest else "Other"
        groups.setdefault(key, []).append(name)
    ordered: list[tuple[str, list[str]]] = []
    for region in sorted(groups):
        if region == "Other":
            continue
        ordered.append((region, sorted(groups[region])))
    if "Other" in groups:
        ordered.append(("Other", sorted(groups["Other"])))
    return ordered
