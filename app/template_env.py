"""Jinja2 template environment singleton with all custom filters.

Called by: all router files that render templates
Depends on: Jinja2
"""

from datetime import datetime, timezone

from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory="app/templates")


# ── Shared Helpers ─────────────────────────────────────────────────────


def _elapsed_seconds(dt) -> float | None:
    """Compute seconds elapsed since dt.

    Handles str, naive, and aware datetimes.
    """
    if not dt:
        return None
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt)
        except (ValueError, TypeError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds()


# ── Custom Jinja2 Filters ───────────────────────────────────────────


def _timesince_filter(dt):
    """Convert datetime to human-readable relative time string."""
    seconds = _elapsed_seconds(dt)
    if seconds is None:
        return ""
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        mins = int(seconds // 60)
        return f"{mins} min ago"
    if seconds < 86400:
        hours = int(seconds // 3600)
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = int(seconds // 86400)
    if days == 1:
        return "1 day ago"
    return f"{days} days ago"


templates.env.filters["timesince"] = _timesince_filter


def _timeago_filter(dt):
    """Compact relative time: '2h ago', '3d ago', '2w ago'."""
    seconds = _elapsed_seconds(dt)
    if seconds is None:
        return "--"
    seconds = int(seconds)
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    if days < 7:
        return f"{days}d ago"
    weeks = days // 7
    if weeks < 5:
        return f"{weeks}w ago"
    months = days // 30
    return f"{months}mo ago"


templates.env.filters["timeago"] = _timeago_filter


def _fmtdate_filter(value, fmt: str = "%b %d, %H:%M", default: str = "\u2014") -> str:
    """Safe date formatter — handles None, strings, and datetime objects."""
    if not value:
        return default
    if isinstance(value, str):
        return value
    try:
        return value.strftime(fmt)
    except (AttributeError, TypeError):
        return default


templates.env.filters["fmtdate"] = _fmtdate_filter


def _sanitize_html_filter(value: str) -> str:
    """Sanitize HTML to prevent XSS — allows safe formatting tags only."""
    if not value:
        return ""
    import nh3

    return nh3.clean(
        value,
        tags={
            "p",
            "br",
            "div",
            "span",
            "table",
            "tr",
            "td",
            "th",
            "thead",
            "tbody",
            "a",
            "b",
            "i",
            "strong",
            "em",
            "ul",
            "ol",
            "li",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "img",
            "blockquote",
            "pre",
            "code",
            "hr",
        },
        attributes={
            "*": {"class"},
            "a": {"href", "title", "target"},
            "img": {"src", "alt", "title", "width", "height"},
            "td": {"colspan", "rowspan", "width", "height"},
            "th": {"colspan", "rowspan", "width", "height"},
        },
        url_schemes={"http", "https", "mailto"},
    )


templates.env.filters["sanitize_html"] = _sanitize_html_filter
