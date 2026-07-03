"""Jinja2 template environment singleton with all custom filters.

Called by: all router files that render templates
Depends on: Jinja2
"""

from datetime import datetime, timezone
from typing import Any

from fastapi.templating import Jinja2Templates
from starlette.responses import Response

templates = Jinja2Templates(directory="app/templates")


def template_response(name: str, context: dict[str, Any], **kwargs: Any) -> Response:
    """Render a Jinja2 template using the shared `templates` instance.

    Wraps Jinja2Templates.TemplateResponse with the legacy `(name, context)`
    calling convention used across the codebase. Starlette 1.0 removed
    positional support for that form and now requires `request` as the first
    positional argument. This helper extracts `request` from the context dict
    (which already needs to be present so Jinja can render `{{ request.X }}`)
    and calls the new signature, so callers don't carry the boilerplate.
    """
    request = context.get("request")
    if request is None:
        raise ValueError("template_response: 'request' must be present in the context dict")
    return templates.TemplateResponse(request, name, context, **kwargs)


# Cache-Control header for full HTML page responses (the base_page shell).
# Browsers heuristically cache responses with no Cache-Control, so a normal
# reload can serve a stale shell pointing at old hashed-CSS/JS bundles after
# a deploy.  "no-cache" means "revalidate before serving from cache" — the
# browser may still store it but must check with the server first.
_PAGE_NO_CACHE_HEADERS: dict[str, str] = {"Cache-Control": "no-cache, must-revalidate"}


def page_response(context: dict[str, Any]) -> Response:
    """Render htmx/base_page.html with Cache-Control: no-cache, must-revalidate.

    All /v2/* full-page routes (the base_page shell) use this helper so that a normal
    browser reload picks up a newly deployed CSS/JS bundle instead of serving the stale
    shell from heuristic cache.  HTMX partial responses and /static/assets/* (Vite-
    hashed, immutable) are intentionally unaffected.
    """
    request = context.get("request")
    if request is None:
        raise ValueError("page_response: 'request' must be present in the context dict")
    # NOTE: the no-cache Cache-Control for full pages is applied in the security-headers
    # middleware (app/main.py) — the OUTERMOST middleware. Header sets on THIS TemplateResponse
    # are dropped by inner response processing before reaching the client (verified live), so
    # they must be applied at the middleware level. This helper is the single full-page entry.
    return templates.TemplateResponse(request, "htmx/base_page.html", context)


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


def _pricefmt_filter(value, default: str = "—") -> str:
    """Format a price: up to 4 decimals with trailing zeros stripped.

    0.7900 -> 0.79, 12.3400 -> 12.34, 5.0000 -> 5, sub-cent 0.0084 kept. Tames the
    Numeric(12,4) trailing-zero noise while preserving the precision component pricing needs.
    """
    if value is None or value == "":
        return default
    try:
        s = f"{float(value):.4f}".rstrip("0").rstrip(".")
        return s or "0"
    except (ValueError, TypeError):
        return default


templates.env.filters["pricefmt"] = _pricefmt_filter


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
            "blockquote",
            "pre",
            "code",
            "hr",
        },
        attributes={
            "a": {"href", "title", "target"},
            "td": {"colspan", "rowspan", "width", "height"},
            "th": {"colspan", "rowspan", "width", "height"},
        },
        url_schemes={"http", "https", "mailto"},
        link_rel="noopener noreferrer",
    )


templates.env.filters["sanitize_html"] = _sanitize_html_filter


def _sub_mpns_filter(subs: list | None) -> list[str]:
    """Extract clean uppercase MPN strings from substitutes.

    Handles both string-format and dict-format subs. Delegates to normalize_mpn() for
    consistent normalization.
    """
    from .utils.normalization import normalize_mpn

    if not subs:
        return []

    result = []
    for s in subs:
        if isinstance(s, str):
            raw = s
        elif isinstance(s, dict):
            raw = s.get("mpn") or ""
        else:
            raw = ""

        mpn = normalize_mpn(raw)
        if mpn:
            result.append(mpn)

    return result


templates.env.filters["sub_mpns"] = _sub_mpns_filter


def _fru_alias_mpns_filter(subs: list | None) -> set[str]:
    """Normalized MPNs of substitutes that were system-derived from the FRU crosswalk.

    Companion to |sub_mpns (same normalize_mpn display form, so membership tests against
    its output match) letting templates flag crosswalk-derived substitutes with a "via
    FRU crosswalk" tooltip — no new UI elements. Provenance is the optional "source" key
    written by search_service's alias expansion.
    """
    from .constants import FRU_ALIAS_SOURCE
    from .utils.normalization import normalize_mpn

    result: set[str] = set()
    for s in subs or []:
        if isinstance(s, dict) and s.get("source") == FRU_ALIAS_SOURCE:
            mpn = normalize_mpn(s.get("mpn") or "")
            if mpn:
                result.add(mpn)
    return result


templates.env.filters["fru_alias_mpns"] = _fru_alias_mpns_filter


def _part_description(obj) -> str:
    """Return part description from MaterialCard (source of truth) with fallback.

    Checks material_card.description first, then the object's own description field.
    Works with Requirement, QuoteLine, ExcessLineItem, or any object with a
    material_card relationship or description attribute.
    """
    # MaterialCard is the canonical source of truth
    card = getattr(obj, "material_card", None)
    if card:
        card_desc = getattr(card, "description", None)
        if card_desc and len(card_desc.strip()) >= 3:
            return card_desc.strip()
    # Fallback to the object's own description field
    own_desc = getattr(obj, "description", None)
    if own_desc and len(own_desc.strip()) >= 3:
        return own_desc.strip()
    return ""


templates.env.filters["part_description"] = _part_description


def _safe_url_filter(value: str | None, fallback: str = "#") -> str:
    """Return value only when it starts with http:// or https://, else fallback.

    Prevents stored javascript:/vbscript:/data: URLs from executing on click.
    Used in templates wherever a user-supplied URL is rendered as an href.
    """
    if not value:
        return fallback
    lowered = value.strip().lower()
    if lowered.startswith("https://") or lowered.startswith("http://"):
        return value
    return fallback


templates.env.filters["safe_url"] = _safe_url_filter


# ── Jinja2 Globals ──────────────────────────────────────────────────


def _now() -> datetime:
    """Current UTC time — for relative date grouping in templates (e.g. the requisition
    Activity tab's Today/Yesterday timeline headers)."""
    return datetime.now(timezone.utc)


templates.env.globals["now"] = _now


def _task_due_state(task, now_utc: datetime) -> tuple[bool, bool]:
    """Return (is_overdue, is_due_today) for a task row, coercing naive due_at to UTC.

    Centralises the comparison so templates never do datetime arithmetic directly, which
    would TypeError under SQLite when due_at is naive and now_utc is aware.
    """
    if task.due_at is None:
        return (False, False)
    due = task.due_at if task.due_at.tzinfo is not None else task.due_at.replace(tzinfo=timezone.utc)
    is_overdue = due <= now_utc
    is_due_today = not is_overdue and due.date() == now_utc.date()
    return (is_overdue, is_due_today)


templates.env.globals["task_due_state"] = _task_due_state

from .services.crm_service import cadence_state  # noqa: E402

templates.env.globals["cadence_state"] = cadence_state
# Canonical buying-role taxonomy exposed as a Jinja2 global so the role-select macros
# in _contact_macros.html can iterate roles inside macro/include contexts (which don't
# inherit template context). Context-level "roles" passed by set_contact_role /
# contacts_tab endpoints takes precedence; this global is the fallback for the
# macro-include path.
# Sourced from the ContactRole StrEnum (single source of truth in app/constants.py) —
# the SAME tuple CANONICAL_ROLES in app/routers/htmx_views.py is built from.
from .constants import ContactRole  # noqa: E402

_CANONICAL_ROLES = tuple(ContactRole)
templates.env.globals["roles"] = _CANONICAL_ROLES

# Buy-plan approval right exposed as a Jinja2 global so templates hide the approve/reject
# UI using the SAME predicate the require_buyplan_approver dependency enforces on the POST
# (single source of truth — the per-user User.can_approve_buy_plans column).
from .dependencies import can_approve_buy_plans, can_approve_purchase_orders, can_verify_po_line  # noqa: E402

templates.env.globals["can_approve_buy_plans"] = can_approve_buy_plans
# Purchase-order approval right exposed the same way: templates hide the verify-PO UI using
# the SAME predicate require_buyplan_po_approver enforces on the POST (Phase D —
# verify-PO moved off ops membership onto User.can_approve_purchase_orders).
templates.env.globals["can_approve_purchase_orders"] = can_approve_purchase_orders
# Per-line variant (Phase 3): same right PLUS the per-user purchase_order_approval_limit
# checked against THIS line's dollar amount — the SAME check verify_po enforces on the
# POST, so an over-limit line hides the Verify/Reject buttons instead of 403ing.
templates.env.globals["can_verify_po_line"] = can_verify_po_line

# CRM P5 trust — canonical industry pick-list exposed to the create/edit account
# forms (single source of truth in app/constants.py; the SAME tuple the inline
# editor + apply_company_field validate against).
from .constants import CRM_INDUSTRIES  # noqa: E402

templates.env.globals["crm_industries"] = CRM_INDUSTRIES

# CRM P5 trust — data-completeness scorer exposed as a Jinja2 global so the contact
# row macro (which doesn't inherit route context) can render a small completeness
# badge. Auto-dispatches on entity kind: Company → company_completeness, SiteContact
# → contact_completeness.
from .models.crm import Company as _Company  # noqa: E402
from .services.crm_completeness import company_completeness as _company_compl  # noqa: E402
from .services.crm_completeness import contact_completeness as _contact_compl  # noqa: E402


def _crm_completeness(obj):
    """Return the completeness dict for a Company or SiteContact."""
    if isinstance(obj, _Company):
        return _company_compl(obj)
    return _contact_compl(obj)


templates.env.globals["crm_completeness"] = _crm_completeness
