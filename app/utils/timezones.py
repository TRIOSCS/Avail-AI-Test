"""Per-user timezone display layer — convert stored-UTC datetimes to a viewer's zone.

Storage stays UTC (the ``UTCDateTime`` convention is unchanged). This module is the
single mechanism for rendering those UTC instants in a specific user's IANA timezone:

  - ``is_valid_timezone`` / ``resolve_zoneinfo`` — validate + resolve an IANA name.
  - ``current_display_zoneinfo`` — the CURRENT request's viewer zone, read from the
    ``current_user_display_tz_var`` contextvar (set by ``require_user``), falling back
    to ``DEFAULT_DISPLAY_TZ`` when unknown.
  - ``to_display_tz`` / ``format_localtime`` / ``format_localdate`` — convert/format a
    UTC datetime, defaulting to the current viewer zone but accepting an explicit zone
    (for server-side use like emails, where there is no request contextvar).

Called by: app/template_env.py (the ``localtime``/``localdate`` Jinja filters and
    ``_task_due_state``), app/routers/htmx/settings.py (the timezone endpoint),
    app/dependencies.py (populating the contextvar). Reusable by services/emails.
Depends on: stdlib zoneinfo + app/request_context.py (pure stdlib).
"""

from __future__ import annotations

from datetime import UTC, datetime
from functools import lru_cache
from zoneinfo import ZoneInfo, available_timezones

from ..request_context import current_user_display_tz_var

# Fallback zone when a viewer's timezone is unknown (NULL display_timezone / no request
# context / an invalid stored value). America/New_York is the business operating zone —
# the SAME zone the background workers hard-code and the buyplan auto-complete default
# (config.buyplan_auto_complete_tz) use — so an un-detected user's "today"/"overdue" and
# rendered timestamps match the prior app-wide behaviour rather than jumping to UTC.
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


def resolve_zoneinfo(name: str | None) -> ZoneInfo:
    """Return a ZoneInfo for *name*, or the DEFAULT_DISPLAY_TZ zone when it is not
    valid.

    ZoneInfo caches instances by key internally, so repeated calls are cheap.
    """
    if name and is_valid_timezone(name):
        return ZoneInfo(name)
    return ZoneInfo(DEFAULT_DISPLAY_TZ)


def current_display_zoneinfo() -> ZoneInfo:
    """Resolve the CURRENT request viewer's zone from the contextvar (default
    fallback)."""
    return resolve_zoneinfo(current_user_display_tz_var.get())


def _as_utc(dt: datetime) -> datetime:
    """Coerce a naive datetime to UTC-aware; pass aware datetimes through unchanged."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


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
    return _as_utc(dt).astimezone(zone)


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
