"""Jinja2 template environment singleton with all custom filters.

Called by: all router files that render templates
Depends on: Jinja2
"""

from datetime import UTC, datetime
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


def _elapsed_seconds(dt: datetime | str | None) -> float | None:
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
        dt = dt.replace(tzinfo=UTC)
    return (datetime.now(UTC) - dt).total_seconds()


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
        formatted: str = value.strftime(fmt)  # Jinja filter: value is untyped by design
        return formatted
    except (AttributeError, TypeError):
        return default


templates.env.filters["fmtdate"] = _fmtdate_filter


def _localtime_filter(value, fmt: str = "%b %d, %Y %H:%M", default: str = "—") -> str:
    """Render a stored-UTC datetime in the CURRENT viewer's timezone.

    The viewer's zone comes from the per-request contextvar (set from the user's
    ``display_timezone``), falling back to the business default when unknown. Storage
    stays UTC — this is display-only. Naive datetimes are treated as UTC. Strings /
    None fall through to ``default`` unchanged (mirrors ``_fmtdate_filter``).
    """
    from .utils.timezones import format_localtime

    if value is None or isinstance(value, str):
        return value if isinstance(value, str) else default
    return format_localtime(value, fmt, default=default)


templates.env.filters["localtime"] = _localtime_filter


def _localdate_filter(value, fmt: str = "%b %d, %Y", default: str = "—") -> str:
    """Render a stored-UTC datetime as a DATE in the current viewer's timezone.

    Companion to ``|localtime`` for date-only displays (e.g. "Member since"). Same
    contextvar-driven zone + UTC-fallback semantics.
    """
    from .utils.timezones import format_localdate

    if value is None or isinstance(value, str):
        return value if isinstance(value, str) else default
    return format_localdate(value, fmt, default=default)


templates.env.filters["localdate"] = _localdate_filter


def _localday_filter(value):
    """Return the calendar DATE of a stored-UTC datetime in the CURRENT viewer's
    timezone.

    Companion to ``|localtime``/``|localdate`` used for DAY-BUCKETING the activity/timeline
    feeds (the "Today"/"Yesterday"/date group headers). Taking ``.date()`` on the raw UTC
    datetime buckets a row on the UTC calendar day, so a viewer east or west of UTC can see
    a row fall under the wrong header at the midnight boundary — while its own rendered
    timestamp (already localized) sits in the next/prior day. This converts to the viewer's
    zone FIRST (the SAME contextvar the filters use), then takes ``.date()``, so the group
    header matches the row. Returns a ``date`` (comparable + subtractable for the day-delta),
    or ``None`` for ``None``/strings (the caller's ``if ts and ...`` guard skips those).
    """
    from .utils.timezones import to_display_tz

    if value is None or isinstance(value, str):
        return None
    local = to_display_tz(value)
    return local.date() if local is not None else None


templates.env.filters["localday"] = _localday_filter


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

    sanitized: str = nh3.clean(
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
    return sanitized


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
        card_desc: str | None = getattr(card, "description", None)
        if card_desc and len(card_desc.strip()) >= 3:
            return card_desc.strip()
    # Fallback to the object's own description field
    own_desc: str | None = getattr(obj, "description", None)
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
    return datetime.now(UTC)


templates.env.globals["now"] = _now


def _task_due_state(task, now_utc: datetime) -> tuple[bool, bool]:
    """Return (is_overdue, is_due_today) for a task row, coercing naive due_at to UTC.

    Task due dates are calendar dates (an ``<input type=date>`` stored at UTC midnight), so
    urgency is judged by *calendar day*, not by clock instant: a task due earlier today is
    still "due today", never "overdue". Overdue means the due date fell on a prior day. The
    two flags are therefore mutually exclusive, so the My Day filter and the results
    grouping — which both consume this one helper — can never disagree. Comparing dates
    (not datetimes) also sidesteps the naive/aware TypeError under SQLite.

    "Today" is the CURRENT VIEWER's local day (``current_display_zoneinfo()`` — their
    ``display_timezone``, falling back to the business default when unknown): ``now`` is a
    real instant, so it is converted before taking its date. A buyer in Asia/Tokyo near UTC
    midnight therefore sees a task due on THEIR calendar day as "today", not "overdue".
    ``due_at`` is NOT converted — it is a UTC-midnight sentinel for the calendar date the
    user picked, and shifting it into another zone would roll it off by a day; ``due.date()``
    already yields that picked date.
    """
    from .utils.timezones import current_display_zoneinfo

    if task.due_at is None:
        return (False, False)
    due = task.due_at if task.due_at.tzinfo is not None else task.due_at.replace(tzinfo=UTC)
    due_date = due.date()
    today = now_utc.astimezone(current_display_zoneinfo()).date()
    is_overdue = due_date < today
    is_due_today = due_date == today
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
from .dependencies import (  # noqa: E402
    can_approve_buy_plans,
    can_approve_purchase_orders,
    can_request_prepayment,
    can_verify_po_line,
)

templates.env.globals["can_approve_buy_plans"] = can_approve_buy_plans
# Purchase-order approval right exposed the same way: templates hide the verify-PO UI using
# the SAME predicate require_buyplan_po_approver enforces on the POST (Phase D —
# verify-PO moved off ops membership onto User.can_approve_purchase_orders).
templates.env.globals["can_approve_purchase_orders"] = can_approve_purchase_orders
# Per-line variant (Phase 3): same right PLUS the per-user purchase_order_approval_limit
# checked against THIS line's dollar amount — the SAME check verify_po enforces on the
# POST, so an over-limit line hides the Verify/Reject buttons instead of 403ing.
templates.env.globals["can_verify_po_line"] = can_verify_po_line
# Prepayment request predicate: the "Request prepayment" button on a cut PO hides using the
# SAME ownership + cut-PO rule create_prepayment enforces on the request (a restricted-role
# non-owner or a line without a live PO gets no button instead of a 404/400 on submit).
templates.env.globals["can_request_prepayment"] = can_request_prepayment

# QP section review rights (Phase 3 decision C): the QP Sales/Purchasing "Mark Reviewed"
# controls hide using the SAME per-user predicates toggle_section_reviewed enforces on the
# POST (reusing the can_approve_qp_sales / can_approve_qp_purchasing columns).
from .dependencies import can_review_qp_purchasing_section, can_review_qp_sales_section  # noqa: E402

templates.env.globals["can_review_qp_sales_section"] = can_review_qp_sales_section
templates.env.globals["can_review_qp_purchasing_section"] = can_review_qp_purchasing_section

# Stale-edit guard (Approvals Workspace D5): edit forms embed the SAME token
# ensure_not_stale checks on the POST — {{ stale_token(obj) }} in a hidden
# expected_updated_at input, single source of truth in services/stale_guard.py.
from .services.stale_guard import stale_token  # noqa: E402

templates.env.globals["stale_token"] = stale_token

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

# Score/price hover layer (idea C) — deterministic prospect fit/readiness factor
# breakdowns exposed as Jinja globals so the prospect card (rendered from the list AND
# from claim/dismiss OOB swaps, none of which share route context) can render the value
# hover without every endpoint re-assembling the breakdown. Each returns an ordered
# list of (label, contribution) derived from the SAME weights the score uses.
from .services.prospect_scoring import fit_breakdown_for_prospect as _fit_breakdown  # noqa: E402
from .services.prospect_scoring import readiness_breakdown_for_prospect as _readiness_breakdown  # noqa: E402

templates.env.globals["prospect_fit_breakdown"] = _fit_breakdown
templates.env.globals["prospect_readiness_breakdown"] = _readiness_breakdown
