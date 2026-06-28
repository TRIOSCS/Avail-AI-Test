"""routers/htmx_views.py — HTMX + Alpine.js MVP frontend views.

Serves server-rendered HTML partials for the HTMX-based frontend.
Full page loads render base.html; HTMX requests get just the partial.
All routes live under /v2 to coexist with the original SPA frontend.

Called by: main.py (router mount)
Depends on: models, dependencies, database, search_service
"""

import asyncio
import html as html_mod
import json
import os
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from loguru import logger
from sqlalchemy import case, desc, or_, select
from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session, joinedload, selectinload

from ..constants import (
    RESTRICTED_ROLES,
    AccessKey,
    ActivityType,
    ApiSourceStatus,
    AttributionStatus,
    BuyPlanStatus,
    ContactStatus,
    OfferStatus,
    ProactiveMatchStatus,
    ProspectAccountStatus,
    QuoteStatus,
    RequisitionStatus,
    SourcingStatus,
    TaskStatus,
    TicketSource,
    TicketStatus,
    UserRole,
    VendorResponseStatus,
)
from ..database import get_db
from ..dependencies import (
    can_approve_buy_plans,
    can_manage_account,
    get_buyplan_for_user,
    get_quote_for_user,
    get_req_for_user,
    get_user,
    has_buyer_role,
    is_manager_or_admin,
    require_access,
    require_admin,
    require_buyer,
    require_buyplan_approver,
    require_requisition_access,
    require_user,
    user_has_access,
)
from ..models import (
    ApiSource,
    BuyPlan,
    BuyPlanLine,
    Company,
    CustomerSite,
    Offer,
    Quote,
    QuoteLine,
    Requirement,
    Requisition,
    RequisitionTask,
    Sighting,
    SiteContact,
    SourcingLead,
    User,
    VendorCard,
    VerificationGroupMember,
)
from ..models.faceted_search import CommoditySpecSchema
from ..models.prospect_account import ProspectAccount
from ..models.vendor_sighting_summary import VendorSightingSummary
from ..scoring import classify_lead, explain_lead, score_unified
from ..services import clay_oauth, task_service
from ..services.activity_service import log_activity as _log_activity
from ..services.buyplan_naming import summarize_top_flag
from ..services.commodity_registry import COMMODITY_TREE, get_display_name
from ..services.faceted_search_service import (
    INTERNAL_FILTER_VALUES,
    SEARCHED_WITHIN_VALUES,
    get_commodity_counts,
    get_commodity_spec_coverage,
    get_facet_counts,
    get_global_facet_counts,
    get_subfilter_options,
    search_materials_faceted,
)
from ..services.part_history_service import (
    customer_purchases_for_card,
    offers_for_card,
    requirements_for_card,
    sightings_for_card,
)
from ..services.prospect_priority import build_priority_snapshot, build_signal_tags, contacts_summary
from ..services.quote_send import (
    QuoteSendDNCBlocked,
    QuoteSendError,
    send_quote_email,
)
from ..services.sighting_aggregation import get_vendor_tier_map
from ..services.sighting_ingest import sighting_from_row
from ..services.status_machine import require_valid_transition
from ..services.vendor_unavailability import apply_to_fresh_sightings, maybe_release_on_offer
from ..template_env import page_response, template_response, templates
from ..utils.search_builder import SearchBuilder
from ..utils.sql_helpers import escape_like
from ._lookup_helpers import get_requisition_or_404, get_vendor_card_or_404
from .auth import _password_login_enabled
from .htmx._shared import _base_ctx, _parse_date_safe, _vite_assets
from .htmx.companies import company_tab
from .htmx.requisitions import requisition_tab, requisitions_list_partial
from .htmx.vendors import vendor_tab

router = APIRouter(tags=["htmx-views"])

# Nav-id aliases: routes that were demoted into a parent nav item highlight the parent
# instead. Empty now: the standalone Quotes list redirects to /v2/requisitions and the
# Reporting surface was retired, so no view needs to borrow another tab's highlight.
# Quote detail (/v2/quotes/{id}) falls through to "quotes", which matches no nav item —
# correct, since it has no parent tab to highlight.
# The global contact lists live under the CRM nav item (twins of Customers/Vendors),
# so they borrow the "crm" highlight.
_NAV_ID_ALIAS: dict[str, str] = {"contacts": "crm", "vendor-contacts": "crm", "approvals": "buy-plans"}


def _is_htmx(request: Request) -> bool:
    """Check if this is an HTMX partial request (vs full page load)."""
    return request.headers.get("HX-Request") == "true"


def _parse_filter_json(raw: str, *, coerce_numeric: bool = False) -> dict:
    """Parse a JSON filter string into a dict, returning {} on failure.

    When coerce_numeric=True, keys ending in _min/_max are cast to float.
    """
    try:
        parsed: dict = json.loads(raw) if raw else {}
    except (ValueError, TypeError):
        return {}
    if not coerce_numeric:
        return parsed
    result: dict = {}
    for key, val in parsed.items():
        if key.endswith("_min") or key.endswith("_max"):
            try:
                result[key] = float(val)
            except (ValueError, TypeError):
                pass
        else:
            result[key] = val
    return result


def _pop_manufacturers(parsed_filters: dict) -> list[str] | None:
    """Pop the 'manufacturers' key out of a parsed sub_filters dict.

    'manufacturers' is a MaterialCard column (the combined dual-brand facet), not a spec
    facet — left in the dict it would zero every spec-facet count. Shared by the faceted
    results route and both sidebar count routes.
    """
    if not parsed_filters:
        return None
    mfr_val = parsed_filters.pop("manufacturers", None)
    if not mfr_val:
        return None
    return mfr_val if isinstance(mfr_val, list) else [mfr_val]


def _parse_card_filter_params(
    statuses: str,
    lifecycle: str,
    rohs: str,
    condition: str,
    has_datasheet: str,
    has_validation_conflict: str,
    has_stock: str,
    has_price: str,
    has_crosses: str,
    internal: str,
    searched_within: str,
    min_searches: str,
) -> dict:
    """Parse the card-level faceted filter params shared by the results-list route and
    BOTH sidebar count routes (sub-filters + global), so the list and the counts can
    never read the same query string differently.

    Unknown/invalid values (incl. non-numeric/negative min_searches and the boolean
    flags) degrade to the no-op default — hand-edited URLs must not 500/422 (a 422
    partial never swaps, htmx shows only the generic error toast) — but each degrade is
    LOGGED so frontend/backend vocabulary drift (e.g. a bucket added to the UI but not
    the backend constants) surfaces in logs instead of silently no-op'ing the filter
    while the active-filter chip claims it is applied.

    Returns keyword args for faceted_search_service (minus commodity / q / sub_filters /
    manufacturers, which each route binds itself).
    """

    def _csv_list(raw: str) -> list[str] | None:
        items = [s.strip() for s in raw.split(",") if s.strip()]
        return items or None

    def _flag(name: str, raw: str) -> bool:
        val = raw.strip().lower()
        if val in {"true", "1", "yes", "on"}:
            return True
        if val not in {"false", "0", "", "no", "off"}:
            logger.warning("materials faceted: invalid {}={!r}, degrading to false", name, raw)
        return False

    def _choice(name: str, raw: str, valid: tuple[str, ...], default: str) -> str:
        if raw in valid:
            return raw
        logger.warning("materials faceted: unknown {}={!r}, degrading to {!r}", name, raw, default)
        return default

    try:
        min_searches_n = int(min_searches)
    except ValueError:
        min_searches_n = -1
    if min_searches_n < 0:
        logger.warning("materials faceted: invalid min_searches={!r}, degrading to 0", min_searches)
        min_searches_n = 0

    return {
        "statuses": _csv_list(statuses),
        "lifecycle": _csv_list(lifecycle),
        "rohs": _csv_list(rohs),
        "condition": _csv_list(condition),
        "has_datasheet": _flag("has_datasheet", has_datasheet),
        "has_validation_conflict": _flag("has_validation_conflict", has_validation_conflict),
        "has_stock": _flag("has_stock", has_stock),
        "has_price": _flag("has_price", has_price),
        "has_crosses": _flag("has_crosses", has_crosses),
        "internal": _choice("internal", internal, INTERNAL_FILTER_VALUES, "all"),
        "searched_within": _choice("searched_within", searched_within, SEARCHED_WITHIN_VALUES, "any"),
        "min_searches": min_searches_n,
    }


# ── Full page entry points ──────────────────────────────────────────────


@router.get("/v2/quotes")
async def quotes_list_redirect():
    """Standalone Quotes list retired — quotes now live on the requirement (Reqs
    workspace Quotes tab) and the CRM account (Quotes tab).

    Kept as a
    redirect so stale bookmarks/links land somewhere sensible.
    Called by: browser navigation to the old /v2/quotes URL.
    """
    from fastapi.responses import RedirectResponse

    return RedirectResponse(url="/v2/requisitions", status_code=307)


# Full-page module access gate (Phase 4b). Maps a resolved current_view to the AccessKey
# that gates it. Views NOT present here (settings, quotes, follow-ups, trouble-tickets)
# are never gated. CRM sub-views (customers/contacts/vendors/...) all gate on CRM.
_VIEW_ACCESS: dict[str, AccessKey] = {
    "requisitions": AccessKey.REQUISITIONS,
    "sightings": AccessKey.SIGHTINGS,
    "materials": AccessKey.MATERIALS,
    "search": AccessKey.SEARCH,
    "buy-plans": AccessKey.BUY_PLANS,
    "approvals": AccessKey.BUY_PLANS,
    "resell": AccessKey.RESELL,
    "crm": AccessKey.CRM,
    "customers": AccessKey.CRM,
    "contacts": AccessKey.CRM,
    "vendors": AccessKey.CRM,
    "vendor-contacts": AccessKey.CRM,
    "proactive": AccessKey.PROACTIVE,
    "prospecting": AccessKey.PROSPECTING,
    "my-day": AccessKey.MY_DAY,
}

# Ordered (AccessKey, full-page url) list in MODULE order. When a user is denied the view
# they requested, v2_page redirects to the FIRST module in this list they are allowed —
# the target is always an allowed view, so no redirect loop is possible.
_MODULE_ENTRY_URLS: tuple[tuple[AccessKey, str], ...] = (
    (AccessKey.REQUISITIONS, "/v2/requisitions"),
    (AccessKey.SIGHTINGS, "/v2/sightings"),
    (AccessKey.MATERIALS, "/v2/materials"),
    (AccessKey.SEARCH, "/v2/search"),
    (AccessKey.BUY_PLANS, "/v2/buy-plans"),
    (AccessKey.RESELL, "/v2/resell"),
    (AccessKey.CRM, "/v2/crm"),
    (AccessKey.PROACTIVE, "/v2/proactive"),
    (AccessKey.PROSPECTING, "/v2/prospecting"),
    (AccessKey.MY_DAY, "/v2/my-day"),
)


@router.get("/v2", response_class=HTMLResponse)
@router.get("/v2/requisitions", response_class=HTMLResponse)
@router.get("/v2/requisitions/{req_id:int}", response_class=HTMLResponse)
@router.get("/v2/search", response_class=HTMLResponse)
@router.get("/v2/vendors", response_class=HTMLResponse)
@router.get("/v2/vendors/{vendor_id:int}", response_class=HTMLResponse)
@router.get("/v2/customers", response_class=HTMLResponse)
@router.get("/v2/customers/{company_id:int}", response_class=HTMLResponse)
@router.get("/v2/contacts", response_class=HTMLResponse)
@router.get("/v2/vendor-contacts", response_class=HTMLResponse)
@router.get("/v2/approvals", response_class=HTMLResponse)
@router.get("/v2/buy-plans/{bp_id:int}", response_class=HTMLResponse)
@router.get("/v2/resell", response_class=HTMLResponse)
@router.get("/v2/resell/{list_id:int}", response_class=HTMLResponse)
@router.get("/v2/quotes/{quote_id:int}", response_class=HTMLResponse)
@router.get("/v2/settings", response_class=HTMLResponse)
@router.get("/v2/prospecting", response_class=HTMLResponse)
@router.get("/v2/prospecting/{prospect_id:int}", response_class=HTMLResponse)
@router.get("/v2/proactive", response_class=HTMLResponse)
@router.get("/v2/materials", response_class=HTMLResponse)
@router.get("/v2/materials/{card_id:int}", response_class=HTMLResponse)
@router.get("/v2/follow-ups", response_class=HTMLResponse)
@router.get("/v2/crm", response_class=HTMLResponse)
@router.get("/v2/sightings", response_class=HTMLResponse)
@router.get("/v2/trouble-tickets", response_class=HTMLResponse)
@router.get("/v2/trouble-tickets/{ticket_id:int}", response_class=HTMLResponse)
@router.get("/v2/my-day", response_class=HTMLResponse)
async def v2_page(request: Request, db: Session = Depends(get_db)):
    """Full page load — serves base.html with initial content via HTMX."""

    path = request.url.path
    user = get_user(request, db)
    if not user:
        return template_response(
            "htmx/login.html", {"request": request, "password_login": _password_login_enabled(), **_vite_assets()}
        )
    # First matching segment wins — order is load-bearing (e.g. /buy-plans before
    # /requisitions). Anything unmatched defaults to the requisitions view.
    _VIEW_SEGMENTS = (
        "approvals",
        "buy-plans",
        "resell",
        "quotes",
        "prospecting",
        "proactive",
        "settings",
        "materials",
        "follow-ups",
        "trouble-tickets",
        "my-day",
        "crm",
        # "vendor-contacts" / "contacts" must precede "vendors" / "customers" — the
        # match is a substring test and "/contacts" is contained in "/vendor-contacts".
        "vendor-contacts",
        "vendors",
        "contacts",
        "customers",
        "search",
        "sightings",
        "requisitions",
    )
    current_view = next((seg for seg in _VIEW_SEGMENTS if f"/{seg}" in path), "requisitions")

    # Trouble-ticket console is admin-only — non-admins get a clean 403 instead of
    # a page shell whose inner (admin-gated) partial would 403 on load.
    if current_view == "trouble-tickets" and user.role != UserRole.ADMIN:
        raise HTTPException(403, "Admin access required")

    # Module access gate (Phase 4b). If the requested view maps to a module the user may
    # not see, redirect to their first allowed module (admins always pass user_has_access
    # so they never redirect). Only the REQUESTED view is checked — the redirect target is
    # always an allowed view, so no loop is possible. Views absent from _VIEW_ACCESS
    # (settings, quotes, follow-ups, trouble-tickets) are never gated.
    gate_key = _VIEW_ACCESS.get(current_view)
    if gate_key is not None and not user_has_access(user, gate_key, db):
        from fastapi.responses import RedirectResponse

        target = next((url for key, url in _MODULE_ENTRY_URLS if user_has_access(user, key, db)), None)
        if target is not None:
            return RedirectResponse(target, status_code=302)
        return HTMLResponse(
            "<p>You don't have access to any sections. Contact an administrator.</p>"
            '<p><a href="/auth/logout">Log out</a></p>',
            status_code=403,
        )

    # Determine the correct partial URL for initial content load
    if current_view == "requisitions":
        # Split-panel workspace is the new default for requisitions
        partial_url = "/v2/partials/parts/workspace"
    elif current_view == "trouble-tickets":
        partial_url = "/v2/partials/trouble-tickets/workspace"
    elif current_view == "crm":
        partial_url = "/v2/partials/crm/shell"
    elif current_view == "sightings":
        partial_url = "/v2/partials/sightings/workspace"
    elif current_view == "resell":
        partial_url = "/v2/partials/resell/workspace"
    elif current_view == "my-day":
        partial_url = "/v2/partials/my-day"
    elif current_view == "search":
        # Deep-link the Part Dossier: ?mpn= rides along to /v2/partials/search so a
        # bookmarked /v2/search?mpn=<PN> paints the dossier on first load.
        mpn_qs = request.query_params.get("mpn", "").strip()
        partial_url = f"/v2/partials/search?mpn={quote(mpn_qs)}" if mpn_qs else "/v2/partials/search"
    elif current_view == "settings":
        # Thread ?tab= through so a deep-link / redirect (e.g. the legacy
        # /v2/trouble-tickets → /v2/settings?tab=tickets) paints the right tab on
        # first full-page load instead of defaulting to Connectors.
        tab_qs = request.query_params.get("tab", "").strip()
        partial_url = f"/v2/partials/settings?tab={quote(tab_qs)}" if tab_qs else "/v2/partials/settings"
    elif current_view in ("buy-plans", "approvals"):
        # Thread ?lens= through so a deep-link / redirect and a reload/bookmark of a pushed
        # stage URL paint the right stage tab on first full-page load instead of falling to
        # _default_lens. Lens keys are the five lifecycle stages. A detail URL
        # (/buy-plans/{id}) is overridden by the _DETAIL_VIEWS block below.
        lens_qs = request.query_params.get("lens", "").strip()
        partial_url = (
            f"/v2/partials/approvals?lens={quote(lens_qs)}"
            if lens_qs in ("sales_orders", "buy_plans", "purchase_orders", "prepayments", "supervise")
            else "/v2/partials/approvals"
        )
    else:
        partial_url = f"/v2/partials/{current_view}"
    # Detail views: a trailing numeric id (/{view}/{id}) overrides the list partial with
    # the detail partial. Each split key equals the current_view, so at most one applies.
    _DETAIL_VIEWS = (
        "requisitions",
        "vendors",
        "customers",
        "buy-plans",
        "resell",
        "quotes",
        "prospecting",
        "trouble-tickets",
    )
    if current_view in _DETAIL_VIEWS and f"/{current_view}/" in path:
        parts = path.split(f"/{current_view}/")
        if len(parts) > 1 and parts[1].isdigit():
            partial_url = f"/v2/partials/{current_view}/{parts[1]}"
            # Thread ?tab= through for customer deep-links so the partial lands on
            # the correct tab when the full page is (re)loaded from a pushed URL.
            if current_view == "customers":
                _tab_qs = request.query_params.get("tab", "").strip()
                if _tab_qs:
                    partial_url = f"{partial_url}?tab={quote(_tab_qs)}"

    nav_active = _NAV_ID_ALIAS.get(current_view, current_view)
    ctx = _base_ctx(request, user, nav_active)
    ctx["partial_url"] = partial_url
    return page_response(ctx)


@router.get("/v2/buy-plans", response_class=HTMLResponse)
async def buy_plans_legacy_redirect(request: Request):
    """302 the legacy Buy Plans URL to the renamed Approvals module (query preserved).

    The hub was renamed Buy Plans → Approvals (SP-1); old bookmarks / pushed lens URLs
    keep working via this redirect. Detail URLs (/v2/buy-plans/{id}) are unchanged and
    still served directly by ``v2_page``.
    """
    from fastapi.responses import RedirectResponse

    qs = request.url.query
    return RedirectResponse(f"/v2/approvals?{qs}" if qs else "/v2/approvals", status_code=302)


# ── Global search ──────────────────────────────────────────────────────


@router.get("/v2/partials/search/global", response_class=HTMLResponse)
async def global_search(
    request: Request,
    q: str = "",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Global search across all entity types (type-ahead)."""
    from app.services.global_search_service import fast_search

    results = fast_search(q, db, user)
    return template_response(
        "htmx/partials/shared/search_results.html",
        {**_base_ctx(request, user), "results": results, "query": q},
    )


@router.post("/v2/partials/search/ai", response_class=HTMLResponse)
async def ai_search_endpoint(
    request: Request,
    q: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """AI-powered search — triggered by Enter key."""
    from app.services.global_search_service import ai_search

    results = await ai_search(q, db, user)
    return template_response(
        "htmx/partials/shared/search_results.html",
        {**_base_ctx(request, user), "results": results, "query": q, "ai_search": True},
    )


@router.get("/v2/partials/search/results", response_class=HTMLResponse)
async def search_results_page(
    request: Request,
    q: str = "",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Full search results page."""
    from app.services.global_search_service import fast_search

    results = fast_search(q, db, user) if q else {"best_match": None, "groups": {}, "total_count": 0}
    return template_response(
        "htmx/partials/search/full_results.html",
        {**_base_ctx(request, user), "results": results, "query": q},
    )


# ── Parts workspace (split-panel entry point) ─────────────────────────


@router.get("/v2/partials/parts/workspace", response_class=HTMLResponse)
async def parts_workspace_partial(
    request: Request,
    user: User = Depends(require_access(AccessKey.REQUISITIONS)),
    db: Session = Depends(get_db),
):
    """Return the split-panel parts workspace shell."""
    from ..services import forecast_service

    ctx = _base_ctx(request, user, "requisitions")
    ctx["pipeline"] = forecast_service.pipeline_summary(db)
    return template_response("htmx/partials/parts/workspace.html", ctx)


# ── AI Digest Endpoints ───────────────────────────────────────────────────────


@router.get("/v2/partials/requisitions/{req_id}/activity-digest", response_class=HTMLResponse)
async def requisition_activity_digest(
    request: Request,
    req_id: int,
    force: int = 0,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """AI digest card for a requisition's activity timeline (HTMX lazy-load)."""
    from ..constants import DigestEntityType
    from ..services.activity_digest_service import get_or_build_digest

    get_requisition_or_404(db, req_id)
    digest = await get_or_build_digest(DigestEntityType.REQUISITION, req_id, db, force=bool(force))
    ctx = _base_ctx(request, user, "requisitions")
    ctx["digest"] = digest
    ctx["refresh_url"] = f"/v2/partials/requisitions/{req_id}/activity-digest"
    return template_response("htmx/partials/shared/activity_digest_card.html", ctx)


@router.get("/v2/partials/customers/{company_id}/activity-digest", response_class=HTMLResponse)
async def customer_activity_digest(
    request: Request,
    company_id: int,
    force: int = 0,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """AI digest card for a company's activity timeline (HTMX lazy-load)."""
    from ..models import Company

    if not db.get(Company, company_id):
        raise HTTPException(404, "Company not found")

    from ..constants import DigestEntityType
    from ..services.activity_digest_service import get_or_build_digest

    digest = await get_or_build_digest(DigestEntityType.COMPANY, company_id, db, force=bool(force))
    ctx = _base_ctx(request, user, "customers")
    ctx["digest"] = digest
    ctx["refresh_url"] = f"/v2/partials/customers/{company_id}/activity-digest"
    return template_response("htmx/partials/shared/activity_digest_card.html", ctx)


# ── Column Prefs Save Endpoints ──────────────────────────────────────────────


# ── AI Parsing in Requisition Offers (Phase 3B) ───────────────────────


@router.get("/v2/partials/requisitions/{req_id}/parse-email-form", response_class=HTMLResponse)
async def parse_email_form(
    request: Request,
    req_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return the parse-email paste form."""
    req = get_requisition_or_404(db, req_id)
    ctx = _base_ctx(request, user, "requisitions")
    ctx["req"] = req
    return template_response("htmx/partials/requisitions/tabs/parse_email_form.html", ctx)


@router.get("/v2/partials/requisitions/{req_id}/paste-offer-form", response_class=HTMLResponse)
async def paste_offer_form(
    request: Request,
    req_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return the paste-offer freeform form."""
    req = get_requisition_or_404(db, req_id)
    ctx = _base_ctx(request, user, "requisitions")
    ctx["req"] = req
    return template_response("htmx/partials/requisitions/tabs/paste_offer_form.html", ctx)


@router.post("/v2/partials/requisitions/{req_id}/parse-email", response_class=HTMLResponse)
async def parse_email_action(
    request: Request,
    req_id: int,
    email_body: str = Form(""),
    email_subject: str = Form(""),
    vendor_name: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Parse vendor email and return editable offer cards."""
    req = get_requisition_or_404(db, req_id)
    require_requisition_access(db, req_id, user)

    if not email_body.strip():
        return HTMLResponse(
            '<div class="p-4 text-center text-sm text-amber-600 bg-amber-50 rounded-lg border border-amber-200">'
            "Please paste the email body to parse.</div>"
        )

    ctx = _base_ctx(request, user, "requisitions")
    ctx["req"] = req
    ctx["vendor_name"] = vendor_name

    try:
        from app.services.ai_email_parser import parse_email

        result = await parse_email(
            email_body=email_body,
            email_subject=email_subject,
            vendor_name=vendor_name,
        )

        if not result:
            ctx["quotes"] = []
            ctx["overall_confidence"] = 0
            ctx["email_type"] = "unclear"
        else:
            ctx["quotes"] = result.get("quotes", [])
            ctx["overall_confidence"] = result.get("overall_confidence", 0)
            ctx["email_type"] = result.get("email_type", "unclear")

    except Exception as exc:
        logger.error(f"Parse email error for req {req_id}: {exc}")
        return HTMLResponse(
            '<div class="p-4 text-center text-sm text-rose-600 bg-rose-50 rounded-lg border border-rose-200">'
            f"Parse failed: {exc}</div>"
        )

    return template_response("htmx/partials/requisitions/tabs/parsed_email_results.html", ctx)


@router.post("/v2/partials/requisitions/{req_id}/parse-offer", response_class=HTMLResponse)
async def parse_offer_action(
    request: Request,
    req_id: int,
    raw_text: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Parse freeform vendor text and return editable offer cards."""
    req = get_requisition_or_404(db, req_id)
    require_requisition_access(db, req_id, user)

    if not raw_text.strip():
        return HTMLResponse(
            '<div class="p-4 text-center text-sm text-amber-600 bg-amber-50 rounded-lg border border-amber-200">'
            "Please paste vendor text to parse.</div>"
        )

    # Build RFQ context for better matching
    reqs = db.query(Requirement).filter(Requirement.requisition_id == req_id).all()
    rfq_context = [{"mpn": r.primary_mpn, "qty": r.target_qty or 1} for r in reqs if r.primary_mpn]

    ctx = _base_ctx(request, user, "requisitions")
    ctx["req"] = req

    try:
        from app.services.freeform_parser_service import parse_freeform_offer

        result = await parse_freeform_offer(raw_text, rfq_context)
        if not result:
            ctx["offers"] = []
        else:
            ctx["offers"] = result.get("offers", [])
    except Exception as exc:
        logger.error(f"Parse offer error for req {req_id}: {exc}")
        return HTMLResponse(
            '<div class="p-4 text-center text-sm text-rose-600 bg-rose-50 rounded-lg border border-rose-200">'
            f"Parse failed: {exc}</div>"
        )

    return template_response("htmx/partials/requisitions/tabs/parsed_offer_results.html", ctx)


@router.post("/v2/partials/requisitions/{req_id}/save-parsed-offers", response_class=HTMLResponse)
async def save_parsed_offers(
    request: Request,
    req_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Save user-edited parsed offers to the requisition."""
    req = get_requisition_or_404(db, req_id)
    require_requisition_access(db, req_id, user)

    form = await request.form()
    vendor_name = form.get("vendor_name", "")

    # Collect offers from form fields (offers[0].mpn, offers[0].qty_available, etc.)
    offers_data: list[dict] = []
    idx = 0
    while True:
        mpn = form.get(f"offers[{idx}].mpn")
        if mpn is None:
            # Also check vendor_name field for freeform offers
            vn = form.get(f"offers[{idx}].vendor_name")
            if vn is None:
                break
        offer = {
            "vendor_name": form.get(f"offers[{idx}].vendor_name", vendor_name),
            "mpn": form.get(f"offers[{idx}].mpn", ""),
            "manufacturer": form.get(f"offers[{idx}].manufacturer"),
            "qty_available": _safe_int(form.get(f"offers[{idx}].qty_available")),
            "unit_price": _safe_float(form.get(f"offers[{idx}].unit_price")),
            "lead_time": form.get(f"offers[{idx}].lead_time"),
            "date_code": form.get(f"offers[{idx}].date_code"),
            "condition": form.get(f"offers[{idx}].condition", "new"),
            "moq": _safe_int(form.get(f"offers[{idx}].moq")),
            "notes": form.get(f"offers[{idx}].notes"),
        }
        offers_data.append(offer)
        idx += 1

    if not offers_data:
        return HTMLResponse(
            '<div class="p-4 text-center text-sm text-amber-600 bg-amber-50 rounded-lg border border-amber-200">'
            "No offers to save.</div>"
        )

    # Match MPNs to requirements
    reqs = db.query(Requirement).filter(Requirement.requisition_id == req_id).all()
    from app.vendor_utils import normalize_vendor_name

    saved_count = 0
    for o in offers_data:
        if not o["mpn"]:
            continue

        # Find matching requirement
        req_match_id = None
        mpn_lower = (o["mpn"] or "").strip().lower()
        for r in reqs:
            if r.primary_mpn and r.primary_mpn.strip().lower() == mpn_lower:
                req_match_id = r.id
                break

        # Resolve vendor card
        vn = o.get("vendor_name") or vendor_name or "Unknown"
        norm_name = normalize_vendor_name(vn)
        card = db.query(VendorCard).filter(VendorCard.normalized_name == norm_name).first()
        if not card:
            card = VendorCard(
                normalized_name=norm_name,
                display_name=vn,
                emails=[],
                phones=[],
            )
            db.add(card)
            db.flush()

        offer = Offer(
            requisition_id=req_id,
            requirement_id=req_match_id,
            vendor_card_id=card.id,
            vendor_name=card.display_name,
            vendor_name_normalized=card.normalized_name,
            mpn=o["mpn"],
            manufacturer=o.get("manufacturer"),
            qty_available=o.get("qty_available"),
            unit_price=o.get("unit_price"),
            lead_time=o.get("lead_time"),
            date_code=o.get("date_code"),
            condition=o.get("condition") or "new",
            moq=o.get("moq"),
            notes=o.get("notes"),
            source="ai_parsed",
            entered_by_id=user.id,
            status=OfferStatus.ACTIVE,
        )
        db.add(offer)
        # Offer hook: the user reviewed and saved this parse ACTIVE — user-initiated
        # proof of availability, release the vendor's matching active records.
        maybe_release_on_offer(db, req_match_id, offer.vendor_name, user)
        saved_count += 1

    db.commit()

    ctx = _base_ctx(request, user, "requisitions")
    ctx["req"] = req
    ctx["saved_count"] = saved_count
    return template_response("htmx/partials/requisitions/tabs/parse_save_success.html", ctx)


def _safe_int(val) -> int | None:
    """Safely convert form value to int."""
    if not val:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


def _safe_float(val) -> float | None:
    """Safely convert form value to float."""
    if not val:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


@router.post("/v2/partials/requisitions/bulk/{action}", response_class=HTMLResponse)
async def requisitions_bulk_action(
    request: Request,
    action: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Apply bulk action to selected requisitions and return refreshed list."""
    form = await request.form()
    ids_str = form.get("ids", "")
    if not ids_str:
        raise HTTPException(400, "No requisition IDs provided")

    try:
        ids = [int(x.strip()) for x in ids_str.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(400, "Invalid ID format")

    if len(ids) > 200:
        raise HTTPException(400, "Maximum 200 requisitions per bulk action")

    valid_actions = {"assign"}
    if action not in valid_actions:
        raise HTTPException(400, f"Invalid action: {action}")

    reqs = db.query(Requisition).filter(Requisition.id.in_(ids)).all()
    for r in reqs:
        require_requisition_access(db, r.id, user)

    if action == "assign":
        owner_id = form.get("owner_id")
        if owner_id:
            new_owner = _safe_int(owner_id)
            if new_owner is None:
                raise HTTPException(400, "owner_id must be an integer")
            for r in reqs:
                r.created_by = new_owner

    db.commit()
    logger.info("Bulk {} applied to {} requisitions by {}", action, len(reqs), user.email)

    return await requisitions_list_partial(
        request=request,
        q="",
        status="",
        owner=0,
        urgency="",
        date_from="",
        date_to="",
        sort="created_at",
        sort_dir="desc",
        limit=50,
        offset=0,
        user=user,
        db=db,
    )


@router.get("/v2/partials/requisitions/{req_id}/edit/{field}", response_class=HTMLResponse)
async def requisition_inline_edit_cell(
    request: Request,
    req_id: int,
    field: str,
    context: str = Query("row"),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return an inline edit form for a single cell (list row or detail header).

    Args:
        field: One of name, status, urgency, deadline, owner.
        context: 'row' for list view, 'header' for detail header.
    """

    valid_fields = {"name", "status", "urgency", "deadline", "owner"}
    if field not in valid_fields:
        return HTMLResponse("Invalid field", status_code=400)

    req = get_req_for_user(db, user, req_id, options=[])
    if not req:
        return HTMLResponse("Not found", status_code=404)
    users = db.query(User).order_by(User.name).all() if field == "owner" else []
    ctx = _base_ctx(request, user, "requisitions")
    ctx.update({"req": req, "field": field, "users": users, "context": context})
    return template_response("htmx/partials/requisitions/inline_cell.html", ctx)


@router.patch("/v2/partials/requisitions/{req_id}/inline", response_class=HTMLResponse)
async def requisition_inline_save(
    request: Request,
    req_id: int,
    field: str = Form(...),
    value: str = Form(default=""),
    context: str = Form(default="row"),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Save an inline edit and return the updated element.

    For context='row', returns the full table row. For context='header', returns the
    updated header card.
    """

    req = get_req_for_user(db, user, req_id, options=[])
    if not req:
        return HTMLResponse("Not found", status_code=404)

    msg = "Updated"

    if field == "name":
        clean = value.strip()
        if clean:
            req.name = clean
            msg = f"Renamed to '{clean}'"
    elif field == "status":
        from ..services.requisition_state import transition

        try:
            transition(req, value, user, db)
            msg = f"Status → {value}"
        except ValueError as e:
            msg = str(e)
    elif field == "urgency":
        if value in ("normal", "hot", "critical"):
            req.urgency = value
            msg = f"Urgency → {value}"
    elif field == "deadline":
        req.deadline = value if value else None
        msg = f"Deadline {'→ ' + value if value else 'cleared'}"
    elif field == "owner":
        if value and value.isdigit():
            req.created_by = int(value)
            msg = "Owner reassigned"

    req.updated_at = datetime.now(timezone.utc)
    req.updated_by_id = user.id
    db.commit()
    db.refresh(req)

    if context == "tab":
        # Tab context — return empty response with trigger to reload the tab
        response = HTMLResponse("")
        response.headers["HX-Trigger"] = json.dumps(
            {
                "showToast": {"message": msg},
                "reqDetailsRefresh": True,
            }
        )
        return response

    if context == "header":
        # Re-fetch with relationships for detail header
        req = (
            db.query(Requisition)
            .options(
                joinedload(Requisition.creator),
                joinedload(Requisition.requirements),
                joinedload(Requisition.offers),
            )
            .filter(Requisition.id == req_id)
            .first()
        )
        requirements = req.requirements or []
        req.offer_count = len(req.offers) if req.offers else 0
        users = db.query(User).order_by(User.name).all()
        ctx = _base_ctx(request, user, "requisitions")
        ctx.update({"req": req, "requirements": requirements, "users": users})
        response = template_response("htmx/partials/requisitions/detail_header.html", ctx)
    else:
        # Row context — re-fetch ORM object with relationships
        req = (
            db.query(Requisition)
            .options(
                joinedload(Requisition.creator),
                joinedload(Requisition.requirements),
                joinedload(Requisition.offers),
            )
            .filter(Requisition.id == req_id)
            .first()
        )
        req.req_count = len(req.requirements) if req.requirements else 0
        req.offer_count = len(req.offers) if req.offers else 0
        ctx = _base_ctx(request, user, "requisitions")
        ctx.update({"req": req, "user_role": getattr(user, "role", UserRole.SALES), "user": user})
        response = template_response("htmx/partials/requisitions/req_row.html", ctx)

    response.headers["HX-Trigger"] = json.dumps({"showToast": {"message": msg}})
    return response


@router.patch("/v2/partials/requisitions/{req_id}/win-probability", response_class=HTMLResponse)
async def requisition_win_probability_save(
    request: Request,
    req_id: int,
    win_probability: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Set win_probability (0-100) on a requisition, or clear it (empty string → NULL).

    Authz: same gate as other inline requisition edits (require_requisition_access).
    Returns an inline display span with the new value.
    """
    from app.dependencies import require_requisition_access

    req = db.get(Requisition, req_id)
    if not req:
        raise HTTPException(404, "Requisition not found")
    require_requisition_access(db, req_id, user)
    stripped = win_probability.strip()
    if stripped == "":
        prob = None
    else:
        try:
            prob = int(stripped)
        except (ValueError, TypeError):
            raise HTTPException(400, "win_probability must be an integer") from None
        if not (0 <= prob <= 100):
            raise HTTPException(400, "win_probability must be between 0 and 100")
    req.win_probability = prob
    req.updated_at = datetime.now(timezone.utc)
    req.updated_by_id = user.id
    db.commit()
    db.refresh(req)
    logger.info("Requisition {} win_probability set to {} by user {}", req_id, prob, user.id)
    ctx = _base_ctx(request, user, "requisitions")
    ctx["req"] = req
    return template_response("htmx/partials/requisitions/_win_probability.html", ctx)


@router.patch("/v2/partials/requisitions/{req_id}/opportunity-value", response_class=HTMLResponse)
async def requisition_opportunity_value_save(
    request: Request,
    req_id: int,
    opportunity_value: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Set opportunity_value (deal $) on a requisition, or clear it (empty string →
    NULL).

    Authz: same gate as other inline requisition edits (require_requisition_access).
    Returns an inline display span with the new value.
    """
    from decimal import Decimal, InvalidOperation

    from app.dependencies import require_requisition_access

    req = db.get(Requisition, req_id)
    if not req:
        raise HTTPException(404, "Requisition not found")
    require_requisition_access(db, req_id, user)
    stripped = opportunity_value.strip()
    if stripped == "":
        value = None
    else:
        try:
            value = Decimal(stripped)
        except (InvalidOperation, ValueError):
            raise HTTPException(400, "opportunity_value must be a number") from None
        if value < 0:
            raise HTTPException(400, "opportunity_value must be >= 0")
    req.opportunity_value = value
    req.updated_at = datetime.now(timezone.utc)
    req.updated_by_id = user.id
    db.commit()
    db.refresh(req)
    logger.info("Requisition {} opportunity_value set to {} by user {}", req_id, value, user.id)
    ctx = _base_ctx(request, user, "requisitions")
    ctx["req"] = req
    return template_response("htmx/partials/requisitions/_opportunity_value.html", ctx)


@router.post("/v2/partials/requisitions/{req_id}/action/{action_name}", response_class=HTMLResponse)
async def requisition_row_action(
    request: Request,
    req_id: int,
    action_name: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Execute a row-level action (claim, unclaim, won, lost, clone)."""

    valid_actions = {"claim", "unclaim", "won", "lost", "clone"}
    if action_name not in valid_actions:
        return HTMLResponse("Invalid action", status_code=400)

    req = get_req_for_user(db, user, req_id, options=[])
    if not req:
        return HTMLResponse("Not found", status_code=404)

    msg = "Action completed"
    form = await request.form()

    if action_name in ("won", "lost"):
        from ..services.requisition_state import OutcomeReasonRequired, transition

        try:
            transition(req, action_name, user, db, reason=form.get("reason", ""))
            msg = f"'{req.name}' → {action_name}"
        except OutcomeReasonRequired as e:
            return HTMLResponse(str(e), status_code=400)
        except ValueError as e:
            msg = str(e)
    elif action_name == "claim":
        from ..services.requirement_status import claim_requisition

        try:
            claim_requisition(req, user, db)
            msg = f"Claimed '{req.name}'"
        except ValueError as e:
            msg = str(e)
    elif action_name == "unclaim":
        from ..services.requirement_status import unclaim_requisition

        unclaim_requisition(req, db, actor=user)
        msg = f"Unclaimed '{req.name}'"
    elif action_name == "clone":
        from ..services.requisition_service import clone_requisition

        new_req = clone_requisition(db, req, user.id)
        msg = f"Cloned → REQ-{new_req.id:03d}"

    if action_name != "clone":
        db.commit()

    # Return refreshed list
    return_format = form.get("return", "list")
    if return_format == "list":
        response = await requisitions_list_partial(
            request=request,
            q="",
            status="",
            owner=0,
            urgency="",
            date_from="",
            date_to="",
            sort="created_at",
            sort_dir="desc",
            limit=50,
            offset=0,
            user=user,
            db=db,
        )
    else:
        response = HTMLResponse("")

    response.headers["HX-Trigger"] = json.dumps({"showToast": {"message": msg}})
    return response


@router.post("/v2/partials/requisitions/{req_id}/create-quote", response_class=HTMLResponse)
async def create_quote_from_offers(
    request: Request,
    req_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Create a new quote from selected offer IDs.

    Returns quote detail partial.
    """
    form = await request.form()
    offer_ids_raw = form.getlist("offer_ids")
    try:
        offer_ids = [int(x) for x in offer_ids_raw if x]
    except (ValueError, TypeError):
        raise HTTPException(400, "offer_ids must be integers")

    if not offer_ids:
        raise HTTPException(400, "No offers selected")

    req = get_requisition_or_404(db, req_id)
    require_requisition_access(db, req_id, user)

    offers = db.query(Offer).filter(Offer.id.in_(offer_ids), Offer.requisition_id == req_id).all()
    if not offers:
        raise HTTPException(404, "No matching offers found")

    # Build line items from offers

    quote_number = f"Q-{req_id}-{db.query(Quote).filter(Quote.requisition_id == req_id).count() + 1}"
    quote = Quote(
        requisition_id=req_id,
        quote_number=quote_number,
        status=QuoteStatus.DRAFT,
        created_by_id=user.id,
        customer_site_id=req.customer_site_id,
    )
    db.add(quote)
    db.flush()

    subtotal = 0.0
    total_cost = 0.0
    for o in offers:
        sell_price = float(o.unit_price or 0)
        cost_price = sell_price  # Default cost = sell, buyer adjusts
        qty = o.qty_available or 1
        margin_pct = 0.0

        line = QuoteLine(
            quote_id=quote.id,
            offer_id=o.id,
            mpn=o.mpn or "",
            manufacturer=o.manufacturer or "",
            qty=qty,
            cost_price=cost_price,
            sell_price=sell_price,
            margin_pct=margin_pct,
        )
        db.add(line)
        subtotal += sell_price * qty
        total_cost += cost_price * qty

    quote.subtotal = subtotal
    quote.total_cost = total_cost
    quote.total_margin_pct = ((subtotal - total_cost) / subtotal * 100) if subtotal else 0
    db.commit()
    db.refresh(quote)

    logger.info("Created quote {} from {} offers by {}", quote.quote_number, len(offers), user.email)

    # Return the quote detail page
    lines = db.query(QuoteLine).filter(QuoteLine.quote_id == quote.id).all()
    ctx = _base_ctx(request, user, "quotes")
    ctx["quote"] = quote
    ctx["lines"] = lines
    ctx["offers"] = offers
    return template_response("htmx/partials/quotes/detail.html", ctx)


@router.post("/v2/partials/requisitions/{req_id}/offers/{offer_id}/review", response_class=HTMLResponse)
async def review_offer(
    request: Request,
    req_id: int,
    offer_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Approve or reject an offer.

    Returns refreshed offers tab.
    """
    form = await request.form()
    action = form.get("action", "")

    if action not in ("approve", "reject"):
        raise HTTPException(400, "Invalid action")

    require_requisition_access(db, req_id, user)
    offer = db.query(Offer).filter(Offer.id == offer_id, Offer.requisition_id == req_id).first()
    if not offer:
        raise HTTPException(404, "Offer not found")

    old_status = offer.status

    if action == "approve":
        require_valid_transition("offer", offer.status, OfferStatus.APPROVED)
        offer.status = OfferStatus.APPROVED
        offer.approved_by_id = user.id

        offer.approved_at = datetime.now(timezone.utc)
        # Offer hook: user approval of a pending offer is user-initiated proof of
        # availability — release the vendor's matching active unavailability records.
        maybe_release_on_offer(db, offer.requirement_id, offer.vendor_name, user)
    else:
        require_valid_transition("offer", offer.status, OfferStatus.REJECTED)
        offer.status = OfferStatus.REJECTED

    _log_activity(
        db,
        activity_type=ActivityType.OFFER_STATUS_CHANGED,
        requisition_id=offer.requisition_id,
        user_id=user.id,
        vendor_card_id=offer.vendor_card_id,
        description=f"Offer {offer.vendor_name} status: {old_status} → {offer.status}",
        details={
            "offer_id": offer.id,
            "old_status": str(old_status),
            "new_status": str(offer.status),
        },
    )

    db.commit()
    logger.info("Offer {} {} by {}", offer_id, action, user.email)

    # Return refreshed offers tab
    return await requisition_tab(request=request, req_id=req_id, tab="offers", user=user, db=db)


@router.get("/v2/partials/requisitions/{req_id}/add-offer-form", response_class=HTMLResponse)
async def add_offer_form(
    request: Request,
    req_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return the manual offer entry form."""
    req = get_requisition_or_404(db, req_id)
    requirements = db.query(Requirement).filter(Requirement.requisition_id == req_id).all()
    ctx = _base_ctx(request, user, "requisitions")
    ctx["req"] = req
    ctx["requirements"] = requirements
    return template_response("htmx/partials/requisitions/add_offer_form.html", ctx)


@router.post("/v2/partials/requisitions/{req_id}/add-offer", response_class=HTMLResponse)
async def add_offer(
    request: Request,
    req_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Create a manual offer and return refreshed offers tab."""
    get_requisition_or_404(db, req_id)  # validates existence
    require_requisition_access(db, req_id, user)

    form = await request.form()
    vendor_name = (form.get("vendor_name") or "").strip()
    mpn = (form.get("mpn") or "").strip()
    if not vendor_name or not mpn:
        return HTMLResponse(
            '<div class="p-3 text-sm text-rose-600 bg-rose-50 rounded mb-4">Vendor name and MPN are required.</div>',
            status_code=400,
        )

    from datetime import date as date_type

    from ..services.offer_qualification import (
        apply_qualification,
        normalize_offer_condition,
    )
    from ..utils.normalization import normalize_mpn_key
    from ..vendor_utils import normalize_vendor_name

    offer = Offer(
        requisition_id=req_id,
        vendor_name=vendor_name,
        vendor_name_normalized=normalize_vendor_name(vendor_name),
        mpn=mpn,
        # Canonical dedup key (dash-stripped) so the part-centric offers query
        # matches consistently with create_offer's normalize_mpn_key.
        normalized_mpn=normalize_mpn_key(mpn),
        qty_available=_safe_int(form.get("qty_available")),
        unit_price=_safe_float(form.get("unit_price")),
        lead_time=form.get("lead_time") or None,
        date_code=form.get("date_code") or None,
        condition=normalize_offer_condition(form.get("condition")) or form.get("condition") or None,
        moq=_safe_int(form.get("moq")),
        manufacturer=form.get("manufacturer") or None,
        spq=_safe_int(form.get("spq")),
        packaging=form.get("packaging") or None,
        firmware=form.get("firmware") or None,
        hardware_code=form.get("hardware_code") or None,
        warranty=form.get("warranty") or None,
        country_of_origin=form.get("country_of_origin") or None,
        valid_until=_parse_date_safe(form.get("valid_until"), date_type),
        notes=form.get("notes") or None,
        requirement_id=_safe_int(form.get("requirement_id")),
        source="manual",
        status=OfferStatus.ACTIVE,
        entered_by_id=user.id,
        created_at=datetime.now(timezone.utc),
    )
    _qkeys = (
        "usage",
        "refurbished_by",
        "refurb_process",
        "cert_doc",
        "part_condition",
        "provenance_story",
        "terms",
        "lead_time_reason",
    )
    qual = {k: (form.get(k) or None) for k in _qkeys}
    qual["requests"] = []
    qual["schema"] = 1  # forward-version the qualification blob (spec §3.1)
    offer.qualification = qual if any(qual[k] for k in _qkeys) else None
    apply_qualification(offer)  # non-raising: composes note + sets status
    db.add(offer)
    db.flush()  # offer.id populated; activity row + offer committed together below
    # Offer hook: a manually entered offer is user-initiated proof of availability —
    # release the vendor's matching active unavailability records.
    maybe_release_on_offer(db, offer.requirement_id, offer.vendor_name, user)
    logger.info("Manual offer created: {} on req {} by {}", mpn, req_id, user.email)

    _log_activity(
        db,
        activity_type=ActivityType.OFFER_CREATED,
        requisition_id=offer.requisition_id,
        requirement_id=offer.requirement_id,
        user_id=user.id,
        vendor_card_id=offer.vendor_card_id,
        description=f"Offer added: {offer.vendor_name} — {offer.mpn}",
        details={"offer_id": offer.id, "source": offer.source},
    )
    db.commit()

    return await requisition_tab(request=request, req_id=req_id, tab="offers", user=user, db=db)


@router.post("/v2/partials/requisitions/{req_id}/offers/{offer_id}/reconfirm", response_class=HTMLResponse)
async def reconfirm_offer(
    request: Request,
    req_id: int,
    offer_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Reconfirm an offer — resets TTL and increments reconfirm count."""
    require_requisition_access(db, req_id, user)
    offer = db.query(Offer).filter(Offer.id == offer_id, Offer.requisition_id == req_id).first()
    if not offer:
        raise HTTPException(404, "Offer not found")

    now = datetime.now(timezone.utc)
    offer.reconfirmed_at = now
    offer.reconfirm_count = (offer.reconfirm_count or 0) + 1
    offer.expires_at = now + timedelta(days=14)
    offer.attribution_status = AttributionStatus.ACTIVE
    offer.is_stale = False
    offer.updated_at = now
    offer.updated_by_id = user.id
    db.commit()
    logger.info("Offer {} reconfirmed (count={}) by {}", offer_id, offer.reconfirm_count, user.email)

    return await requisition_tab(request=request, req_id=req_id, tab="offers", user=user, db=db)


# ── Sprint 2: Offer Management Completion ─────────────────────────────


@router.get("/v2/partials/requisitions/{req_id}/offers/{offer_id}/edit-form", response_class=HTMLResponse)
async def edit_offer_form(
    request: Request,
    req_id: int,
    offer_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return inline edit form for an existing offer."""
    offer = db.query(Offer).filter(Offer.id == offer_id, Offer.requisition_id == req_id).first()
    if not offer:
        raise HTTPException(404, "Offer not found")
    requirements = db.query(Requirement).filter(Requirement.requisition_id == req_id).all()
    return template_response(
        "htmx/partials/requisitions/edit_offer_form.html",
        {"request": request, "offer": offer, "req_id": req_id, "requirements": requirements},
    )


@router.post("/v2/partials/requisitions/{req_id}/offers/{offer_id}/edit", response_class=HTMLResponse)
async def edit_offer(
    request: Request,
    req_id: int,
    offer_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Save edits to an offer and return refreshed offers tab."""
    from ..models.intelligence import ChangeLog

    require_requisition_access(db, req_id, user)
    offer = db.query(Offer).filter(Offer.id == offer_id, Offer.requisition_id == req_id).first()
    if not offer:
        raise HTTPException(404, "Offer not found")

    form = await request.form()
    trackable = [
        "vendor_name",
        "qty_available",
        "unit_price",
        "lead_time",
        "condition",
        "date_code",
        "moq",
        "notes",
        "manufacturer",
        "spq",
        "packaging",
        "firmware",
        "hardware_code",
        "warranty",
        "country_of_origin",
        "valid_until",
    ]
    now = datetime.now(timezone.utc)

    for field in trackable:
        new_val = form.get(field, "").strip()
        old_val = str(getattr(offer, field) or "")
        if new_val != old_val and new_val:
            if field in ("qty_available", "moq", "spq"):
                try:
                    setattr(offer, field, int(new_val))
                except ValueError:
                    continue
            elif field == "unit_price":
                try:
                    setattr(offer, field, float(new_val))
                except ValueError:
                    continue
            elif field == "valid_until":
                from datetime import date as date_type

                try:
                    setattr(offer, field, date_type.fromisoformat(new_val) if new_val else None)
                except ValueError:
                    continue
            else:
                setattr(offer, field, new_val)
            db.add(
                ChangeLog(
                    entity_type="offer",
                    entity_id=offer_id,
                    user_id=user.id,
                    field_name=field,
                    old_value=old_val,
                    new_value=new_val,
                )
            )

    req_id_val = form.get("requirement_id", "")
    if req_id_val:
        offer.requirement_id = int(req_id_val) if req_id_val.isdigit() else None

    from ..services.offer_qualification import (
        apply_qualification,
        normalize_offer_condition,
    )

    _qkeys = (
        "usage",
        "refurbished_by",
        "refurb_process",
        "cert_doc",
        "part_condition",
        "provenance_story",
        "terms",
        "lead_time_reason",
    )
    submitted_qual = {k: (form.get(k) or None) for k in _qkeys}
    if any(submitted_qual.values()):
        merged = dict(offer.qualification or {})
        merged.update(submitted_qual)
        merged.setdefault("requests", [])
        merged["schema"] = 1  # forward-version the qualification blob (spec §3.1)
        offer.qualification = merged
    cond_raw = form.get("condition", "").strip()
    if cond_raw:
        offer.condition = normalize_offer_condition(cond_raw) or cond_raw

    apply_qualification(offer)  # non-raising: composes note + sets status
    offer.updated_at = now
    offer.updated_by_id = user.id
    db.commit()
    logger.info("Offer {} edited by {}", offer_id, user.email)

    return await requisition_tab(request=request, req_id=req_id, tab="offers", user=user, db=db)


@router.delete("/v2/partials/requisitions/{req_id}/offers/{offer_id}", response_class=HTMLResponse)
async def delete_offer_htmx(
    request: Request,
    req_id: int,
    offer_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Delete an offer and return refreshed offers tab."""
    require_requisition_access(db, req_id, user)
    offer = db.query(Offer).filter(Offer.id == offer_id, Offer.requisition_id == req_id).first()
    if not offer:
        raise HTTPException(404, "Offer not found")
    db.delete(offer)
    db.commit()
    logger.info("Offer {} deleted by {}", offer_id, user.email)

    return await requisition_tab(request=request, req_id=req_id, tab="offers", user=user, db=db)


@router.post("/v2/partials/requisitions/{req_id}/offers/{offer_id}/mark-sold", response_class=HTMLResponse)
async def mark_offer_sold_htmx(
    request: Request,
    req_id: int,
    offer_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Mark an offer as sold and return refreshed offers tab."""
    from ..models.intelligence import ChangeLog

    require_requisition_access(db, req_id, user)
    offer = db.query(Offer).filter(Offer.id == offer_id, Offer.requisition_id == req_id).first()
    if not offer:
        raise HTTPException(404, "Offer not found")
    if offer.status == OfferStatus.SOLD:
        return await requisition_tab(request=request, req_id=req_id, tab="offers", user=user, db=db)

    old_status = offer.status
    require_valid_transition("offer", offer.status, OfferStatus.SOLD)
    offer.status = OfferStatus.SOLD
    offer.updated_at = datetime.now(timezone.utc)
    offer.updated_by_id = user.id
    db.add(
        ChangeLog(
            entity_type="offer",
            entity_id=offer_id,
            user_id=user.id,
            field_name="status",
            old_value=old_status,
            new_value="sold",
        )
    )

    _log_activity(
        db,
        activity_type=ActivityType.OFFER_STATUS_CHANGED,
        requisition_id=offer.requisition_id,
        user_id=user.id,
        vendor_card_id=offer.vendor_card_id,
        description=f"Offer {offer.vendor_name} status: {old_status} → {offer.status}",
        details={
            "offer_id": offer.id,
            "old_status": str(old_status),
            "new_status": str(offer.status),
        },
    )

    db.commit()
    logger.info("Offer {} marked sold by {}", offer_id, user.email)

    return await requisition_tab(request=request, req_id=req_id, tab="offers", user=user, db=db)


@router.get("/v2/partials/offers/review-queue", response_class=HTMLResponse)
async def offer_review_queue(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Render the offer review queue page — medium-confidence AI-parsed offers."""
    offers = (
        db.query(Offer)
        .filter(Offer.status == OfferStatus.PENDING_REVIEW)
        .order_by(Offer.created_at.desc())
        .limit(100)
        .all()
    )
    return template_response(
        "htmx/partials/offers/review_queue.html",
        {"request": request, "offers": offers, "user": user},
    )


@router.post("/v2/partials/offers/{offer_id}/promote", response_class=HTMLResponse)
async def promote_offer_htmx(
    request: Request,
    offer_id: int,
    user: User = Depends(require_access(AccessKey.APPROVE_OFFERS)),
    db: Session = Depends(get_db),
):
    """Promote a pending_review offer to active and return refreshed queue."""
    offer = db.get(Offer, offer_id)
    if not offer:
        raise HTTPException(404, "Offer not found")
    require_requisition_access(db, offer.requisition_id, user, owner_id=offer.entered_by_id, label="Offer")
    if offer.status != OfferStatus.PENDING_REVIEW:
        raise HTTPException(400, "Only pending_review offers can be promoted")

    old_status = offer.status
    require_valid_transition("offer", offer.status, OfferStatus.ACTIVE)
    offer.status = OfferStatus.ACTIVE
    offer.approved_by_id = user.id
    offer.approved_at = datetime.now(timezone.utc)
    offer.updated_at = datetime.now(timezone.utc)
    offer.updated_by_id = user.id

    # Offer hook: user approval of a pending offer is user-initiated proof of
    # availability — release the vendor's matching active unavailability records.
    maybe_release_on_offer(db, offer.requirement_id, offer.vendor_name, user)

    _log_activity(
        db,
        activity_type=ActivityType.OFFER_STATUS_CHANGED,
        requisition_id=offer.requisition_id,
        user_id=user.id,
        vendor_card_id=offer.vendor_card_id,
        description=f"Offer {offer.vendor_name} status: {old_status} → {offer.status}",
        details={
            "offer_id": offer.id,
            "old_status": str(old_status),
            "new_status": str(offer.status),
        },
    )

    db.commit()
    logger.info("Offer {} promoted by {}", offer_id, user.email)

    return await offer_review_queue(request=request, user=user, db=db)


@router.post("/v2/partials/offers/{offer_id}/reject", response_class=HTMLResponse)
async def reject_offer_htmx(
    request: Request,
    offer_id: int,
    user: User = Depends(require_access(AccessKey.APPROVE_OFFERS)),
    db: Session = Depends(get_db),
):
    """Reject a pending_review offer and return refreshed queue."""
    offer = db.get(Offer, offer_id)
    if not offer:
        raise HTTPException(404, "Offer not found")
    require_requisition_access(db, offer.requisition_id, user, owner_id=offer.entered_by_id, label="Offer")
    if offer.status != OfferStatus.PENDING_REVIEW:
        raise HTTPException(400, "Only pending_review offers can be rejected")

    old_status = offer.status
    require_valid_transition("offer", offer.status, OfferStatus.REJECTED)
    offer.status = OfferStatus.REJECTED
    offer.updated_at = datetime.now(timezone.utc)
    offer.updated_by_id = user.id

    _log_activity(
        db,
        activity_type=ActivityType.OFFER_STATUS_CHANGED,
        requisition_id=offer.requisition_id,
        user_id=user.id,
        vendor_card_id=offer.vendor_card_id,
        description=f"Offer {offer.vendor_name} status: {old_status} → {offer.status}",
        details={
            "offer_id": offer.id,
            "old_status": str(old_status),
            "new_status": str(offer.status),
        },
    )

    db.commit()
    logger.info("Offer {} rejected by {}", offer_id, user.email)

    return await offer_review_queue(request=request, user=user, db=db)


@router.get("/v2/partials/offers/{offer_id}/changelog", response_class=HTMLResponse)
async def offer_changelog(
    request: Request,
    offer_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Render change history for an offer."""
    from ..models.intelligence import ChangeLog

    offer = db.get(Offer, offer_id)
    if not offer:
        raise HTTPException(404, "Offer not found")
    rows = (
        db.query(ChangeLog)
        .filter(ChangeLog.entity_type == "offer", ChangeLog.entity_id == offer_id)
        .options(joinedload(ChangeLog.user))
        .order_by(ChangeLog.created_at.desc())
        .limit(50)
        .all()
    )
    return template_response(
        "htmx/partials/offers/changelog.html",
        {"request": request, "offer": offer, "changes": rows},
    )


@router.post("/v2/partials/requisitions/{req_id}/log-activity", response_class=HTMLResponse)
async def log_activity(
    request: Request,
    req_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Log a manual activity (note/call/email) for a requisition."""
    from ..models.intelligence import ActivityLog

    get_requisition_or_404(db, req_id)  # validates existence
    require_requisition_access(db, req_id, user)

    form = await request.form()
    activity_type = form.get("activity_type", "note")
    channel_map = {"note": "note", "phone_call": "phone", "email_sent": "email"}

    log = ActivityLog(
        user_id=user.id,
        requisition_id=req_id,
        activity_type=activity_type,
        channel=channel_map.get(activity_type, "note"),
        contact_name=form.get("vendor_name", ""),
        contact_phone=form.get("contact_phone", ""),
        contact_email=form.get("contact_email", ""),
        notes=form.get("notes", ""),
    )
    db.add(log)
    db.commit()
    db.refresh(log)
    logger.info("Activity logged for req {} by {}: {}", req_id, user.email, activity_type)

    # Return refreshed activity tab
    return await requisition_tab(request=request, req_id=req_id, tab="activity", user=user, db=db)


@router.get("/v2/partials/requisitions/{req_id}/rfq-compose", response_class=HTMLResponse)
async def rfq_compose(
    request: Request,
    req_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Render the RFQ compose form for a requisition."""
    from ..models.offers import Contact as RfqContact

    req = get_requisition_or_404(db, req_id)

    parts = db.query(Requirement).filter(Requirement.requisition_id == req_id).all()

    # Get unique vendors from sightings for this requisition's parts
    part_ids = [p.id for p in parts]
    vendors = []
    if part_ids:
        # Get distinct vendor names from sightings, then match to VendorCard
        vendor_names = (
            db.query(Sighting.vendor_name_normalized)
            .filter(Sighting.requirement_id.in_(part_ids), Sighting.vendor_name_normalized.isnot(None))
            .distinct()
            .all()
        )
        norm_names = [n[0] for n in vendor_names if n[0]]
        vendor_rows = (
            (
                db.query(VendorCard)
                .options(selectinload(VendorCard.vendor_contacts))
                .filter(VendorCard.normalized_name.in_(norm_names))
                .limit(50)
                .all()
            )
            if norm_names
            else []
        )
        # Check which vendors already have RFQs sent
        sent_vendor_names = set()
        existing_contacts = db.query(RfqContact).filter(RfqContact.requisition_id == req_id).all()
        for c in existing_contacts:
            if c.vendor_name_normalized:
                sent_vendor_names.add(c.vendor_name_normalized)

        for v in vendor_rows:
            # Get contacts for this vendor
            v_contacts = v.vendor_contacts[:5]  # Already eagerly loaded
            vendors.append(
                {
                    "id": v.id,
                    "display_name": v.display_name,
                    "normalized_name": v.normalized_name,
                    "domain": v.domain,
                    "contacts": v_contacts,
                    "already_asked": v.normalized_name in sent_vendor_names,
                    "emails": [c.email for c in v_contacts if c.email],
                }
            )

    ctx = _base_ctx(request, user, "requisitions")
    ctx["req"] = req
    ctx["parts"] = parts
    ctx["vendors"] = vendors
    return template_response("htmx/partials/requisitions/rfq_compose.html", ctx)


@router.post("/v2/partials/requisitions/{req_id}/ai-cleanup-email", response_class=HTMLResponse)
async def ai_cleanup_email(
    request: Request,
    req_id: int,
    body: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Clean up user-written email — fix grammar, tone, and formatting."""
    get_requisition_or_404(db, req_id)  # validates existence
    require_requisition_access(db, req_id, user)

    user_text = body.strip()
    if not user_text:
        return HTMLResponse('<p class="text-xs text-amber-600 mt-1">Write your email first, then click Clean Up.</p>')

    try:
        from app.utils.claude_client import claude_text

        result = await claude_text(
            prompt=(
                f"Clean up this RFQ email: fix grammar, spelling, punctuation. "
                f"Improve clarity and professional tone. Keep it concise. "
                f"Do NOT add information the user didn't include. "
                f"Do NOT change the meaning or add new requests. "
                f"Return ONLY the cleaned-up email text, nothing else.\n\n"
                f"---\n{user_text}\n---"
            ),
            system="You are an email editor for a professional electronic components buyer.",
            model_tier="fast",
            max_tokens=1000,
        )
        cleaned = result.strip() if result else user_text
    except Exception as exc:
        logger.error("AI cleanup error for req {}: {}", req_id, exc)
        cleaned = user_text

    # Return a script that replaces the textarea content with the cleaned text
    escaped = cleaned.replace("\\", "\\\\").replace("`", "\\`").replace("$", "\\$").replace("</", "<\\/")
    return HTMLResponse(
        f'<script>document.getElementById("rfq-body-textarea").value = `{escaped}`;</script>'
        '<p class="text-xs text-green-600 mt-1">Email cleaned up. Review and edit as needed.</p>'
    )


@router.post("/v2/partials/requisitions/{req_id}/ai-rephrase-email", response_class=HTMLResponse)
async def ai_rephrase_email(
    request: Request,
    req_id: int,
    body: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Rephrase the RFQ email so each send reads uniquely, keeping all parts intact."""
    get_requisition_or_404(db, req_id)  # validates existence
    require_requisition_access(db, req_id, user)

    user_text = body.strip()
    if not user_text:
        return HTMLResponse(
            '<p class="text-xs text-amber-600 mt-1">Write your email first, then click AI Rephrase.</p>'
        )

    from app.services.email_drafting import draft_email

    result = await draft_email("rfq_rephrase", {"body": user_text})
    rephrased = (result or {}).get("body") or user_text

    # Return a script that replaces the textarea content with the rephrased text.
    escaped = rephrased.replace("\\", "\\\\").replace("`", "\\`").replace("$", "\\$").replace("</", "<\\/")
    return HTMLResponse(
        f'<script>document.getElementById("rfq-body-textarea").value = `{escaped}`;</script>'
        '<p class="text-xs text-green-600 mt-1">Rephrased. Review and edit as needed.</p>'
    )


@router.post("/v2/partials/requisitions/{req_id}/rfq-send", response_class=HTMLResponse)
async def rfq_send(
    request: Request,
    req_id: int,
    user: User = Depends(require_access(AccessKey.SEND_RFQ)),
    db: Session = Depends(get_db),
):
    """Send RFQs via Graph API, falling back to DB-only in test mode."""
    import os

    from ..models.offers import Contact as RfqContact

    req = get_requisition_or_404(db, req_id)
    require_requisition_access(db, req_id, user)

    form = await request.form()
    vendor_names = form.getlist("vendor_names")
    vendor_emails = form.getlist("vendor_emails")
    subject = form.get("subject", f"RFQ - {req.name}")
    body = form.get("body", "")
    parts_text = form.get("parts_summary", "")

    if not vendor_names:
        raise HTTPException(400, "No vendors selected")

    # Try to get a fresh Graph API token for real email send
    token = None
    is_testing = os.environ.get("TESTING") == "1"
    if not is_testing:
        try:
            from ..dependencies import require_fresh_token

            token = await require_fresh_token(request, db)
        except HTTPException:
            token = None
            logger.warning("No Graph API token available — creating contacts without sending")

    sent = []
    failed = []

    if token and not is_testing:
        # Real email send via Graph API
        vendor_groups = []
        for name, email in zip(vendor_names, vendor_emails):
            if not email:
                continue
            vendor_groups.append(
                {
                    "vendor_name": name,
                    "vendor_email": email,
                    "parts": parts_text,
                    "subject": subject,
                    "body": body
                    or f"Dear {name},\n\nWe are looking for the following parts: {parts_text}\n\nPlease provide your best pricing and availability.\n\nThank you.",
                }
            )

        if vendor_groups:
            try:
                from ..email_service import send_batch_rfq

                results = await send_batch_rfq(
                    token=token,
                    db=db,
                    user_id=user.id,
                    requisition_id=req_id,
                    vendor_groups=vendor_groups,
                )
                for r in results:
                    status = r.get("status", "sent")
                    entry = {"vendor": r.get("vendor_name", ""), "email": r.get("vendor_email", ""), "status": status}
                    if status == "failed":
                        failed.append(entry)
                    else:
                        sent.append(entry)
            except Exception as exc:
                logger.error("Batch RFQ send failed: {}", exc)
                # Fall back to DB-only mode
                for name, email in zip(vendor_names, vendor_emails):
                    if not email:
                        continue
                    contact = RfqContact(
                        requisition_id=req_id,
                        user_id=user.id,
                        contact_type="email",
                        vendor_name=name,
                        vendor_name_normalized=name.lower().strip(),
                        vendor_contact=email,
                        parts_included=parts_text,
                        subject=subject,
                        status=ContactStatus.PENDING,
                        status_updated_at=datetime.now(timezone.utc),
                    )
                    db.add(contact)
                    sent.append({"vendor": name, "email": email, "status": "draft"})
                db.commit()
    else:
        # Test mode or no token — create Contact records without sending
        for name, email in zip(vendor_names, vendor_emails):
            if not email:
                continue
            contact = RfqContact(
                requisition_id=req_id,
                user_id=user.id,
                contact_type="email",
                vendor_name=name,
                vendor_name_normalized=name.lower().strip(),
                vendor_contact=email,
                parts_included=parts_text,
                subject=subject,
                status=ContactStatus.SENT,
                status_updated_at=datetime.now(timezone.utc),
            )
            db.add(contact)
            sent.append({"vendor": name, "email": email, "status": "sent"})
        db.commit()

    logger.info("RFQ: {} sent, {} failed for req {} by {}", len(sent), len(failed), req_id, user.email)

    ctx = _base_ctx(request, user, "requisitions")
    ctx["req"] = req
    ctx["sent_results"] = sent
    ctx["failed_results"] = failed
    ctx["total_sent"] = len(sent)
    ctx["total_failed"] = len(failed)
    return template_response("htmx/partials/requisitions/rfq_results.html", ctx)


# ── Follow-ups & Response Review (Phase 6) ───────────────────────────


@router.get("/v2/partials/follow-ups", response_class=HTMLResponse)
async def follow_ups_list_partial(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Cross-requisition follow-up queue as HTML partial."""
    from ..config import settings as cfg
    from ..models.offers import Contact as RfqContact

    threshold_days = getattr(cfg, "follow_up_days", 2)
    threshold = datetime.now() - __import__("datetime").timedelta(days=threshold_days)

    stale_q = db.query(RfqContact).filter(
        RfqContact.contact_type == "email",
        RfqContact.status.in_(["sent", "opened"]),
        RfqContact.created_at < threshold,
    )
    if getattr(user, "role", None) in (UserRole.SALES, UserRole.TRADER):
        stale_q = stale_q.join(Requisition).filter(Requisition.created_by == user.id)

    stale = stale_q.order_by(RfqContact.created_at.asc()).limit(500).all()

    req_ids = {c.requisition_id for c in stale}
    req_names: dict[int, str] = {}
    if req_ids:
        for r in db.query(Requisition.id, Requisition.name).filter(Requisition.id.in_(req_ids)).all():
            req_names[r.id] = r.name

    from datetime import timezone as tz

    now = datetime.now(tz.utc)
    follow_ups = []
    for c in stale:
        ca = c.created_at if c.created_at else now
        days_waiting = (now - ca).days
        follow_ups.append(
            {
                "contact_id": c.id,
                "requisition_id": c.requisition_id,
                "requisition_name": req_names.get(c.requisition_id, "Unknown"),
                "vendor_name": c.vendor_name,
                "vendor_email": c.vendor_contact,
                "parts": c.parts_included or [],
                "status": c.status,
                "days_waiting": days_waiting,
            }
        )

    ctx = _base_ctx(request, user, "follow-ups")
    ctx.update({"follow_ups": follow_ups, "total": len(follow_ups)})
    return template_response("htmx/partials/follow_ups/list.html", ctx)


@router.post("/v2/partials/follow-ups/{contact_id}/send", response_class=HTMLResponse)
async def send_follow_up_htmx(
    request: Request,
    contact_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Send a follow-up email for a stale contact.

    Returns success card.
    """
    import os

    from ..models.offers import Contact as RfqContact

    contact = db.get(RfqContact, contact_id)
    if not contact:
        raise HTTPException(404, "Contact not found")
    require_requisition_access(db, contact.requisition_id, user, owner_id=contact.user_id, label="Contact")

    form = await request.form()
    body = (form.get("body") or "").strip()

    # DNC hard-block — never email a do-not-contact vendor (checked in all modes,
    # before the TESTING gate), mirroring send_reply_htmx / send_batch_rfq.
    if contact.vendor_contact:
        dnc = (
            db.query(SiteContact)
            .filter(
                sqlfunc.lower(SiteContact.email) == contact.vendor_contact.lower(),
                SiteContact.do_not_contact.is_(True),
            )
            .first()
        )
        if dnc:
            logger.warning(
                "Follow-up skipped — do-not-contact flag set for vendor '{}' ({})",
                contact.vendor_name,
                contact.vendor_contact,
            )
            return HTMLResponse(
                '<div class="rounded bg-rose-50 border border-rose-200 text-rose-700 text-xs px-2 py-1.5">'
                "This vendor is on the do-not-contact list — follow-up not sent.</div>"
            )

    is_testing = os.environ.get("TESTING") == "1"
    email_sent = False

    if not is_testing and contact.vendor_contact:
        # Try to send real follow-up via Graph API
        try:
            from ..dependencies import require_fresh_token

            token = await require_fresh_token(request, db)

            from ..utils.graph_client import GraphClient

            gc = GraphClient(token)
            follow_up_subject = f"Follow-up: {contact.subject or 'RFQ'}"
            follow_up_body = (
                body
                or f"Dear {contact.vendor_name},\n\nI'm following up on our previous inquiry. Please let us know if you have availability.\n\nThank you."
            )
            payload = {
                "message": {
                    "subject": follow_up_subject,
                    "body": {"contentType": "Text", "content": follow_up_body},
                    "toRecipients": [{"emailAddress": {"address": contact.vendor_contact}}],
                },
                "saveToSentItems": "true",
            }
            await gc.post_json("/me/sendMail", payload)
            email_sent = True
        except Exception as exc:
            logger.warning("Follow-up email send failed for contact {}: {}", contact_id, exc)

    from datetime import timezone as tz

    if email_sent or is_testing:
        contact.status = ContactStatus.SENT
        contact.status_updated_at = datetime.now(tz.utc)
        db.commit()

    mode = "via Graph API" if email_sent else ("test mode" if is_testing else "FAILED")
    logger.info(
        "Follow-up {} for contact {} (vendor: {}, {}) by {}",
        "sent" if email_sent or is_testing else "FAILED",
        contact_id,
        contact.vendor_name,
        mode,
        user.email,
    )

    ctx = _base_ctx(request, user, "follow-ups")
    ctx["contact_id"] = contact_id
    ctx["vendor_name"] = contact.vendor_name or "Vendor"
    return template_response("htmx/partials/follow_ups/sent_success.html", ctx)


@router.post("/v2/partials/follow-ups/{contact_id}/ai-draft", response_class=HTMLResponse)
async def ai_draft_follow_up(
    request: Request,
    contact_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Draft a contextual follow-up body and fill the compose textarea."""
    from datetime import timezone as tz

    from ..models.offers import Contact as RfqContact

    contact = db.get(RfqContact, contact_id)
    if not contact:
        raise HTTPException(404, "Contact not found")
    require_requisition_access(db, contact.requisition_id, user, owner_id=contact.user_id, label="Contact")

    days_waiting = (datetime.now(tz.utc) - contact.created_at).days if contact.created_at else None

    from app.services.email_drafting import draft_email

    result = await draft_email(
        "follow_up",
        {
            "vendor_name": contact.vendor_name,
            "parts": contact.parts_included or [],
            "days_waiting": days_waiting,
            "subject": contact.subject,
        },
    )
    drafted = (result or {}).get("body") or ""

    escaped = drafted.replace("\\", "\\\\").replace("`", "\\`").replace("$", "\\$").replace("</", "<\\/")
    return HTMLResponse(
        f'<script>document.getElementById("follow-up-body-{contact_id}").value = `{escaped}`;</script>'
        '<p class="text-xs text-green-600 mt-1">Draft ready. Review and edit before sending.</p>'
    )


@router.post("/v2/partials/requisitions/{req_id}/responses/{response_id}/review", response_class=HTMLResponse)
async def review_response_htmx(
    request: Request,
    req_id: int,
    response_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Mark a vendor response as reviewed or rejected.

    Returns updated card.
    """
    from ..models.offers import VendorResponse

    require_requisition_access(db, req_id, user)
    vr = (
        db.query(VendorResponse)
        .filter(
            VendorResponse.id == response_id,
            VendorResponse.requisition_id == req_id,
        )
        .first()
    )
    if not vr:
        raise HTTPException(404, "Response not found")

    form = await request.form()
    new_status = form.get("status", "")
    if new_status not in ("reviewed", "rejected"):
        raise HTTPException(400, "Status must be 'reviewed' or 'rejected'")

    vr.status = new_status
    db.commit()
    logger.info("Response {} marked as {} by {}", response_id, new_status, user.email)

    req = db.query(Requisition).filter(Requisition.id == req_id).first()
    ctx = _base_ctx(request, user, "requisitions")
    ctx["r"] = vr
    ctx["req"] = req
    return template_response("htmx/partials/requisitions/tabs/response_card.html", ctx)


@router.post(
    "/v2/partials/requisitions/{req_id}/responses/{response_id}/ai-draft-reply",
    response_class=HTMLResponse,
)
async def ai_draft_reply(
    request: Request,
    req_id: int,
    response_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Draft an AI reply to a vendor response and render an editable compose block."""
    from ..models.offers import VendorResponse

    require_requisition_access(db, req_id, user)

    vr = (
        db.query(VendorResponse)
        .filter(
            VendorResponse.id == response_id,
            VendorResponse.requisition_id == req_id,
        )
        .first()
    )
    if not vr:
        raise HTTPException(404, "Response not found")

    parsed = vr.parsed_data or {}

    from app.services.email_drafting import draft_email

    result = await draft_email(
        "vendor_reply",
        {
            "classification": vr.classification,
            "vendor_name": vr.vendor_name,
            "mpn": parsed.get("mpn"),
            "qty": parsed.get("qty") or parsed.get("qty_available"),
            "price": parsed.get("price") or parsed.get("unit_price"),
            "lead_time": parsed.get("lead_time"),
            "subject": vr.subject,
        },
    )

    default_subject = vr.subject or "RFQ"
    if not default_subject.lower().startswith("re:"):
        default_subject = f"Re: {default_subject}"

    ctx = _base_ctx(request, user, "requisitions")
    ctx["req_id"] = req_id
    ctx["r"] = vr
    ctx["reply_subject"] = (result or {}).get("subject") or default_subject
    ctx["reply_body"] = (result or {}).get("body") or ""
    ctx["ai_failed"] = result is None
    return template_response("htmx/partials/requisitions/tabs/reply_compose.html", ctx)


@router.post(
    "/v2/partials/requisitions/{req_id}/responses/{response_id}/send-reply",
    response_class=HTMLResponse,
)
async def send_reply_htmx(
    request: Request,
    req_id: int,
    response_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Send a reply to a vendor response (as the signed-in user) and mark it
    reviewed."""
    import os

    from ..models.offers import VendorResponse

    require_requisition_access(db, req_id, user)

    vr = (
        db.query(VendorResponse)
        .filter(
            VendorResponse.id == response_id,
            VendorResponse.requisition_id == req_id,
        )
        .first()
    )
    if not vr:
        raise HTTPException(404, "Response not found")

    form = await request.form()
    subject = (form.get("subject") or "").strip() or f"Re: {vr.subject or 'RFQ'}"
    body = (form.get("body") or "").strip()
    if not body:
        raise HTTPException(400, "Reply body is required")

    # DNC hard-block — never email a do-not-contact vendor (checked in all modes).
    if vr.vendor_email:
        from sqlalchemy import func as _sqlfunc

        dnc = (
            db.query(SiteContact)
            .filter(
                _sqlfunc.lower(SiteContact.email) == vr.vendor_email.lower(),
                SiteContact.do_not_contact.is_(True),
            )
            .first()
        )
        if dnc:
            return HTMLResponse(
                '<div class="rounded bg-rose-50 border border-rose-200 text-rose-700 text-xs px-2 py-1.5">'
                "This vendor is on the do-not-contact list — reply not sent.</div>"
            )

    is_testing = os.environ.get("TESTING") == "1"
    email_sent = False

    if not is_testing and vr.vendor_email:
        try:
            from ..dependencies import require_fresh_token

            token = await require_fresh_token(request, db)

            from ..utils.graph_client import GraphClient

            gc = GraphClient(token)
            payload = {
                "message": {
                    "subject": subject,
                    "body": {"contentType": "Text", "content": body},
                    "toRecipients": [{"emailAddress": {"address": vr.vendor_email}}],
                },
                "saveToSentItems": "true",
            }
            await gc.post_json("/me/sendMail", payload)
            email_sent = True
        except Exception as exc:
            logger.warning("Vendor reply send failed for response {}: {}", response_id, exc)

    if email_sent or is_testing:
        vr.status = VendorResponseStatus.REVIEWED
        db.commit()

    logger.info(
        "Vendor reply {} for response {} (vendor: {}) by {}",
        "sent" if email_sent or is_testing else "FAILED",
        response_id,
        vr.vendor_name,
        user.email,
    )

    req = db.query(Requisition).filter(Requisition.id == req_id).first()
    ctx = _base_ctx(request, user, "requisitions")
    ctx["r"] = vr
    ctx["req"] = req
    return template_response("htmx/partials/requisitions/tabs/response_card.html", ctx)


@router.post("/v2/partials/requisitions/{req_id}/poll-inbox", response_class=HTMLResponse)
async def poll_inbox_htmx(
    request: Request,
    req_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Trigger a FULL inbox scan for the current user (not requisition-scoped), then
    return the refreshed responses tab."""
    get_requisition_or_404(db, req_id)  # validates existence
    require_requisition_access(db, req_id, user)
    logger.info("Inbox poll requested for req {} by {}", req_id, user.email)
    await _run_inbox_scan_now(user, db)
    return await requisition_tab(request=request, req_id=req_id, tab="responses", user=user, db=db)


@router.delete("/v2/partials/requisitions/{req_id}/requirements/{item_id}", response_class=HTMLResponse)
async def delete_requirement(
    request: Request,
    req_id: int,
    item_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Delete a requirement from a requisition.

    Returns empty response for hx-swap='delete'.
    """
    get_requisition_or_404(db, req_id)  # validates existence
    require_requisition_access(db, req_id, user)
    item = db.query(Requirement).filter(Requirement.id == item_id, Requirement.requisition_id == req_id).first()
    if not item:
        raise HTTPException(404, "Requirement not found")
    db.delete(item)
    db.commit()
    return HTMLResponse("")


@router.put("/v2/partials/requisitions/{req_id}/requirements/{item_id}", response_class=HTMLResponse)
async def update_requirement(
    request: Request,
    req_id: int,
    item_id: int,
    primary_mpn: str = Form(...),
    manufacturer: str = Form(""),
    target_qty: int = Form(1),
    brand: str = Form(""),
    target_price: float | None = Form(None),
    substitutes: str = Form(""),
    customer_pn: str = Form(""),
    need_by_date: str = Form(""),
    condition: str = Form(""),
    date_codes: str = Form(""),
    firmware: str = Form(""),
    hardware_codes: str = Form(""),
    packaging: str = Form(""),
    notes: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Update a requirement inline.

    Returns the updated row HTML.
    """
    from datetime import date as date_type

    from ..utils.normalization import parse_substitute_mpns

    if not manufacturer.strip():
        raise HTTPException(422, "Manufacturer is required")

    req = get_requisition_or_404(db, req_id)
    require_requisition_access(db, req_id, user)
    item = db.query(Requirement).filter(Requirement.id == item_id, Requirement.requisition_id == req_id).first()
    if not item:
        raise HTTPException(404, "Requirement not found")

    from ..search_service import resolve_material_card
    from ..utils.normalization import normalize_mpn_key

    form_data = await request.form()
    sub_mpns = form_data.getlist("sub_mpn")
    sub_mfrs = form_data.getlist("sub_manufacturer")
    subs_raw = [{"mpn": m.strip(), "manufacturer": mfr.strip()} for m, mfr in zip(sub_mpns, sub_mfrs) if m.strip()]

    item.primary_mpn = primary_mpn.strip()
    item.normalized_mpn = normalize_mpn_key(primary_mpn)
    card = resolve_material_card(primary_mpn, db)
    item.material_card_id = card.id if card else None
    item.target_qty = target_qty
    item.brand = brand.strip() or None
    item.manufacturer = manufacturer.strip()
    item.target_price = target_price
    item.substitutes = parse_substitute_mpns(subs_raw, primary_mpn)
    for sub in item.substitutes:
        resolve_material_card(sub["mpn"], db, manufacturer=sub.get("manufacturer", ""))
    item.customer_pn = customer_pn.strip() or None
    item.condition = condition.strip() or None
    item.date_codes = date_codes.strip() or None
    item.firmware = firmware.strip() or None
    item.hardware_codes = hardware_codes.strip() or None
    item.packaging = packaging.strip() or None
    item.notes = notes.strip() or None
    # Parse need_by_date from ISO string
    if need_by_date.strip():
        try:
            item.need_by_date = date_type.fromisoformat(need_by_date.strip())
        except ValueError:
            item.need_by_date = None
    else:
        item.need_by_date = None
    db.commit()
    db.refresh(item)

    # Attach sighting_count for the template
    sighting_count = db.query(Sighting).filter(Sighting.requirement_id == item.id).count()
    item.sighting_count = sighting_count

    ctx = _base_ctx(request, user, "requisitions")
    ctx["r"] = item
    ctx["req"] = req
    return template_response("htmx/partials/requisitions/tabs/req_row.html", ctx)


# ── Search partials ─────────────────────────────────────────────────────


@router.get("/v2/partials/search", response_class=HTMLResponse)
async def search_form_partial(
    request: Request,
    mpn: str = "",
    user: User = Depends(require_access(AccessKey.SEARCH)),
    db: Session = Depends(get_db),
):
    """Search surface entry point.

    With ``mpn`` → render the Part Dossier shell ("The Bench") whose sections lazy-load
    from part_dossier.py. Without ``mpn`` → the recent-searches landing (search box that
    deep-links the dossier + a lazy-loaded recent list). The new routes live in
    routers/part_dossier.py; this stays the single /v2/partials/search entry point.
    """
    ctx = _base_ctx(request, user, "search")
    if mpn.strip():
        ctx["mpn"] = mpn.strip().upper()
        return template_response("htmx/partials/search/dossier_shell.html", ctx)
    return template_response("htmx/partials/search/form.html", ctx)


@router.get("/v2/partials/search/history", response_class=HTMLResponse)
async def search_history_panel(
    request: Request,
    mpn: str = "",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Render the 'What we know' history panel for the searched MPN.

    Called by: results_shell.html right column (hx-get).
    Depends on: part_history_service.get_part_history, normalize_mpn_key,
                fru_matrix_service.get_fru_view/get_reverse_context (compact FRU
                crosswalk context — both are capped/cheap reads).
    """
    from ..services.fru_matrix_service import get_fru_view, get_reverse_context
    from ..services.part_history_service import PartHistory, get_part_history
    from ..utils.normalization import normalize_mpn_key

    key = normalize_mpn_key(mpn)  # pure/cheap; outside try so it can be logged on failure
    try:
        history = get_part_history(db, key)
        error = False
    except Exception:
        logger.exception("search_history_panel failed mpn={} key={} user={}", mpn, key, user.id)
        history = PartHistory(found=False)
        error = True

    # FRU crosswalk context, only for a concrete searched MPN: forward (the MPN is a
    # FRU) and reverse (the MPN appears under FRUs). The card is ADDITIVE, so its
    # lookups get their own scoped try/except — a crosswalk failure degrades to "no
    # crosswalk card" and must never discard a successfully loaded history or flip
    # the panel into the history-error state. (A history failure already suppresses
    # the card via the template's `not error` guard, so the lookups are skipped.)
    fru_view = None
    fru_reverse = None
    if key and not error:
        try:
            fru_view = get_fru_view(db, mpn)
            fru_reverse = get_reverse_context(db, mpn)
        except Exception:
            logger.exception("search_history_panel FRU context failed mpn={} key={} user={}", mpn, key, user.id)
            fru_view = None
            fru_reverse = None

    ctx = _base_ctx(request, user, "search")
    ctx.update(
        {
            "history": history,
            "error": error,
            "fru_view": fru_view,
            "fru_reverse": fru_reverse,
            "fru_query": mpn,
        }
    )
    return template_response("htmx/partials/search/history_panel.html", ctx)


@router.post("/v2/partials/search/run", response_class=HTMLResponse)
async def search_run(
    request: Request,
    mpn: str = Form(default=""),
    requirement_id: int = Query(default=0),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Launch a streaming part search and return the results shell HTML.

    Generates a search_id, launches stream_search_mpn as a background task, and returns
    the results_shell.html template with SSE connection details.

    If requirement_id is provided, searches for that requirement's MPN. Otherwise uses
    the mpn form field.
    """
    from uuid import uuid4

    from ..utils.async_helpers import safe_background_task as _safe_bg

    search_mpn = mpn.strip()

    # If searching from a requirement row, get the MPN from query params
    if not search_mpn and requirement_id:
        req = db.query(Requirement).filter(Requirement.id == requirement_id).first()
        if req:
            search_mpn = req.primary_mpn or ""

    # Also check query params for mpn (when called from requirement detail)
    if not search_mpn:
        search_mpn = request.query_params.get("mpn", "").strip()

    if not search_mpn:
        return HTMLResponse('<div class="p-4 text-sm text-red-600">Please enter a part number.</div>')

    # Generate a unique search ID and launch streaming search in background
    search_id = str(uuid4())
    enabled_sources = _get_enabled_sources(db)

    from ..search_service import stream_search_mpn

    await _safe_bg(stream_search_mpn(search_id, search_mpn), task_name="stream_search_mpn")

    ctx = _base_ctx(request, user, "search")
    ctx.update(
        {
            "search_id": search_id,
            "mpn": search_mpn,
            "enabled_sources": enabled_sources,
        }
    )
    return template_response("htmx/partials/search/results_shell.html", ctx)


@router.get("/v2/partials/search/stream")
async def search_stream(
    request: Request,
    search_id: str = Query(...),
    user: User = Depends(require_user),
):
    """SSE stream endpoint for search results.

    Subscribes to the SSE broker channel for the given search_id and yields events until
    the 'done' event is received or the client disconnects.
    """
    from sse_starlette.sse import EventSourceResponse

    from ..services.sse_broker import broker

    async def event_generator():
        async for msg in broker.listen(f"search:{search_id}"):
            if await request.is_disconnected():
                break
            yield {"event": msg["event"], "data": msg["data"]}
            if msg["event"] == "done":
                break

    return EventSourceResponse(event_generator())


def _get_enabled_sources(db: Session) -> list[dict]:
    """Return list of enabled API sources for the source progress chips.

    Called by: search_run
    Depends on: ApiSource model
    """
    from ..models import ApiSource

    sources = db.query(ApiSource).filter(ApiSource.status != ApiSourceStatus.DISABLED).all()
    return [{"name": s.name, "status": s.status} for s in sources]


def _get_cached_search_results(search_id: str) -> list[dict] | None:
    """Read cached search results from Redis.

    Called by: search_filter
    Depends on: search_service._get_search_redis
    """
    try:
        from ..search_service import _get_search_redis

        rc = _get_search_redis()
        if rc:
            data = rc.get(f"search:{search_id}:results")
            if data:
                return json.loads(data)
    except Exception:
        logger.warning("Redis cache lookup failed for search", exc_info=True)
    return None


@router.get("/v2/partials/search/filter", response_class=HTMLResponse)
async def search_filter(
    request: Request,
    search_id: str = Query(...),
    confidence: str = Query("all"),
    source: str = Query("all"),
    sort: str = Query("best"),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Re-render search results with filters applied, reading from Redis cache.

    Called by: search filter bar (HTMX)
    Depends on: _get_cached_search_results, vendor_card.html template
    """
    results = _get_cached_search_results(search_id)
    if results is None:
        return HTMLResponse('<div class="text-sm text-gray-500 p-4">Search results expired. Please search again.</div>')

    # Apply filters
    if confidence != "all":
        color_map = {"high": "green", "medium": "amber", "low": "red"}
        results = [r for r in results if r.get("confidence_color") == color_map.get(confidence)]

    if source != "all":
        results = [r for r in results if source in (r.get("sources_found") or [])]

    # Apply sort
    if sort == "cheapest":
        results.sort(key=lambda r: r.get("unit_price") or float("inf"))
    elif sort == "stock":
        results.sort(key=lambda r: r.get("qty_available") or 0, reverse=True)
    else:
        results.sort(key=lambda r: (r.get("score", 0), r.get("confidence_pct", 0)), reverse=True)

    # Re-render cards using vendor_card.html for each result
    cards_html = ""
    for i, card in enumerate(results):
        cards_html += templates.get_template("htmx/partials/search/vendor_card.html").render(
            card=card, card_index=i, search_id=search_id
        )
    return HTMLResponse(cards_html)


@router.get("/v2/partials/search/lead-detail", response_class=HTMLResponse)
async def search_lead_detail(
    request: Request,
    idx: int = Query(0, ge=0),
    mpn: str = Query(""),
    search_id: str = Query(""),
    vendor_key: str = Query(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return the lead detail drawer content for a single search result.

    When search_id + vendor_key are provided, reads from Redis cache (new flow).
    Otherwise falls back to re-running the search via quick_search_mpn (legacy).

    Called by: lead detail drawer (HTMX)
    Depends on: _get_cached_search_results, vendor_utils.normalize_vendor_name
    """
    # ── New path: read from Redis cache by vendor_key ──
    if search_id and vendor_key:
        results = _get_cached_search_results(search_id)
        if results:
            from ..vendor_utils import normalize_vendor_name

            lead = next(
                (r for r in results if normalize_vendor_name(r.get("vendor_name", "")) == vendor_key),
                None,
            )
            if lead:
                ctx = _base_ctx(request, user, "search")
                ctx.update({"lead": lead, "mpn": lead.get("mpn_matched", "")})
                return template_response("htmx/partials/search/lead_detail.html", ctx)
        return HTMLResponse('<p class="p-4 text-sm text-gray-500">Lead not found in cache. Please search again.</p>')

    # ── Legacy path: re-run search by MPN + index ──
    if not mpn.strip():
        return HTMLResponse('<p class="p-4 text-sm text-gray-500">No part number specified.</p>')

    try:
        from ..search_service import quick_search_mpn

        raw_results = await quick_search_mpn(mpn.strip(), db)
        results = raw_results if isinstance(raw_results, list) else raw_results.get("sightings", [])
    except Exception as exc:
        logger.error("Lead detail search failed for {}: {}", mpn, exc)
        return HTMLResponse(f'<p class="p-4 text-sm text-red-600">Search error: {html_mod.escape(str(exc))}</p>')

    if idx >= len(results):
        return HTMLResponse('<p class="p-4 text-sm text-gray-500">Lead not found.</p>')

    r = results[idx]

    # Enrich the single result with scoring data
    unified = score_unified(
        source_type=r.get("source_type", ""),
        vendor_score=r.get("vendor_score"),
        is_authorized=r.get("is_authorized", False),
        unit_price=r.get("unit_price"),
        qty_available=r.get("qty_available"),
        age_hours=r.get("age_hours"),
        has_price=bool(r.get("unit_price")),
        has_qty=bool(r.get("qty_available")),
        has_lead_time=bool(r.get("lead_time")),
        has_condition=bool(r.get("condition")),
    )
    r["confidence_pct"] = unified["confidence_pct"]
    r["confidence_color"] = unified["confidence_color"]
    r["source_badge"] = unified["source_badge"]
    r["score_components"] = unified.get("components", {})
    r["lead_quality"] = classify_lead(
        score=unified["score"],
        is_authorized=r.get("is_authorized", False),
        has_price=bool(r.get("unit_price")),
        has_qty=bool(r.get("qty_available")),
        has_contact=bool(r.get("vendor_email") or r.get("vendor_phone")),
        evidence_tier=r.get("evidence_tier"),
    )
    r["reason"] = explain_lead(
        vendor_name=r.get("vendor_name"),
        is_authorized=r.get("is_authorized", False),
        vendor_score=r.get("vendor_score"),
        unit_price=r.get("unit_price"),
        qty_available=r.get("qty_available"),
        has_contact=bool(r.get("vendor_email") or r.get("vendor_phone")),
        evidence_tier=r.get("evidence_tier"),
        source_type=r.get("source_type"),
    )

    # Look up vendor safety data from SourcingLead records if available
    safety_band = "unknown"
    safety_score = None
    safety_summary = "Safety is assessed when leads are sourced through requisitions."
    safety_flags = []
    safety_available = False

    vendor_name = r.get("vendor_name", "")
    if vendor_name:
        lead_row = (
            db.query(SourcingLead)
            .filter(SourcingLead.vendor_name.ilike(vendor_name))
            .order_by(SourcingLead.created_at.desc())
            .first()
        )
        if lead_row and lead_row.vendor_safety_band:
            safety_band = lead_row.vendor_safety_band
            safety_score = lead_row.vendor_safety_score
            safety_summary = lead_row.vendor_safety_summary or safety_summary
            safety_flags = lead_row.vendor_safety_flags or []
            safety_available = True

    # Look up material card for this MPN
    from ..models.intelligence import MaterialCard

    material_card_id = None
    mpn_clean = mpn.strip().lower()
    if mpn_clean:
        mc = db.query(MaterialCard.id).filter(MaterialCard.normalized_mpn == mpn_clean).first()
        if mc:
            material_card_id = mc.id

    ctx = _base_ctx(request, user, "search")
    ctx.update(
        {
            "lead": r,
            "mpn": mpn.strip(),
            "idx": idx,
            "safety_band": safety_band,
            "safety_score": safety_score,
            "safety_summary": safety_summary,
            "safety_flags": safety_flags,
            "safety_available": safety_available,
            "material_card_id": material_card_id,
        }
    )
    return template_response("htmx/partials/search/lead_detail.html", ctx)


@router.get("/v2/partials/search/requisition-picker", response_class=HTMLResponse)
async def requisition_picker(
    request: Request,
    mpn: str = Query(""),
    items: str = Query("[]"),
    action: str = Query("add"),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Render requisition picker modal for adding shortlisted results.

    Called by: shortlist_bar.html "Add to Requisition" button
    Depends on: models.sourcing (Requisition)
    """
    recent_reqs = (
        db.query(Requisition)
        .filter(Requisition.is_scratch.is_(False))
        .order_by(Requisition.created_at.desc())
        .limit(20)
        .all()
    )

    ctx = _base_ctx(request, user, "search")
    ctx.update(
        {
            "requisitions": recent_reqs,
            "mpn": mpn,
            "items_json": items,
            "action": action,
        }
    )
    return template_response("htmx/partials/search/requisition_picker_modal.html", ctx)


@router.post("/v2/partials/search/add-to-requisition", response_class=HTMLResponse)
async def add_to_requisition(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Add shortlisted search results to a requisition as Sighting rows.

    Creates a Requirement for the MPN if one doesn't exist on the requisition.
    Persists each selected result as a Sighting row.

    Called by: requisition_picker_modal.html action button
    Depends on: models.sourcing (Requisition, Requirement, Sighting)
    """
    body = await request.json()
    requisition_id = body.get("requisition_id")
    mpn = body.get("mpn", "").strip()
    items = body.get("items", [])

    if not requisition_id or not mpn or not items:
        return HTMLResponse(
            '<div class="text-red-600 text-sm p-2">Missing required fields.</div>',
            status_code=400,
        )

    req = db.get(Requisition, requisition_id)
    if not req:
        return HTMLResponse(
            '<div class="text-red-600 text-sm p-2">Requisition not found.</div>',
            status_code=404,
        )
    require_requisition_access(db, int(requisition_id), user)

    # Find or create Requirement for this MPN
    requirement = (
        db.query(Requirement)
        .filter_by(
            requisition_id=requisition_id,
            primary_mpn=mpn,
        )
        .first()
    )

    if not requirement:
        requirement = Requirement(
            requisition_id=requisition_id,
            primary_mpn=mpn,
            normalized_mpn=mpn.strip().upper(),
            target_qty=None,
            sourcing_status=SourcingStatus.OPEN,
        )
        db.add(requirement)
        db.flush()

    # Create Sighting rows (shared mapping — see services.sighting_ingest)
    created_rows: list[Sighting] = []
    for item in items:
        sighting = sighting_from_row(requirement.id, item)
        db.add(sighting)
        created_rows.append(sighting)

    # Re-apply durable vendor+part unavailability knowledge — a manually added
    # sighting for a known-dead vendor+part renders flagged with its reason; the
    # user can Mark available to override.
    apply_to_fresh_sightings(db, requirement, created_rows)

    db.commit()

    count = len(items)
    return HTMLResponse(
        f'<div class="text-sm text-emerald-600 p-2">'
        f"Added {count} result{'s' if count != 1 else ''} to requisition &ldquo;{req.name}&rdquo;"
        f"</div>"
    )


# ── Sprint 5: Quote Workflow Completion ────────────────────────────────


@router.post("/v2/partials/quotes/{quote_id}/preview", response_class=HTMLResponse)
async def preview_quote(
    request: Request,
    quote_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Render quote email preview before sending."""
    quote = get_quote_for_user(db, user, quote_id, options=[joinedload(Quote.quote_lines)])

    return template_response(
        "htmx/partials/quotes/preview.html",
        {"request": request, "quote": quote},
    )


@router.delete("/v2/partials/quotes/{quote_id}", response_class=HTMLResponse)
async def delete_quote_htmx(
    request: Request,
    quote_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Delete a draft quote and redirect to the requisitions page."""
    quote = get_quote_for_user(db, user, quote_id)
    if quote.status != QuoteStatus.DRAFT:
        raise HTTPException(400, "Only draft quotes can be deleted")

    db.delete(quote)
    db.commit()
    logger.info("Quote {} deleted by {}", quote_id, user.email)

    return HTMLResponse(status_code=200, headers={"HX-Redirect": "/v2/requisitions"})


@router.post("/v2/partials/quotes/{quote_id}/reopen", response_class=HTMLResponse)
async def reopen_quote(
    request: Request,
    quote_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Reopen a sent/closed quote back to draft."""
    quote = get_quote_for_user(db, user, quote_id)
    if quote.status not in (QuoteStatus.SENT, QuoteStatus.WON, QuoteStatus.LOST):
        raise HTTPException(400, "Only sent/won/lost quotes can be reopened")

    require_valid_transition("quote", quote.status, QuoteStatus.DRAFT)
    quote.status = QuoteStatus.DRAFT
    quote.updated_at = datetime.now(timezone.utc)
    db.commit()
    logger.info("Quote {} reopened by {}", quote_id, user.email)

    return await quote_detail_partial(request=request, quote_id=quote_id, user=user, db=db)


@router.get("/v2/partials/quotes/recent-terms", response_class=HTMLResponse)
async def recent_terms(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return recent payment/shipping terms as datalist options."""
    from sqlalchemy import distinct

    payment_terms = (
        db.query(distinct(Quote.payment_terms))
        .filter(Quote.payment_terms.isnot(None), Quote.payment_terms != "")
        .order_by(Quote.payment_terms)
        .limit(20)
        .all()
    )
    shipping_terms = (
        db.query(distinct(Quote.shipping_terms))
        .filter(Quote.shipping_terms.isnot(None), Quote.shipping_terms != "")
        .order_by(Quote.shipping_terms)
        .limit(20)
        .all()
    )
    payment_opts = [f'<option value="{t[0]}">' for t in payment_terms if t[0]]
    shipping_opts = [f'<option value="{t[0]}">' for t in shipping_terms if t[0]]
    html = f'<datalist id="payment-terms">{"".join(payment_opts)}</datalist>'
    html += f'<datalist id="shipping-terms">{"".join(shipping_opts)}</datalist>'
    return HTMLResponse(html)


@router.get("/v2/partials/pricing-history/{mpn}", response_class=HTMLResponse)
async def pricing_history(
    request: Request,
    mpn: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return pricing history table for an MPN."""
    from ..utils.normalization import normalize_mpn_key

    norm = normalize_mpn_key(mpn)
    offers = (
        (
            db.query(Offer)
            .filter(Offer.normalized_mpn == norm, Offer.unit_price.isnot(None))
            .order_by(Offer.created_at.desc())
            .limit(50)
            .all()
        )
        if norm
        else []
    )

    return template_response(
        "htmx/partials/quotes/pricing_history.html",
        {"request": request, "offers": offers, "mpn": mpn},
    )


@router.post("/v2/partials/quotes/{quote_id}/edit", response_class=HTMLResponse)
async def edit_quote_metadata(
    request: Request,
    quote_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Update quote metadata (payment terms, shipping, notes) and return refreshed
    detail."""
    quote = get_quote_for_user(db, user, quote_id)

    form = await request.form()
    if form.get("payment_terms"):
        quote.payment_terms = form["payment_terms"].strip()
    if form.get("shipping_terms"):
        quote.shipping_terms = form["shipping_terms"].strip()
    if form.get("notes"):
        quote.notes = form["notes"].strip()
    if form.get("valid_until"):
        quote.valid_until = form["valid_until"].strip()

    quote.updated_at = datetime.now(timezone.utc)
    db.commit()
    logger.info("Quote {} metadata edited by {}", quote_id, user.email)

    return await quote_detail_partial(request=request, quote_id=quote_id, user=user, db=db)


# ── Sprint 6: RFQ Workflow Depth ────────────────────────────────────────


@router.get("/v2/partials/requisitions/{req_id}/rfq-prepare", response_class=HTMLResponse)
async def rfq_prepare_panel(
    request: Request,
    req_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return RFQ preparation panel — vendor data + exhaustion check."""
    from ..models.offers import Contact as RfqContact

    req = get_requisition_or_404(db, req_id)

    # Get requirements for this req
    requirements = db.query(Requirement).filter(Requirement.requisition_id == req_id).all()
    mpns = [r.primary_mpn for r in requirements if r.primary_mpn]

    # Get vendors already contacted
    existing_contacts = (
        db.query(RfqContact.vendor_name_normalized).filter(RfqContact.requisition_id == req_id).distinct().all()
    )
    contacted_norms = {c[0] for c in existing_contacts if c[0]}

    # Get suggested vendors from sightings (join on normalized vendor name)
    from ..models import Sighting

    suggested_vendors = (
        (
            db.query(
                VendorCard.id,
                VendorCard.display_name,
                VendorCard.normalized_name,
                sqlfunc.count(Sighting.id).label("sighting_count"),
            )
            .join(Sighting, Sighting.vendor_name_normalized == VendorCard.normalized_name)
            .filter(
                Sighting.mpn_matched.in_(mpns) if mpns else sqlfunc.literal(False),
                VendorCard.is_blacklisted.isnot(True),
            )
            .group_by(VendorCard.id)
            .order_by(sqlfunc.count(Sighting.id).desc())
            .limit(20)
            .all()
        )
        if mpns
        else []
    )

    vendors = []
    for v in suggested_vendors:
        vendors.append(
            {
                "id": v.id,
                "display_name": v.display_name,
                "normalized_name": v.normalized_name,
                "sighting_count": v.sighting_count,
                "already_contacted": v.normalized_name in contacted_norms,
            }
        )

    return template_response(
        "htmx/partials/requisitions/rfq_prepare.html",
        {"request": request, "req": req, "vendors": vendors, "mpns": mpns, "total_contacted": len(contacted_norms)},
    )


@router.post("/v2/partials/requisitions/{req_id}/log-phone", response_class=HTMLResponse)
async def log_phone_call(
    request: Request,
    req_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Log a phone call to a vendor and return updated activity tab."""
    get_requisition_or_404(db, req_id)  # validates existence
    require_requisition_access(db, req_id, user)

    form = await request.form()
    vendor_name = form.get("vendor_name", "").strip()
    vendor_phone = form.get("vendor_phone", "").strip()
    notes = form.get("notes", "").strip()

    if not vendor_name or not vendor_phone:
        raise HTTPException(400, "Vendor name and phone are required")

    from ..models.offers import Contact as RfqContact
    from ..services.activity_service import log_call_activity

    contact = RfqContact(
        requisition_id=req_id,
        user_id=user.id,
        contact_type="phone",
        vendor_name=vendor_name,
        vendor_contact=vendor_phone,
        details=notes or f"Phone call to {vendor_name}",
        status=ContactStatus.SENT,
    )
    db.add(contact)

    # Route through log_call_activity so the call is matched to a vendor/company,
    # recorded as the canonical CALL_LOGGED type, and bumps last_activity_at.
    # force_meaningful=True: a manually logged call is a deliberate human interaction →
    # always meaningful, regardless of the duration-gate (which targets auto-captured
    # Teams/8x8 calls that carry a real duration).
    log = log_call_activity(
        user_id=user.id,
        direction="outbound",
        phone=vendor_phone,
        duration_seconds=None,
        external_id=None,
        contact_name=vendor_name,
        db=db,
        requisition_id=req_id,
        force_meaningful=True,
    )
    if log is not None:
        log.notes = notes or f"Called {vendor_name} at {vendor_phone}"
    db.commit()
    logger.info("Phone call logged for req {} vendor {} by {}", req_id, vendor_name, user.email)

    return template_response(
        "htmx/partials/requisitions/phone_log_success.html",
        {"request": request, "vendor_name": vendor_name, "vendor_phone": vendor_phone},
    )


@router.post("/v2/partials/follow-ups/send-batch", response_class=HTMLResponse)
async def send_batch_follow_up(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Send follow-ups to all stale contacts at once."""
    from ..models.offers import Contact as RfqContact

    cfg = getattr(request.app, "state", None)
    threshold_days = getattr(cfg, "follow_up_days", 2) if cfg else 2
    threshold = datetime.now(timezone.utc) - timedelta(days=threshold_days)

    q = db.query(RfqContact).filter(
        RfqContact.contact_type == "email",
        RfqContact.status.in_(["sent", "opened"]),
        RfqContact.created_at < threshold,
    )
    # Restricted roles act only on contacts under their own requisitions; buyer/manager/admin
    # stay global. Keep this in lockstep with follow_up_badge so the badge counts what the
    # batch acts on.
    if user.role in RESTRICTED_ROLES:
        q = q.join(Requisition, RfqContact.requisition_id == Requisition.id).filter(Requisition.created_by == user.id)
    stale = q.limit(50).all()

    sent_count = 0
    for contact in stale:
        contact.status = ContactStatus.RESPONDED
        contact.status_updated_at = datetime.now(timezone.utc)
        sent_count += 1
    db.commit()
    logger.info("Batch follow-up: {} contacts marked by {}", sent_count, user.email)

    msg = f"{sent_count} contact{'s' if sent_count != 1 else ''} marked as responded."
    return HTMLResponse(
        f'<div class="text-sm text-green-700 bg-green-50 border border-green-200 rounded-lg px-3 py-2">{msg}</div>'
    )


@router.get("/v2/partials/follow-ups/badge", response_class=HTMLResponse)
async def follow_up_badge(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return follow-up count badge for nav sidebar."""
    from ..models.offers import Contact as RfqContact

    threshold = datetime.now(timezone.utc) - timedelta(days=2)
    q = db.query(sqlfunc.count(RfqContact.id)).filter(
        RfqContact.contact_type == "email",
        RfqContact.status.in_(["sent", "opened"]),
        RfqContact.created_at < threshold,
    )
    # Same per-owner scope as send_batch_follow_up so the badge matches the batch.
    if user.role in RESTRICTED_ROLES:
        q = q.join(Requisition, RfqContact.requisition_id == Requisition.id).filter(Requisition.created_by == user.id)
    count = q.scalar() or 0
    if count > 0:
        return HTMLResponse(
            f'<span class="ml-auto px-1.5 py-0.5 text-[10px] font-bold text-white bg-amber-500 rounded-full">{count}</span>'
        )
    return HTMLResponse("")


@router.patch("/v2/partials/requisitions/{req_id}/responses/{response_id}/status", response_class=HTMLResponse)
async def update_response_status(
    request: Request,
    req_id: int,
    response_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Update vendor response status (reviewed/rejected/flagged)."""
    from ..models.offers import VendorResponse

    require_requisition_access(db, req_id, user)
    vr = (
        db.query(VendorResponse)
        .filter(
            VendorResponse.id == response_id,
            VendorResponse.requisition_id == req_id,
        )
        .first()
    )
    if not vr:
        raise HTTPException(404, "Response not found")

    form = await request.form()
    new_status = form.get("status", "").strip()
    valid = {"reviewed", "rejected", "flagged", "new"}
    if new_status not in valid:
        raise HTTPException(400, f"Invalid status. Must be one of: {', '.join(valid)}")

    vr.status = new_status
    db.commit()
    logger.info("Response {} status → {} by {}", response_id, new_status, user.email)

    return template_response(
        "htmx/partials/requisitions/response_status_badge.html",
        {"request": request, "response": vr},
    )


# ── Sprint 7: Email Integration ────────────────────────────────────────


@router.get("/v2/partials/emails/thread/{conversation_id}", response_class=HTMLResponse)
async def email_thread_viewer(
    request: Request,
    conversation_id: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Render email thread viewer with all messages."""
    messages = []
    error = None
    try:
        from ..dependencies import require_fresh_token as _rft

        token = await _rft(request, db)
        from ..services.email_threads import fetch_thread_messages

        messages = await fetch_thread_messages(conversation_id, token)
    except HTTPException:
        error = "M365 connection needs refresh — please reconnect in Settings"
    except (ConnectionError, TimeoutError, OSError, RuntimeError) as exc:
        logger.error("Could not load thread: {}", exc)
        error = "Could not load thread. Please try again."

    return template_response(
        "htmx/partials/emails/thread_viewer.html",
        {"request": request, "messages": messages, "conversation_id": conversation_id, "error": error},
    )


@router.post("/v2/partials/emails/reply", response_class=HTMLResponse)
async def send_email_reply(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Send an email reply and return success confirmation."""
    form = await request.form()
    to = form.get("to", "").strip()
    subject = form.get("subject", "").strip()
    body = form.get("body", "").strip()
    conversation_id = form.get("conversation_id", "").strip()

    if not to or not body:
        raise HTTPException(400, "Recipient and message body are required")

    # DNC hard-block — never email a do-not-contact recipient (checked before any
    # send attempt), mirroring send_reply_htmx / send_batch_rfq.
    dnc = (
        db.query(SiteContact)
        .filter(
            sqlfunc.lower(SiteContact.email) == to.lower(),
            SiteContact.do_not_contact.is_(True),
        )
        .first()
    )
    if dnc:
        logger.warning("Email reply skipped — do-not-contact flag set for recipient ({})", to)
        return HTMLResponse(
            '<div class="rounded bg-rose-50 border border-rose-200 text-rose-700 text-xs px-2 py-1.5">'
            "This recipient is on the do-not-contact list — reply not sent.</div>"
        )

    error = None
    try:
        from ..dependencies import require_fresh_token as _rft

        token = await _rft(request, db)
        from ..email_service import _build_html_body
        from ..utils.graph_client import GraphClient

        gc = GraphClient(token)
        html_body = _build_html_body(body)
        mail_payload = {
            "message": {
                "subject": subject or "Re:",
                "body": {"contentType": "HTML", "content": html_body},
                "toRecipients": [{"emailAddress": {"address": to}}],
            },
            "saveToSentItems": "true",
        }
        result = await gc.post_json("/me/sendMail", mail_payload)
        if "error" in result:
            error = f"Send failed: {result.get('detail', 'Unknown error')}"
    except HTTPException:
        error = "M365 connection needs refresh"
    except (ConnectionError, TimeoutError, OSError, RuntimeError) as exc:
        logger.error("Email send failed: {}", exc)
        error = "Send failed. Please try again or contact support."

    return template_response(
        "htmx/partials/emails/reply_result.html",
        {"request": request, "to": to, "error": error, "conversation_id": conversation_id},
    )


@router.get("/v2/partials/emails/thread/{conversation_id}/summary", response_class=HTMLResponse)
async def email_thread_summary(
    request: Request,
    conversation_id: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return AI-generated summary of an email thread."""
    summary = None
    error = None
    try:
        from ..dependencies import require_fresh_token as _rft

        token = await _rft(request, db)
        from ..services.email_intelligence_service import summarize_thread

        summary = await summarize_thread(token, conversation_id, db, user.id)
        if not summary:
            error = "Could not generate summary"
    except HTTPException:
        error = "M365 connection needs refresh"
    except (ConnectionError, TimeoutError, OSError, RuntimeError) as exc:
        logger.error("Summary failed: {}", exc)
        error = "Summary failed. Please try again."

    return template_response(
        "htmx/partials/emails/thread_summary.html",
        {"request": request, "summary": summary, "error": error},
    )


@router.get("/v2/partials/email-intelligence", response_class=HTMLResponse)
async def email_intelligence_partial(
    request: Request,
    classification: str = "",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return email intelligence dashboard as HTML partial."""
    from ..services.email_intelligence_service import get_recent_intelligence
    from ..services.response_analytics import get_email_intelligence_dashboard

    items = get_recent_intelligence(db, user.id, limit=50, classification=classification or None)
    dashboard = get_email_intelligence_dashboard(db, user.id, days=7)

    return template_response(
        "htmx/partials/emails/intelligence_dashboard.html",
        {"request": request, "items": items, "dashboard": dashboard, "classification": classification},
    )


# ── Dashboard partial ───────────────────────────────────────────────────


@router.get("/v2/partials/dashboard", response_class=HTMLResponse)
async def dashboard_partial(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return dashboard stats partial."""
    open_reqs = (
        db.query(sqlfunc.count(Requisition.id))
        .filter(
            Requisition.status.in_(
                [
                    RequisitionStatus.OPEN,
                    RequisitionStatus.DRAFT,
                ]
            )
        )
        .scalar()
        or 0
    )
    vendor_count = db.query(sqlfunc.count(VendorCard.id)).scalar() or 0
    company_count = db.query(sqlfunc.count(Company.id)).filter(Company.is_active.is_(True)).scalar() or 0

    ctx = _base_ctx(request, user, "dashboard")
    ctx["stats"] = {"open_reqs": open_reqs, "vendor_count": vendor_count, "company_count": company_count}
    return template_response("htmx/partials/dashboard.html", ctx)


# ── AI Insights HTMX routes (Phase 6) ─────────────────────────────────


def _render_insights(request, user, insights, entity_type, entity_id):
    """Render the shared insights panel partial."""
    ctx = _base_ctx(request, user, entity_type)
    ctx["insights"] = insights
    ctx["entity_type"] = entity_type
    ctx["entity_id"] = entity_id
    return template_response("htmx/partials/shared/insights_panel.html", ctx)


@router.get("/v2/partials/requisitions/{req_id}/insights", response_class=HTMLResponse)
async def requisition_insights_panel(
    request: Request,
    req_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return cached AI insights panel for a requisition."""
    from ..services.knowledge_service import get_cached_insights

    insights = get_cached_insights(db, req_id)
    return _render_insights(request, user, insights, "requisitions", req_id)


@router.post("/v2/partials/requisitions/{req_id}/insights/refresh", response_class=HTMLResponse)
async def requisition_insights_refresh(
    request: Request,
    req_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Generate fresh AI insights for a requisition and return panel."""
    from ..services.knowledge_service import generate_insights, get_cached_insights

    try:
        generate_insights(db, req_id)
    except Exception as e:
        logger.warning(f"Insight generation failed for req {req_id}: {e}")
    insights = get_cached_insights(db, req_id)
    return _render_insights(request, user, insights, "requisitions", req_id)


@router.get("/v2/partials/vendors/{vendor_id}/insights", response_class=HTMLResponse)
async def vendor_insights_panel(
    request: Request,
    vendor_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return cached AI insights panel for a vendor."""
    from ..services.knowledge_service import get_cached_vendor_insights

    insights = get_cached_vendor_insights(db, vendor_id)
    return _render_insights(request, user, insights, "vendors", vendor_id)


@router.post("/v2/partials/vendors/{vendor_id}/insights/refresh", response_class=HTMLResponse)
async def vendor_insights_refresh(
    request: Request,
    vendor_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Generate fresh AI insights for a vendor and return panel."""
    from ..services.knowledge_service import generate_vendor_insights, get_cached_vendor_insights

    try:
        generate_vendor_insights(db, vendor_id)
    except Exception as e:
        logger.warning(f"Insight generation failed for vendor {vendor_id}: {e}")
    insights = get_cached_vendor_insights(db, vendor_id)
    return _render_insights(request, user, insights, "vendors", vendor_id)


@router.get("/v2/partials/customers/{company_id}/insights", response_class=HTMLResponse)
async def company_insights_panel(
    request: Request,
    company_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return cached AI insights panel for a company."""
    from ..services.knowledge_service import get_cached_company_insights

    insights = get_cached_company_insights(db, company_id)
    return _render_insights(request, user, insights, "customers", company_id)


@router.post("/v2/partials/customers/{company_id}/insights/refresh", response_class=HTMLResponse)
async def company_insights_refresh(
    request: Request,
    company_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Generate fresh AI insights for a company and return panel."""
    from ..services.knowledge_service import generate_company_insights, get_cached_company_insights

    try:
        generate_company_insights(db, company_id)
    except Exception as e:
        logger.warning(f"Insight generation failed for company {company_id}: {e}")
    insights = get_cached_company_insights(db, company_id)
    return _render_insights(request, user, insights, "customers", company_id)


@router.get("/v2/partials/dashboard/pipeline-insights", response_class=HTMLResponse)
async def pipeline_insights_panel(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return cached pipeline insights for the dashboard."""
    from ..services.knowledge_service import get_cached_pipeline_insights

    insights = get_cached_pipeline_insights(db)
    return _render_insights(request, user, insights, "dashboard", 0)


@router.post("/v2/partials/dashboard/pipeline-insights/refresh", response_class=HTMLResponse)
async def pipeline_insights_refresh(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Generate fresh pipeline insights and return panel."""
    from ..services.knowledge_service import generate_pipeline_insights, get_cached_pipeline_insights

    try:
        generate_pipeline_insights(db)
    except Exception as e:
        logger.warning(f"Pipeline insight generation failed: {e}")
    insights = get_cached_pipeline_insights(db)
    return _render_insights(request, user, insights, "dashboard", 0)


# ── Buy Plans partials ─────────────────────────────────────────────────


def _is_ops_member(user: User, db: Session) -> bool:
    """Check if user is in the ops verification group."""
    return db.query(VerificationGroupMember).filter_by(user_id=user.id, is_active=True).first() is not None


def _can_supervise(user: User, db: Session) -> bool:
    """True when the user may see cross-user (scope=all) deal data.

    Managers/admins and ops verification-group members qualify.
    """
    return user.role in (UserRole.MANAGER, UserRole.ADMIN) or _is_ops_member(user, db)


# Roles that cut/claim POs. Deliberately NOT the broader BUYER_ROLES (which includes
# SALES/TRADER) — only these may re-source a cancelled PO or claim an open-pool line.
_PO_CUTTER_ROLES = (UserRole.BUYER, UserRole.MANAGER, UserRole.ADMIN)


def _can_resource(user: User) -> bool:
    """True when the user may re-source / claim buy-plan lines (a PO-cutter)."""
    return user.role in _PO_CUTTER_ROLES


def _can_see_all_deals(user: User, db: Session) -> bool:
    """True when the user may view every owner's deals on the Deal Hub board.

    PO-cutters (buyers + managers/admins) and ops verification-group members see the
    full deal flow; sales/traders are scoped to their own deals only. Broader than
    ``_can_supervise`` by including buyers, who need cross-owner visibility to cut POs.
    """
    return _can_resource(user) or _is_ops_member(user, db)


def _resolve_deal_scope(scope: str, can_see_all: bool) -> str:
    """Normalize a requested deal scope against the user's visibility.

    Empty/unknown → the role default (``all`` for can-see-all users, else ``mine``).
    ``all`` requested by a user without cross-owner visibility is forced to ``mine`` so
    no other rep's plans leak.
    """
    if scope not in ("mine", "all"):
        return "all" if can_see_all else "mine"
    if scope == "all" and not can_see_all:
        return "mine"
    return scope


def _require_po_cutter(user: User) -> None:
    """403 unless the user is an active PO-cutter (buyer/manager/admin)."""
    if not _can_resource(user) or not getattr(user, "is_active", True):
        raise HTTPException(403, "Only buyers and managers can re-source / claim lines")


# Canonical stage-tab lens keys (underscored). URL paths use dashes (sales-orders, …).
_APPROVALS_TABS = ("sales_orders", "buy_plans", "purchase_orders", "prepayments", "supervise")

# Per-gate approve-right attribute that gates each stage tab's pinned "Pending approvals"
# section. The keys match services.approvals.queue.TAB_GATE; the values are User columns.
# buy_plans is intentionally absent — that tab is gate-less (board only, no pending section).
_TAB_APPROVE_ATTR = {
    "sales_orders": "can_approve_buy_plans",
    "purchase_orders": "can_approve_pos",
    "prepayments": "can_approve_prepayments",
}


def _default_lens(user: User, db: Session) -> str:
    """Pick the landing stage tab for the Approvals hub based on the user's role.

    - buyers land on the Purchase Orders stage (their PO cut queue),
    - managers/admins/ops land on Supervise,
    - everyone else (sales/trader) lands on the Buy Plans deal board.
    """
    if user.role == UserRole.BUYER:
        return "purchase_orders"
    if _can_supervise(user, db):
        return "supervise"
    return "buy_plans"


@router.get("/v2/partials/approvals", response_class=HTMLResponse)
@router.get("/v2/partials/buy-plans", response_class=HTMLResponse)
async def buy_plans_list_partial(
    request: Request,
    lens: str = "",
    user: User = Depends(require_access(AccessKey.BUY_PLANS)),
    db: Session = Depends(get_db),
):
    """Return the Approvals hub shell (stage-tab switcher).

    The shell renders the five lifecycle stage tabs + a lazy body that loads the active
    stage tab partial into ``#bp-hub-body``. Row data is fetched by the body, not here.
    ``/v2/partials/buy-plans`` is kept as a back-compat alias for in-flight htmx.
    """
    active_lens = lens if lens in _APPROVALS_TABS else _default_lens(user, db)

    # Spotlight markers: plan rows that carry an open step needing this user's action.
    # Buy Plans is its own primary nav tab, so the source is registered under "buy-plans".
    from ..services.alerts import markers_for_tab

    alert_markers = markers_for_tab(db, user, "buy-plans")

    ctx = _base_ctx(request, user, "buy-plans")
    ctx.update(
        {
            "lens": active_lens,
            "alert_markers": alert_markers,
            # Only Supervise is gate-rendered in the shell; the four stage tabs are always
            # shown (their work surface + pinned approval section gate by role inside).
            "can_supervise": _can_supervise(user, db),
        }
    )
    return template_response("htmx/partials/buy_plans/hub.html", ctx)


@router.get("/v2/partials/approvals/{tab}", response_class=HTMLResponse)
async def approvals_tab_partial(
    request: Request,
    tab: str,
    scope: str = "",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Render one Approvals stage-tab body into ``#bp-hub-body``.

    Composes the re-homed work surface for the stage (deal board / buyer orders +
    re-sourcing pool / neutral empty state) with a pinned per-gate "Pending approvals"
    section (services.approvals.queue.build_queue_view), shown only when the viewer holds
    that gate's approve right. ``supervise`` reuses the manager triage body. ``tab`` arrives
    dash-cased (e.g. purchase-orders) and maps to the underscored stage key.

    ``scope`` applies to the Buy Plans stage's deal board only: it is role-resolved exactly
    like the standalone board (sales/traders locked to ``mine``), and its All/Mine toggle
    reloads THIS whole tab body so the pinned approval section survives the swap.
    """
    lens = tab.replace("-", "_")
    if lens not in _APPROVALS_TABS:
        raise HTTPException(404, "Unknown approvals tab")

    if lens == "supervise":
        return _render_supervise_body(request, user, db)

    ctx = _base_ctx(request, user, "buy-plans")
    if lens in _TAB_APPROVE_ATTR:
        from ..services.approvals.queue import build_queue_view

        ctx["view"] = build_queue_view(db, user, lens)
        ctx["show_pending"] = bool(getattr(user, _TAB_APPROVE_ATTR[lens], False))

    if lens == "buy_plans":
        from ..services.buyplan_hub import completed_archive, deals_board

        # Role-resolve the deal-board scope exactly like the standalone /board route, but
        # point the All/Mine toggle at THIS tab URL so a toggle reloads the whole tab body
        # (pinned approval section + board) rather than swapping in the bare board.
        can_all = _can_see_all_deals(user, db)
        board_scope = _resolve_deal_scope(scope, can_all)
        ctx.update(
            {
                "board": deals_board(
                    db,
                    user,
                    scope=board_scope,
                    statuses=[BuyPlanStatus.ACTIVE.value, BuyPlanStatus.HALTED.value],
                ),
                "scope": board_scope,
                "archive": completed_archive(db, user, scope=board_scope),
                "can_see_all_deals": can_all,
                "scope_toggle_url": "/v2/partials/approvals/buy-plans",
            }
        )
        return template_response("htmx/partials/approvals/_tab_buy_plans.html", ctx)

    if lens == "purchase_orders":
        from ..services.buyplan_hub import buyer_line_queue, resourcing_pool_queue, team_line_queue

        ctx.update(
            {
                "orders_queue": buyer_line_queue(db, user),
                "team": team_line_queue(db, user),
                "resource_queue": resourcing_pool_queue(db),
                "can_claim": _can_resource(user),
            }
        )
        return template_response("htmx/partials/approvals/_tab_purchase_orders.html", ctx)

    if lens == "sales_orders":
        from ..services.buyplan_hub import deals_board

        can_all = _can_see_all_deals(user, db)
        board_scope = _resolve_deal_scope(scope, can_all)
        # DRAFT/PENDING work surface only. No `archive`: the Completed archive belongs to
        # the Buy Plans tab, and the shared board renders it only when `archive` is passed.
        ctx.update(
            {
                "board": deals_board(
                    db,
                    user,
                    scope=board_scope,
                    statuses=[BuyPlanStatus.DRAFT.value, BuyPlanStatus.PENDING.value],
                ),
                "scope": board_scope,
                "can_see_all_deals": can_all,
                "scope_toggle_url": "/v2/partials/approvals/sales-orders",
            }
        )
        return template_response("htmx/partials/approvals/_tab_sales_orders.html", ctx)

    # prepayments — approval-only stage (no work surface in SP-1)
    return template_response("htmx/partials/approvals/_tab_prepayments.html", ctx)


@router.get("/v2/partials/approvals/sales-orders/new", response_class=HTMLResponse)
async def sales_order_new(
    request: Request,
    requisition_id: int | None = None,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """New Sales Order origination surface (requisition picker → offer/sell builder).

    The two-segment path after ``approvals/`` does not collide with the one-segment
    ``/v2/partials/approvals/{tab}`` converter. With no ``requisition_id`` it lists open
    (OPEN_PIPELINE) requisitions that carry at least one ACTIVE offer, scoped to what the
    user may see. With ``requisition_id`` it loads that requisition's per-requirement
    offer/sell-price form (``get_builder_data`` + ``apply_smart_defaults``), enforcing access
    via ``get_req_for_user`` (404 for a restricted role that does not own it).
    """
    from sqlalchemy import func

    from ..constants import OfferStatus, RequisitionStatus
    from ..dependencies import get_req_for_user
    from ..models import Offer, Requirement
    from ..services.quote_builder_service import apply_smart_defaults, get_builder_data

    ctx = _base_ctx(request, user, "buy-plans")

    if requisition_id is not None:
        req = get_req_for_user(db, user, requisition_id)
        lines = get_builder_data(req.id, db)
        apply_smart_defaults(lines)
        ctx.update({"selected_req": req, "lines": lines})
        return template_response("htmx/partials/approvals/_sales_order_new.html", ctx)

    # Picker mode: open requisitions with at least one active offer, scoped to the viewer.
    has_active_offer = (
        select(Offer.id)
        .join(Requirement, Offer.requirement_id == Requirement.id)
        .where(
            Requirement.requisition_id == Requisition.id,
            Offer.status == OfferStatus.ACTIVE,
        )
        .exists()
    )
    q = db.query(Requisition).filter(
        Requisition.status.in_(list(RequisitionStatus.OPEN_PIPELINE)),
        has_active_offer,
    )
    if user.role in RESTRICTED_ROLES:
        q = q.filter(Requisition.created_by == user.id)
    reqs = q.order_by(Requisition.id.desc()).all()

    counts: dict[int, int] = {}
    if reqs:
        counts = dict(
            db.query(Requirement.requisition_id, func.count(Offer.id))
            .join(Offer, Offer.requirement_id == Requirement.id)
            .filter(
                Requirement.requisition_id.in_([r.id for r in reqs]),
                Offer.status == OfferStatus.ACTIVE,
            )
            .group_by(Requirement.requisition_id)
            .all()
        )

    picker_rows = [
        {"id": r.id, "name": r.name, "customer": r.customer_name or "", "offer_count": counts.get(r.id, 0)}
        for r in reqs
    ]
    ctx.update({"selected_req": None, "picker_rows": picker_rows})
    return template_response("htmx/partials/approvals/_sales_order_new.html", ctx)


@router.post("/v2/partials/approvals/sales-orders/create", response_class=HTMLResponse)
async def sales_order_create(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Originate a DRAFT buy plan (Sales Order) from the chosen offers, then render its
    detail.

    Parses ``requisition_id`` + per-requirement ``offer_<rid>`` / ``sell_<rid>`` form fields,
    enforces requisition access (``require_requisition_access`` — 404 for a restricted role
    that does not own it), and calls ``create_sales_order_from_offers``. On the builder's
    duplicate-open-SO ValueError it renders the existing open Sales Order's detail with a
    toast (never a 500); any other ValueError (e.g. no requirements) is a 400.
    """
    from ..dependencies import require_requisition_access
    from ..services.buyplan_builder import (
        DuplicateSalesOrderError,
        create_sales_order_from_offers,
    )

    form = await request.form()
    raw_req_id = form.get("requisition_id")
    if not raw_req_id:
        raise HTTPException(400, "Requisition is required")
    try:
        req_id = int(raw_req_id)
    except (TypeError, ValueError):
        raise HTTPException(400, "Invalid requisition")

    require_requisition_access(db, req_id, user)

    selections: dict[int, int] = {}
    sell_prices: dict[int, float] = {}
    for key, value in form.multi_items():
        if key.startswith("offer_"):
            try:
                selections[int(key[len("offer_") :])] = int(value)
            except (TypeError, ValueError):
                continue
        elif key.startswith("sell_"):
            if value in (None, ""):
                continue
            try:
                sell_prices[int(key[len("sell_") :])] = float(value)
            except (TypeError, ValueError):
                continue

    try:
        plan = create_sales_order_from_offers(req_id, selections, sell_prices, db, user)
    except DuplicateSalesOrderError as exc:
        # An open Sales Order already exists for this requisition — open it instead of
        # 500ing. The exception carries the existing plan id, so no re-query is needed.
        existing_id = exc.existing_plan_id
        resp = await buy_plan_detail_partial(request, existing_id, user, db)
        resp.headers["HX-Trigger"] = json.dumps(
            {
                "showToast": {
                    "message": f"There is already an open buy plan for this requisition (plan #{existing_id}).",
                    "type": "warning",
                }
            }
        )
        resp.headers["HX-Push-Url"] = f"/v2/buy-plans/{existing_id}"
        return resp
    except ValueError:
        # Any other origination failure (e.g. requisition has no requirements). Return a
        # curated client message rather than echoing the raw builder error.
        raise HTTPException(400, "Could not build a buy plan from the selected offers.")

    resp = await buy_plan_detail_partial(request, plan.id, user, db)
    resp.headers["HX-Push-Url"] = f"/v2/buy-plans/{plan.id}"
    return resp


@router.get("/v2/partials/buy-plans/resource", response_class=HTMLResponse)
async def buy_plans_resource_partial(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Open-claim queue body for the "Needs Re-sourcing" lens (pool-wide).

    Lists every line whose cut PO was cancelled (vendor fell down) and is unassigned,
    awaiting any PO-cutter to claim + backfill.
    """
    from ..services.buyplan_hub import resourcing_pool_queue

    ctx = _base_ctx(request, user, "buy-plans")
    ctx["queue"] = resourcing_pool_queue(db)
    ctx["can_claim"] = _can_resource(user)
    return template_response("htmx/partials/buy_plans/_resource_queue.html", ctx)


@router.get("/v2/partials/buy-plans/orders", response_class=HTMLResponse)
async def buy_plans_orders_partial(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Buyer Orders body (re-homed under the Purchase Orders stage tab): the actionable
    per-line PO cut queue.

    Also includes a read-only "Team Orders" awareness section listing open lines
    assigned to OTHER buyers (see ``team_line_queue``).
    """
    from ..services.buyplan_hub import buyer_line_queue, team_line_queue

    ctx = _base_ctx(request, user, "buy-plans")
    ctx["queue"] = buyer_line_queue(db, user)
    ctx["team"] = team_line_queue(db, user)
    return template_response("htmx/partials/buy_plans/_orders_queue.html", ctx)


@router.get("/v2/partials/buy-plans/board", response_class=HTMLResponse)
async def buy_plans_board_partial(
    request: Request,
    scope: str = "",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Deal board body (re-homed under the Buy Plans / Supervise stage tabs): stage-
    grouped deal cards.

    Scope is role-defaulted: PO-cutters + ops (``_can_see_all_deals``) default to
    ``all`` and may toggle to ``mine``; sales/traders are locked to ``mine`` so no
    other rep's plans leak.
    """
    from ..services.buyplan_hub import completed_archive, deals_board

    can_all = _can_see_all_deals(user, db)
    scope = _resolve_deal_scope(scope, can_all)

    ctx = _base_ctx(request, user, "buy-plans")
    ctx.update(
        {
            "board": deals_board(db, user, scope=scope),
            "scope": scope,
            "archive": completed_archive(db, user, scope=scope),
            "can_see_all_deals": can_all,
        }
    )
    return template_response("htmx/partials/buy_plans/_board.html", ctx)


@router.get("/v2/partials/buy-plans/archive", response_class=HTMLResponse)
async def buy_plans_archive_partial(
    request: Request,
    scope: str = "",
    offset: int = 0,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Completed-transactions archive page (lazy "load older" chunk).

    Returns just the rows partial (not the whole section) so an htmx "Load older" click
    can append the next page in place. Scope is role-resolved exactly like the board so
    no other rep's completed plans leak to a sales/trader user.
    """
    from ..services.buyplan_hub import completed_archive

    scope = _resolve_deal_scope(scope, _can_see_all_deals(user, db))

    ctx = _base_ctx(request, user, "buy-plans")
    ctx.update(
        {
            "archive": completed_archive(db, user, scope=scope, offset=offset),
            "scope": scope,
        }
    )
    return template_response("htmx/partials/buy_plans/_archive_rows.html", ctx)


def _render_supervise_body(request: Request, user: User, db: Session) -> HTMLResponse:
    """Build + render the supervise lens body for ``user``.

    Shared by the ``GET /supervise`` route and the supervise-origin action returns.
    Non-supervisors never see cross-user data: they get the mine-scope board instead
    (defense in depth — the hub also hides the Supervise button for them).
    """
    from ..services.buyplan_hub import completed_archive, deals_board, supervise_overview

    if not _can_supervise(user, db):
        ctx = _base_ctx(request, user, "buy-plans")
        ctx.update(
            {
                "board": deals_board(db, user, scope="mine"),
                "scope": "mine",
                "archive": completed_archive(db, user, scope="mine"),
            }
        )
        return template_response("htmx/partials/buy_plans/_board.html", ctx)

    ctx = _base_ctx(request, user, "buy-plans")
    ctx.update(
        {
            "overview": supervise_overview(db),
            "board": deals_board(db, user, scope="all"),
            "archive": completed_archive(db, user, scope="all"),
            "is_ops": _is_ops_member(user, db),
            "is_manager": user.role in (UserRole.MANAGER, UserRole.ADMIN),
            "can_approve": can_approve_buy_plans(user),
            "user": user,
        }
    )
    return template_response("htmx/partials/buy_plans/_supervise.html", ctx)


@router.get("/v2/partials/buy-plans/supervise", response_class=HTMLResponse)
async def buy_plans_supervise_partial(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Manager/ops "Supervise" lens body: triage panel + all-scope deal board.

    Role-gated — a non-supervisor is served the mine-scope board so no other
    user's plans leak (see ``_render_supervise_body``).
    """
    return _render_supervise_body(request, user, db)


@router.get("/v2/partials/buy-plans/{plan_id}", response_class=HTMLResponse)
async def buy_plan_detail_partial(
    request: Request,
    plan_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return buy plan detail as HTML partial."""
    bp = get_buyplan_for_user(
        db,
        user,
        plan_id,
        options=[
            joinedload(BuyPlan.lines).joinedload(BuyPlanLine.offer),
            joinedload(BuyPlan.lines).joinedload(BuyPlanLine.requirement),
            joinedload(BuyPlan.lines).joinedload(BuyPlanLine.buyer),
            joinedload(BuyPlan.quote),
            joinedload(BuyPlan.requisition),
            joinedload(BuyPlan.submitted_by),
            joinedload(BuyPlan.approved_by),
        ],
    )

    ctx = _base_ctx(request, user, "buy-plans")
    ctx.update(
        {
            "bp": bp,
            "lines": bp.lines or [],
            "is_ops_member": _is_ops_member(user, db),
            "can_resource": _can_resource(user),
            "user": user,
            # Most-urgent flag reason so the indicator states the issue at first glance.
            "top_flag": summarize_top_flag(bp.ai_flags),
        }
    )
    return template_response("htmx/partials/buy_plans/detail.html", ctx)


@router.post("/v2/partials/buy-plans/{plan_id}/submit", response_class=HTMLResponse)
async def buy_plan_submit_partial(
    request: Request,
    plan_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Submit a draft buy plan with SO# — returns refreshed detail partial."""
    from ..services.buyplan_notifications import (
        notify_approved,
        notify_submitted,
        run_notify_bg,
    )
    from ..services.buyplan_workflow import submit_buy_plan

    # Per-record ownership: non-owner SALES/TRADER → 404 before any mutation.
    get_buyplan_for_user(db, user, plan_id)

    form = await request.form()
    so = form.get("sales_order_number", "").strip()
    if not so:
        raise HTTPException(400, "Sales Order # is required")

    try:
        plan = submit_buy_plan(
            plan_id,
            so,
            user,
            db,
            customer_po_number=form.get("customer_po_number") or None,
            salesperson_notes=form.get("salesperson_notes") or None,
        )
        db.commit()
        if plan.auto_approved:
            await run_notify_bg(notify_approved, plan.id)
        else:
            await run_notify_bg(notify_submitted, plan.id)
    except ValueError as e:
        raise HTTPException(400, str(e))

    return await buy_plan_detail_partial(request, plan_id, user, db)


@router.post("/v2/partials/buy-plans/{plan_id}/approve", response_class=HTMLResponse)
async def buy_plan_approve_partial(
    request: Request,
    plan_id: int,
    user: User = Depends(require_buyplan_approver),
    db: Session = Depends(get_db),
):
    """Approve or reject a pending buy plan — returns refreshed detail.

    Gated by ``require_buyplan_approver`` (403 unless the user holds the per-user
    can_approve_buy_plans right). Reject requires a reason (enforced in the service).

    QP Phase C1: the approval engine OWNS the gate. We look up the open BUY_PLAN
    ApprovalRequest for this plan and resolve it via the engine's ``decide`` — which drives
    the buy-plan side effects (ACTIVE + buyer tasks / DRAFT) in the SAME transaction. We let
    ``decide`` raise (no swallowing) so a side-effect failure rolls back the whole decision
    atomically (RISK 1). If NO open request exists — a plan that went PENDING before C1
    deployed — we fall back to the legacy ``approve_buy_plan`` and log a WARNING (RISK 3,
    transition window; the fallback is removed in a follow-up once no pre-C1 plans remain).
    """
    from sqlalchemy import select as _select

    from ..constants import ApprovalRequestStatus, ApprovalSubjectType
    from ..models.approvals import ApprovalRequest
    from ..services.approvals.service import decide as svc_decide
    from ..services.buyplan_notifications import (
        notify_approved,
        notify_rejected,
        run_notify_bg,
    )
    from ..services.buyplan_workflow import approve_buy_plan

    form = await request.form()
    action = form.get("action", "approve")
    origin = form.get("origin", "")
    notes = form.get("notes")

    open_request = (
        db.execute(
            _select(ApprovalRequest).where(
                ApprovalRequest.subject_type == ApprovalSubjectType.BUY_PLAN,
                ApprovalRequest.subject_id == plan_id,
                ApprovalRequest.status == ApprovalRequestStatus.REQUESTED,
            )
        )
        .scalars()
        .first()
    )

    try:
        if open_request is not None:
            # Engine path: decide() resolves the request AND drives the plan side effects.
            svc_decide(db, open_request.id, user, action, comment=notes or None)
        else:
            # RISK 3 fallback: plan pending pre-C1 with no engine request yet.
            logger.warning(
                "Buy plan {} approve/reject with no open engine request — falling back to legacy approve_buy_plan",
                plan_id,
            )
            approve_buy_plan(plan_id, action, user, db, notes=notes)
        db.commit()
        if action == "approve":
            await run_notify_bg(notify_approved, plan_id)
        else:
            await run_notify_bg(notify_rejected, plan_id)
    except PermissionError as e:
        # The dependency already 403s unauthorized callers; this maps the service's
        # defense-in-depth approval-right check to 403 (not 400) if it is ever reached.
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))

    if origin == "supervise":
        return _render_supervise_body(request, user, db)

    return await buy_plan_detail_partial(request, plan_id, user, db)


@router.post("/v2/partials/buy-plans/{plan_id}/verify-so", response_class=HTMLResponse)
async def buy_plan_verify_so_partial(
    request: Request,
    plan_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Ops verifies SO — returns refreshed detail."""
    from ..services.buyplan_notifications import (
        notify_so_rejected,
        notify_so_verified,
        run_notify_bg,
    )
    from ..services.buyplan_workflow import verify_so

    form = await request.form()
    action = form.get("action", "approve")
    origin = form.get("origin", "")

    try:
        plan = verify_so(
            plan_id,
            action,
            user,
            db,
            rejection_note=form.get("rejection_note"),
        )
        db.commit()
        if action == "approve":
            await run_notify_bg(notify_so_verified, plan.id)
        else:
            await run_notify_bg(notify_so_rejected, plan.id, action=action)
    except (ValueError, PermissionError) as e:
        raise HTTPException(400, str(e))

    if origin == "supervise":
        return _render_supervise_body(request, user, db)

    return await buy_plan_detail_partial(request, plan_id, user, db)


@router.post("/v2/partials/buy-plans/{plan_id}/lines/{line_id}/confirm-po", response_class=HTMLResponse)
async def buy_plan_confirm_po_partial(
    request: Request,
    plan_id: int,
    line_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Buyer confirms PO.

    Returns the refreshed detail partial by default (``origin=""``, the original
    behavior). When ``origin == "queue"`` the call came from the buyer's Orders lens
    and we return the re-rendered orders queue so the confirmed line drops out.
    """
    from datetime import datetime

    from ..services.buyplan_notifications import notify_po_confirmed, run_notify_bg
    from ..services.buyplan_workflow import confirm_po

    # Per-record ownership: non-owner SALES/TRADER → 404 before any mutation.
    get_buyplan_for_user(db, user, plan_id)

    form = await request.form()
    po_number = form.get("po_number", "").strip()
    ship_date_str = form.get("estimated_ship_date", "")
    origin = form.get("origin", "")

    if not po_number:
        raise HTTPException(400, "PO number is required")

    ship_date = None
    if ship_date_str:
        try:
            ship_date = datetime.fromisoformat(ship_date_str)
        except ValueError:
            ship_date = datetime.now()
    else:
        ship_date = datetime.now()

    try:
        confirm_po(plan_id, line_id, po_number, ship_date, user, db)
        db.commit()
        await run_notify_bg(notify_po_confirmed, plan_id, line_id=line_id)
    except ValueError as e:
        raise HTTPException(400, str(e))

    if origin == "queue":
        return await buy_plans_orders_partial(request, user, db)

    return await buy_plan_detail_partial(request, plan_id, user, db)


@router.post("/v2/partials/buy-plans/{plan_id}/lines/{line_id}/resource", response_class=HTMLResponse)
async def buy_plan_resource_line_partial(
    request: Request,
    plan_id: int,
    line_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Re-source a line whose vendor PO was cancelled.

    Records the cancellation (vendor performance), marks the offer sold + the vendor
    unavailable, drops the line into the open claim pool, and fires the URGENT backfill
    alert to all other buyers. ``scope=plan`` re-sources the plan's other cut lines too.
    """
    from ..services.buyplan_notifications import notify_resource_requested, run_notify_bg
    from ..services.buyplan_workflow import resource_line

    # Per-record ownership (non-owner SALES/TRADER → 404) + PO-cutter role gate (403).
    get_buyplan_for_user(db, user, plan_id)
    _require_po_cutter(user)

    form = await request.form()
    reason_code = form.get("reason_code", "").strip()
    reason_note = (form.get("reason_note") or "").strip() or None
    scope = form.get("scope", "line")
    origin = form.get("origin", "")
    also_line_ids = [int(i) for i in form.getlist("also_line_ids")] if scope == "plan" else []

    if not reason_code:
        raise HTTPException(400, "A re-source reason is required")

    try:
        payload = resource_line(plan_id, line_id, reason_code, reason_note, user, db, also_line_ids=also_line_ids)
        db.commit()
    except ValueError as e:
        # Log before re-raising so a real failure (e.g. an un-keyable requirement deep in
        # the service) leaves a server trace instead of a silent, mislabeled 400.
        logger.warning("Re-source failed for plan {} line {}: {}", plan_id, line_id, e)
        raise HTTPException(400, str(e))

    # Broadcast one urgent alert PER re-sourced line (scope=plan re-sources siblings too,
    # and each pooled line needs its own claim).
    for resourced in payload["resourced_lines"]:
        await run_notify_bg(
            notify_resource_requested, plan_id, line_id=resourced["line_id"], actor_id=user.id, reason=reason_code
        )

    if origin == "resource":
        return await buy_plans_resource_partial(request, user, db)
    return await buy_plan_detail_partial(request, plan_id, user, db)


@router.post("/v2/partials/buy-plans/{plan_id}/lines/{line_id}/claim", response_class=HTMLResponse)
async def buy_plan_claim_line_partial(
    request: Request,
    plan_id: int,
    line_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Claim an open-pool (RESOURCING) line. First-to-claim wins.

    No per-record ownership gate: the open pool is intentionally claimable by ANY active
    PO-cutter regardless of who owns the parent requisition. The lost race → 409.
    """
    from ..services.buyplan_workflow import claim_line

    _require_po_cutter(user)

    form = await request.form()
    origin = form.get("origin", "")

    try:
        claim_line(plan_id, line_id, user, db)
        db.commit()
    except ValueError as e:
        logger.info("Claim lost/invalid for plan {} line {} by {}: {}", plan_id, line_id, user.id, e)
        raise HTTPException(409, str(e))

    if origin == "resource":
        return await buy_plans_resource_partial(request, user, db)
    return await buy_plan_detail_partial(request, plan_id, user, db)


@router.post("/v2/partials/buy-plans/{plan_id}/lines/{line_id}/verify-po", response_class=HTMLResponse)
async def buy_plan_verify_po_partial(
    request: Request,
    plan_id: int,
    line_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Ops verifies PO — returns refreshed detail."""
    from ..services.buyplan_notifications import (
        notify_completed,
        notify_po_rejected,
        run_notify_bg,
    )
    from ..services.buyplan_workflow import check_completion, verify_po

    form = await request.form()
    action = form.get("action", "approve")
    origin = form.get("origin", "")

    try:
        verify_po(plan_id, line_id, action, user, db, rejection_note=form.get("rejection_note"))
        db.commit()
        if action == "reject":
            await run_notify_bg(notify_po_rejected, plan_id, line_id=line_id)
        updated = check_completion(plan_id, db)
        if updated and updated.status == BuyPlanStatus.COMPLETED:
            db.commit()
            await run_notify_bg(notify_completed, plan_id)
    except (ValueError, PermissionError) as e:
        raise HTTPException(400, str(e))

    if origin == "supervise":
        return _render_supervise_body(request, user, db)

    return await buy_plan_detail_partial(request, plan_id, user, db)


@router.post("/v2/partials/buy-plans/{plan_id}/lines/{line_id}/issue", response_class=HTMLResponse)
async def buy_plan_flag_issue_partial(
    request: Request,
    plan_id: int,
    line_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Buyer flags issue on a line — returns refreshed detail."""
    from ..services.buyplan_workflow import flag_line_issue

    # Per-record ownership: non-owner SALES/TRADER → 404 before any mutation.
    get_buyplan_for_user(db, user, plan_id)

    form = await request.form()
    issue_type = form.get("issue_type", "other")
    note = form.get("note", "")

    try:
        flag_line_issue(plan_id, line_id, issue_type, user, db, note=note)
        db.commit()
    except ValueError as e:
        raise HTTPException(400, str(e))

    return await buy_plan_detail_partial(request, plan_id, user, db)


@router.post("/v2/partials/buy-plans/{plan_id}/cancel", response_class=HTMLResponse)
async def buy_plan_cancel_partial(
    request: Request,
    plan_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Cancel a buy plan — delegates to the service (line cascade + notification)."""
    from ..services.buyplan_notifications import notify_cancelled, run_notify_bg
    from ..services.buyplan_workflow import cancel_buy_plan

    # Per-record ownership: non-owner SALES/TRADER → 404 before any mutation.
    get_buyplan_for_user(db, user, plan_id)

    form = await request.form()
    try:
        plan = cancel_buy_plan(plan_id, user, db, reason=form.get("reason"))
        db.commit()
        await run_notify_bg(notify_cancelled, plan.id)
    except ValueError as e:
        raise HTTPException(400, str(e))

    return await buy_plan_detail_partial(request, plan_id, user, db)


# ── Settings: Ops verification group ─────────────────────────────────


@router.get("/v2/partials/settings/ops-group", response_class=HTMLResponse)
async def settings_ops_group_tab(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Ops verification group management tab — admin only."""
    if user.role != UserRole.ADMIN:
        raise HTTPException(403, "Admin only")
    from .admin.buy_plan_ops import ops_group_context

    ctx = _base_ctx(request, user, "settings")
    ctx.update(ops_group_context(db))
    return template_response("htmx/partials/settings/ops_group.html", ctx)


@router.get("/v2/partials/settings/users", response_class=HTMLResponse)
async def settings_users_tab(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Users management tab (invite / role / activate) — admin only."""
    if user.role != UserRole.ADMIN:
        raise HTTPException(403, "Admin only")
    from .admin.users import users_context

    ctx = _base_ctx(request, user, "settings")
    ctx.update(users_context(db))
    return template_response("htmx/partials/settings/users.html", ctx)


@router.get("/v2/partials/settings/scorecard", response_class=HTMLResponse)
async def settings_scorecard_tab(
    request: Request,
    time_range: str = "this_month",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Activity Scorecard tab — per-user activity leaderboard. Manager/admin only.

    A leaderboard of all users' activity is oversight/performance data, so it is gated
    to the supervisor tier (MANAGER + ADMIN) via is_manager_or_admin — buyers/sales/
    traders never see it. On an HX-Request triggered by the time-range selector only the
    table fragment is swapped; the first paint (and a direct hit) renders the full tab.
    """
    if not is_manager_or_admin(user):
        raise HTTPException(403, "Managers and admins only")
    from ..services.activity_scorecard import (
        DEFAULT_TIME_RANGE,
        TALK_TIME_BUCKET_SECONDS,
        TIME_RANGE_LABELS,
        TIME_RANGES,
        compute_scorecard,
        scoring_formula_parts,
    )

    if time_range not in TIME_RANGES:
        time_range = DEFAULT_TIME_RANGE

    ctx = _base_ctx(request, user, "settings")
    ctx.update(
        {
            "rows": compute_scorecard(db, time_range),
            "time_range": time_range,
            "time_ranges": TIME_RANGES,
            "time_range_labels": TIME_RANGE_LABELS,
            "formula_parts": scoring_formula_parts(),
            "talk_bucket_min": TALK_TIME_BUCKET_SECONDS // 60,
        }
    )

    # Time-range selector swaps only the table fragment; full-tab on first paint.
    if request.headers.get("HX-Request") == "true" and request.headers.get("HX-Trigger-Name") == "time_range":
        return template_response("htmx/partials/settings/_scorecard_table.html", ctx)
    return template_response("htmx/partials/settings/scorecard.html", ctx)


@router.post("/v2/partials/buy-plans/{plan_id}/reset", response_class=HTMLResponse)
async def buy_plan_reset_partial(
    request: Request,
    plan_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Reset halted/cancelled plan to draft — returns refreshed detail."""
    from ..services.buyplan_workflow import reset_buy_plan_to_draft

    # Per-record ownership: non-owner SALES/TRADER → 404 before any mutation.
    get_buyplan_for_user(db, user, plan_id)

    try:
        reset_buy_plan_to_draft(plan_id, user, db)
        db.commit()
    except ValueError as e:
        raise HTTPException(400, str(e))

    return await buy_plan_detail_partial(request, plan_id, user, db)


# ── Sourcing partials ──────────────────────────────────────────────────


@router.get("/v2/sourcing/{requirement_id}", response_class=HTMLResponse)
async def v2_sourcing_page(request: Request, requirement_id: int, db: Session = Depends(get_db)):
    """Full page load for sourcing results."""
    user = get_user(request, db)
    if not user:
        return template_response(
            "htmx/login.html", {"request": request, "password_login": _password_login_enabled(), **_vite_assets()}
        )
    ctx = _base_ctx(request, user, "requisitions")
    ctx["partial_url"] = f"/v2/partials/sourcing/{requirement_id}"
    return page_response(ctx)


@router.get("/v2/sourcing/leads/{lead_id}", response_class=HTMLResponse)
async def v2_lead_detail_page(request: Request, lead_id: int, db: Session = Depends(get_db)):
    """Full page load for lead detail."""
    user = get_user(request, db)
    if not user:
        return template_response(
            "htmx/login.html", {"request": request, "password_login": _password_login_enabled(), **_vite_assets()}
        )
    ctx = _base_ctx(request, user, "requisitions")
    ctx["partial_url"] = f"/v2/partials/sourcing/leads/{lead_id}"
    return page_response(ctx)


@router.get("/v2/partials/sourcing/{requirement_id}/stream")
async def sourcing_stream(
    request: Request,
    requirement_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """SSE endpoint for sourcing search progress.

    Streams per-source completion events as connectors finish searching.
    Client connects via hx-ext="sse" sse-connect attribute.
    Channel: sourcing:{requirement_id}
    """
    from sse_starlette.sse import EventSourceResponse

    from ..services.sse_broker import broker

    req = db.query(Requirement).filter(Requirement.id == requirement_id).first()
    if not req:
        raise HTTPException(404, "Requirement not found")

    async def event_generator():
        async for msg in broker.listen(f"sourcing:{requirement_id}"):
            if await request.is_disconnected():
                break
            yield {
                "event": msg["event"],
                "data": msg["data"],
            }

    return EventSourceResponse(event_generator())


@router.post("/v2/partials/sourcing/{requirement_id}/search", response_class=HTMLResponse)
async def sourcing_search_trigger(
    request: Request,
    requirement_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Trigger multi-source search for a requirement.

    Runs connectors in parallel, publishes SSE events per source completion, syncs leads
    on completion, returns redirect to sourcing results.
    """
    import asyncio
    import json

    from ..services.sse_broker import broker

    req = db.query(Requirement).filter(Requirement.id == requirement_id).first()
    if not req:
        raise HTTPException(404, "Requirement not found")
    # Search triggers connector SPEND + cross-owner disclosure — scope to the owner.
    require_requisition_access(db, req.requisition_id, user, label="Requirement")

    mpn = req.primary_mpn or ""
    sources = ["brokerbin", "nexar", "digikey", "mouser", "oemsecrets", "element14"]
    channel = f"sourcing:{requirement_id}"
    all_sightings = []

    async def search_source(source_name):
        start_t = time.time()
        try:
            from ..search_service import quick_search_mpn

            raw = await quick_search_mpn(mpn, db)
            results = raw if isinstance(raw, list) else raw.get("sightings", [])
            elapsed = int((time.time() - start_t) * 1000)
            count = len(results) if results else 0
            await broker.publish(
                channel,
                "source-complete",
                json.dumps({"source": source_name, "count": count, "elapsed_ms": elapsed, "status": "done"}),
            )
            return results or []
        except Exception as exc:
            elapsed = int((time.time() - start_t) * 1000)
            logger.error("Sourcing search failed for {} on {}: {}", mpn, source_name, exc)
            await broker.publish(
                channel,
                "source-complete",
                json.dumps(
                    {"source": source_name, "count": 0, "elapsed_ms": elapsed, "status": "failed", "error": str(exc)}
                ),
            )
            return []

    results_by_source = await asyncio.gather(*[search_source(s) for s in sources], return_exceptions=True)

    for source_results in results_by_source:
        if isinstance(source_results, list):
            all_sightings.extend(source_results)

    await broker.publish(
        channel, "search-complete", json.dumps({"total": len(all_sightings), "requirement_id": requirement_id})
    )

    return HTMLResponse(status_code=200, headers={"HX-Redirect": f"/v2/sourcing/{requirement_id}"})


@router.get("/v2/partials/sourcing/{requirement_id}", response_class=HTMLResponse)
async def sourcing_results_partial(
    request: Request,
    requirement_id: int,
    confidence: str = "",
    safety: str = "",
    freshness: str = "",
    source: str = "",
    status: str = "",
    contactability: str = "",
    corroborated: str = "",
    sort: str = "best",
    page: int = Query(1, ge=1),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return sourcing results with lead cards for a requirement.

    Supports filtering by confidence band, safety band, freshness window, source type,
    buyer status, contactability, and corroboration. Sorts by best overall (default),
    freshest, safest, easiest to contact, or most proven.
    """
    from ..models.sourcing_lead import SourcingLead

    req = db.query(Requirement).filter(Requirement.id == requirement_id).first()
    if not req:
        raise HTTPException(404, "Requirement not found")

    query = db.query(SourcingLead).filter(SourcingLead.requirement_id == requirement_id)

    if confidence:
        bands = [b.strip() for b in confidence.split(",")]
        query = query.filter(SourcingLead.confidence_band.in_(bands))
    if safety:
        bands = [b.strip() for b in safety.split(",")]
        query = query.filter(SourcingLead.vendor_safety_band.in_(bands))
    if freshness and freshness != "all":
        now = datetime.now(timezone.utc)
        cutoffs = {"24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30)}
        if freshness in cutoffs:
            query = query.filter(SourcingLead.source_last_seen_at >= now - cutoffs[freshness])
    if source:
        sources_list = [s.strip() for s in source.split(",")]
        query = query.filter(SourcingLead.primary_source_type.in_(sources_list))
    if status and status != "all":
        statuses = [s.strip() for s in status.split(",")]
        query = query.filter(SourcingLead.buyer_status.in_(statuses))
    if contactability == "has_email":
        query = query.filter(SourcingLead.contact_email.isnot(None))
    elif contactability == "has_phone":
        query = query.filter(SourcingLead.contact_phone.isnot(None))
    if corroborated == "yes":
        query = query.filter(SourcingLead.corroborated.is_(True))
    elif corroborated == "no":
        query = query.filter(SourcingLead.corroborated.is_(False))

    sort_map = {
        "best": [SourcingLead.confidence_score.desc()],
        "freshest": [SourcingLead.source_last_seen_at.desc().nullslast()],
        "safest": [SourcingLead.vendor_safety_score.desc().nullslast()],
        "contact": [SourcingLead.contactability_score.desc().nullslast()],
        "proven": [SourcingLead.historical_success_score.desc().nullslast()],
    }
    for col in sort_map.get(sort, sort_map["best"]):
        query = query.order_by(col)

    total = query.count()
    per_page = 24
    leads = query.offset((page - 1) * per_page).limit(per_page).all()

    lead_sighting_data = {}
    if leads:
        for lead in leads:
            best_sighting = (
                db.query(Sighting)
                .filter(
                    Sighting.requirement_id == requirement_id,
                    Sighting.vendor_name_normalized == lead.vendor_name_normalized,
                )
                .order_by(Sighting.created_at.desc().nullslast())
                .first()
            )
            if best_sighting:
                lead_sighting_data[lead.id] = {
                    "qty_available": best_sighting.qty_available,
                    "unit_price": best_sighting.unit_price,
                }

    ctx = _base_ctx(request, user, "requisitions")
    ctx.update(
        {
            "requirement": req,
            "leads": leads,
            "lead_sighting_data": lead_sighting_data,
            "total": total,
            "page": page,
            "total_pages": max(1, (total + per_page - 1) // per_page),
            "per_page": per_page,
            "f_confidence": confidence,
            "f_safety": safety,
            "f_freshness": freshness,
            "f_source": source,
            "f_status": status,
            "f_contactability": contactability,
            "f_corroborated": corroborated,
            "f_sort": sort,
        }
    )
    return template_response("htmx/partials/sourcing/results.html", ctx)


@router.get("/v2/partials/sourcing/leads/{lead_id}", response_class=HTMLResponse)
async def lead_detail_partial(
    request: Request,
    lead_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return lead detail as HTML partial.

    Loads the SourcingLead, its evidence (sorted by confidence_impact desc), groups
    evidence by source category, fetches vendor card and best sighting.
    """
    from ..models.sourcing_lead import LeadEvidence, SourcingLead
    from ..services.sourcing_leads import _source_category

    lead = db.query(SourcingLead).filter(SourcingLead.id == lead_id).first()
    if not lead:
        raise HTTPException(404, "Lead not found")

    evidence = (
        db.query(LeadEvidence)
        .filter(LeadEvidence.lead_id == lead.id)
        .order_by(LeadEvidence.confidence_impact.desc().nullslast())
        .all()
    )

    evidence_by_category = {}
    for ev in evidence:
        cat = _source_category(ev.source_type)
        evidence_by_category.setdefault(cat, []).append(ev)

    category_labels = {
        "api": "API",
        "marketplace": "Marketplace",
        "salesforce_history": "Salesforce History",
        "avail_history": "Avail History",
        "web_ai": "Web / AI",
        "safety_review": "Safety Review",
        "buyer_feedback": "Buyer Feedback",
    }

    requirement = db.query(Requirement).filter(Requirement.id == lead.requirement_id).first()

    vendor_card = lead.vendor_card

    best_sighting = (
        db.query(Sighting)
        .filter(
            Sighting.requirement_id == lead.requirement_id,
            Sighting.vendor_name_normalized == lead.vendor_name_normalized,
        )
        .order_by(Sighting.created_at.desc().nullslast())
        .first()
    )

    ctx = _base_ctx(request, user, "requisitions")
    ctx.update(
        {
            "lead": lead,
            "evidence": evidence,
            "evidence_by_category": evidence_by_category,
            "category_labels": category_labels,
            "requirement": requirement,
            "vendor_card": vendor_card,
            "best_sighting": best_sighting,
        }
    )
    return template_response("htmx/partials/sourcing/lead_detail.html", ctx)


@router.post("/v2/partials/sourcing/leads/{lead_id}/status", response_class=HTMLResponse)
async def lead_status_update(
    request: Request,
    lead_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Update lead buyer status.

    Returns updated lead card when called from results view (for OOB swap), or updated
    lead detail when called from lead detail page.
    """
    from ..models.sourcing_lead import SourcingLead
    from ..services.sourcing_leads import update_lead_status

    _lead = db.get(SourcingLead, lead_id)
    if not _lead:
        raise HTTPException(404, "Lead not found")
    require_requisition_access(db, _lead.requisition_id, user, label="Lead")

    form = await request.form()
    status_val = form.get("status", "").strip()
    note = form.get("note", "").strip() or None

    try:
        lead = update_lead_status(
            db,
            lead_id,
            status_val,
            note=note,
            actor_user_id=user.id,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))

    if not lead:
        raise HTTPException(404, "Lead not found")

    hx_target = request.headers.get("HX-Target", "")
    referer = request.headers.get("HX-Current-URL", "")

    # Workspace panel context: return updated panel detail
    if hx_target == "split-right-sourcing":
        return await lead_panel_partial(request, lead_id, user, db)

    # Full-page lead detail context
    if "/leads/" in referer:
        return await lead_detail_partial(request, lead_id, user, db)

    # Workspace lead row context: return updated lead row
    if hx_target.startswith("lead-row-"):
        best_sighting = (
            db.query(Sighting)
            .filter(
                Sighting.requirement_id == lead.requirement_id,
                Sighting.vendor_name_normalized == lead.vendor_name_normalized,
            )
            .order_by(Sighting.created_at.desc().nullslast())
            .first()
        )
        lead_sighting_data = {}
        if best_sighting:
            lead_sighting_data[lead.id] = {
                "qty_available": best_sighting.qty_available,
                "unit_price": best_sighting.unit_price,
            }
        ctx = _base_ctx(request, user, "requisitions")
        ctx.update({"lead": lead, "lead_sighting_data": lead_sighting_data, "selected_lead_id": 0})
        return template_response("htmx/partials/sourcing/lead_row.html", ctx)

    # Default: card view (results grid)
    best_sighting = (
        db.query(Sighting)
        .filter(
            Sighting.requirement_id == lead.requirement_id,
            Sighting.vendor_name_normalized == lead.vendor_name_normalized,
        )
        .order_by(Sighting.created_at.desc().nullslast())
        .first()
    )
    lead_sighting_data = {}
    if best_sighting:
        lead_sighting_data[lead.id] = {
            "qty_available": best_sighting.qty_available,
            "unit_price": best_sighting.unit_price,
        }

    ctx = _base_ctx(request, user, "requisitions")
    ctx.update({"lead": lead, "lead_sighting_data": lead_sighting_data})
    return template_response("htmx/partials/sourcing/lead_card.html", ctx)


@router.post("/v2/partials/sourcing/leads/{lead_id}/feedback", response_class=HTMLResponse)
async def lead_feedback(
    request: Request,
    lead_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Add feedback event to a lead without changing status.

    Returns updated lead detail.
    """
    from ..models.sourcing_lead import SourcingLead
    from ..services.sourcing_leads import append_lead_feedback

    _lead = db.get(SourcingLead, lead_id)
    if not _lead:
        raise HTTPException(404, "Lead not found")
    require_requisition_access(db, _lead.requisition_id, user, label="Lead")

    form = await request.form()
    note = form.get("note", "").strip() or None
    reason_code = form.get("reason_code", "").strip() or None
    contact_method = form.get("contact_method", "").strip() or None

    lead = append_lead_feedback(
        db,
        lead_id,
        note=note,
        reason_code=reason_code,
        contact_method=contact_method,
        actor_user_id=user.id,
    )
    if not lead:
        raise HTTPException(404, "Lead not found")

    return await lead_detail_partial(request, lead_id, user, db)


# ── Sourcing workspace (split-panel) ─────────────────────────────────


@router.get("/v2/sourcing/{requirement_id}/workspace", response_class=HTMLResponse)
async def v2_sourcing_workspace_page(request: Request, requirement_id: int, db: Session = Depends(get_db)):
    """Full page load for sourcing workspace (split-panel view)."""
    user = get_user(request, db)
    if not user:
        return template_response(
            "htmx/login.html", {"request": request, "password_login": _password_login_enabled(), **_vite_assets()}
        )
    ctx = _base_ctx(request, user, "requisitions")
    ctx["partial_url"] = f"/v2/partials/sourcing/{requirement_id}/workspace"
    return page_response(ctx)


@router.get("/v2/partials/sourcing/{requirement_id}/workspace", response_class=HTMLResponse)
async def sourcing_workspace_partial(
    request: Request,
    requirement_id: int,
    confidence: str = "",
    safety: str = "",
    freshness: str = "",
    source: str = "",
    status: str = "",
    contactability: str = "",
    corroborated: str = "",
    sort: str = "best",
    page: int = Query(1, ge=1),
    lead: int = Query(0, ge=0),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return sourcing workspace split-panel layout.

    Reuses the same filtering/sorting logic as sourcing_results_partial but renders lead
    rows in a split-panel instead of card grid. Optional lead=ID param pre-selects a
    lead in the right panel.
    """
    from ..models.sourcing_lead import SourcingLead

    req = db.query(Requirement).filter(Requirement.id == requirement_id).first()
    if not req:
        raise HTTPException(404, "Requirement not found")

    query = db.query(SourcingLead).filter(SourcingLead.requirement_id == requirement_id)

    if confidence:
        bands = [b.strip() for b in confidence.split(",")]
        query = query.filter(SourcingLead.confidence_band.in_(bands))
    if safety:
        bands = [b.strip() for b in safety.split(",")]
        query = query.filter(SourcingLead.vendor_safety_band.in_(bands))
    if freshness and freshness != "all":
        now = datetime.now(timezone.utc)
        cutoffs = {"24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30)}
        if freshness in cutoffs:
            query = query.filter(SourcingLead.source_last_seen_at >= now - cutoffs[freshness])
    if source:
        sources_list = [s.strip() for s in source.split(",")]
        query = query.filter(SourcingLead.primary_source_type.in_(sources_list))
    if status and status != "all":
        statuses = [s.strip() for s in status.split(",")]
        query = query.filter(SourcingLead.buyer_status.in_(statuses))
    if contactability == "has_email":
        query = query.filter(SourcingLead.contact_email.isnot(None))
    elif contactability == "has_phone":
        query = query.filter(SourcingLead.contact_phone.isnot(None))
    if corroborated == "yes":
        query = query.filter(SourcingLead.corroborated.is_(True))
    elif corroborated == "no":
        query = query.filter(SourcingLead.corroborated.is_(False))

    sort_map = {
        "best": [SourcingLead.confidence_score.desc()],
        "freshest": [SourcingLead.source_last_seen_at.desc().nullslast()],
        "safest": [SourcingLead.vendor_safety_score.desc().nullslast()],
        "contact": [SourcingLead.contactability_score.desc().nullslast()],
        "proven": [SourcingLead.historical_success_score.desc().nullslast()],
    }
    for col in sort_map.get(sort, sort_map["best"]):
        query = query.order_by(col)

    total = query.count()
    per_page = 24
    leads = query.offset((page - 1) * per_page).limit(per_page).all()

    lead_sighting_data = {}
    if leads:
        for ld in leads:
            best_sighting = (
                db.query(Sighting)
                .filter(
                    Sighting.requirement_id == requirement_id,
                    Sighting.vendor_name_normalized == ld.vendor_name_normalized,
                )
                .order_by(Sighting.created_at.desc().nullslast())
                .first()
            )
            if best_sighting:
                lead_sighting_data[ld.id] = {
                    "qty_available": best_sighting.qty_available,
                    "unit_price": best_sighting.unit_price,
                }

    ctx = _base_ctx(request, user, "requisitions")
    ctx.update(
        {
            "requirement": req,
            "leads": leads,
            "lead_sighting_data": lead_sighting_data,
            "total": total,
            "page": page,
            "total_pages": max(1, (total + per_page - 1) // per_page),
            "per_page": per_page,
            "f_confidence": confidence,
            "f_safety": safety,
            "f_freshness": freshness,
            "f_source": source,
            "f_status": status,
            "f_contactability": contactability,
            "f_corroborated": corroborated,
            "f_sort": sort,
            "selected_lead_id": lead if lead else 0,
        }
    )
    return template_response("htmx/partials/sourcing/workspace.html", ctx)


@router.get("/v2/partials/sourcing/{requirement_id}/workspace-list", response_class=HTMLResponse)
async def sourcing_workspace_list_partial(
    request: Request,
    requirement_id: int,
    confidence: str = "",
    safety: str = "",
    freshness: str = "",
    source: str = "",
    status: str = "",
    contactability: str = "",
    corroborated: str = "",
    sort: str = "best",
    page: int = Query(1, ge=1),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return just the lead list rows for the workspace left panel.

    Used when filters change — swaps only #lead-list-content without touching the right
    panel or overall layout.
    """
    from ..models.sourcing_lead import SourcingLead

    req = db.query(Requirement).filter(Requirement.id == requirement_id).first()
    if not req:
        raise HTTPException(404, "Requirement not found")

    query = db.query(SourcingLead).filter(SourcingLead.requirement_id == requirement_id)

    if confidence:
        bands = [b.strip() for b in confidence.split(",")]
        query = query.filter(SourcingLead.confidence_band.in_(bands))
    if safety:
        bands = [b.strip() for b in safety.split(",")]
        query = query.filter(SourcingLead.vendor_safety_band.in_(bands))
    if freshness and freshness != "all":
        now = datetime.now(timezone.utc)
        cutoffs = {"24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30)}
        if freshness in cutoffs:
            query = query.filter(SourcingLead.source_last_seen_at >= now - cutoffs[freshness])
    if source:
        sources_list = [s.strip() for s in source.split(",")]
        query = query.filter(SourcingLead.primary_source_type.in_(sources_list))
    if status and status != "all":
        statuses = [s.strip() for s in status.split(",")]
        query = query.filter(SourcingLead.buyer_status.in_(statuses))
    if contactability == "has_email":
        query = query.filter(SourcingLead.contact_email.isnot(None))
    elif contactability == "has_phone":
        query = query.filter(SourcingLead.contact_phone.isnot(None))
    if corroborated == "yes":
        query = query.filter(SourcingLead.corroborated.is_(True))
    elif corroborated == "no":
        query = query.filter(SourcingLead.corroborated.is_(False))

    sort_map = {
        "best": [SourcingLead.confidence_score.desc()],
        "freshest": [SourcingLead.source_last_seen_at.desc().nullslast()],
        "safest": [SourcingLead.vendor_safety_score.desc().nullslast()],
        "contact": [SourcingLead.contactability_score.desc().nullslast()],
        "proven": [SourcingLead.historical_success_score.desc().nullslast()],
    }
    for col in sort_map.get(sort, sort_map["best"]):
        query = query.order_by(col)

    total = query.count()
    per_page = 24
    leads = query.offset((page - 1) * per_page).limit(per_page).all()

    lead_sighting_data = {}
    if leads:
        for ld in leads:
            best_sighting = (
                db.query(Sighting)
                .filter(
                    Sighting.requirement_id == requirement_id,
                    Sighting.vendor_name_normalized == ld.vendor_name_normalized,
                )
                .order_by(Sighting.created_at.desc().nullslast())
                .first()
            )
            if best_sighting:
                lead_sighting_data[ld.id] = {
                    "qty_available": best_sighting.qty_available,
                    "unit_price": best_sighting.unit_price,
                }

    ctx = _base_ctx(request, user, "requisitions")
    ctx.update(
        {
            "requirement": req,
            "leads": leads,
            "lead_sighting_data": lead_sighting_data,
            "total": total,
            "page": page,
            "total_pages": max(1, (total + per_page - 1) // per_page),
            "per_page": per_page,
            "f_confidence": confidence,
            "f_safety": safety,
            "f_freshness": freshness,
            "f_source": source,
            "f_status": status,
            "f_contactability": contactability,
            "f_corroborated": corroborated,
            "f_sort": sort,
            "selected_lead_id": 0,
        }
    )

    html_parts = []
    if leads:
        for ld in leads:
            rendered = templates.get_template("htmx/partials/sourcing/lead_row.html").render({**ctx, "lead": ld})
            html_parts.append(rendered)
    else:
        html_parts.append(
            '<div class="flex flex-col items-center justify-center py-12 text-gray-400">'
            '<p class="text-sm">No leads found</p></div>'
        )

    return HTMLResponse("".join(html_parts))


@router.get("/v2/partials/sourcing/leads/{lead_id}/panel", response_class=HTMLResponse)
async def lead_panel_partial(
    request: Request,
    lead_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return condensed lead detail for workspace right panel.

    Same data as lead_detail_partial but uses lead_panel.html template (no breadcrumb,
    collapsible sections, denser layout). Includes OOB swap of the lead row in the left
    panel for highlight update.
    """
    from ..models.sourcing_lead import LeadEvidence, SourcingLead
    from ..services.sourcing_leads import _source_category

    lead = db.query(SourcingLead).filter(SourcingLead.id == lead_id).first()
    if not lead:
        raise HTTPException(404, "Lead not found")

    evidence = (
        db.query(LeadEvidence)
        .filter(LeadEvidence.lead_id == lead.id)
        .order_by(LeadEvidence.confidence_impact.desc().nullslast())
        .all()
    )

    evidence_by_category = {}
    for ev in evidence:
        cat = _source_category(ev.source_type)
        evidence_by_category.setdefault(cat, []).append(ev)

    category_labels = {
        "api": "API",
        "marketplace": "Marketplace",
        "salesforce_history": "Salesforce History",
        "avail_history": "Avail History",
        "web_ai": "Web / AI",
        "safety_review": "Safety Review",
        "buyer_feedback": "Buyer Feedback",
    }

    requirement = db.query(Requirement).filter(Requirement.id == lead.requirement_id).first()
    vendor_card = lead.vendor_card

    best_sighting = (
        db.query(Sighting)
        .filter(
            Sighting.requirement_id == lead.requirement_id,
            Sighting.vendor_name_normalized == lead.vendor_name_normalized,
        )
        .order_by(Sighting.created_at.desc().nullslast())
        .first()
    )

    ctx = _base_ctx(request, user, "requisitions")
    ctx.update(
        {
            "lead": lead,
            "evidence": evidence,
            "evidence_by_category": evidence_by_category,
            "category_labels": category_labels,
            "requirement": requirement,
            "vendor_card": vendor_card,
            "best_sighting": best_sighting,
        }
    )
    return template_response("htmx/partials/sourcing/lead_panel.html", ctx)


# ── Materials partials ────────────────────────────────────────────────


@router.get("/v2/partials/materials", response_class=HTMLResponse)
async def materials_list_partial(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Redirect to faceted workspace — all materials browsing uses the sidebar
    layout."""
    return await materials_workspace_partial(request, user, db)


@router.get("/v2/partials/materials/workspace", response_class=HTMLResponse)
async def materials_workspace_partial(
    request: Request,
    user: User = Depends(require_access(AccessKey.MATERIALS)),
    db: Session = Depends(get_db),
):
    """Render the faceted search workspace layout."""
    from ..models.intelligence import MaterialCard

    total_materials = db.query(MaterialCard).filter(MaterialCard.deleted_at.is_(None)).count()
    all_subs = [sub for subs in COMMODITY_TREE.values() for sub in subs]
    ctx = _base_ctx(request, user, "materials")
    ctx["total_materials"] = total_materials
    ctx["display_names"] = {sub: get_display_name(sub) for sub in all_subs}
    ctx["global_facet_counts"] = get_global_facet_counts(db)
    # The workspace is require_user, but POST /api/materials/add is require_buyer —
    # hide the "Add part" button from roles whose submit would 403 (dead-end otherwise).
    ctx["can_add_parts"] = has_buyer_role(user)
    return template_response("htmx/partials/materials/workspace.html", ctx)


@router.get("/v2/partials/materials/filters/manufacturers", response_class=HTMLResponse)
async def materials_filters_manufacturers_partial(
    request: Request,
    commodity: str = "",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Render manufacturer filter dropdown."""
    from ..services.faceted_search_service import get_manufacturer_options

    options = get_manufacturer_options(db, commodity=commodity or None)
    ctx = _base_ctx(request, user, "materials")
    ctx["manufacturer_options"] = options
    return template_response("htmx/partials/materials/filters/manufacturers.html", ctx)


@router.get("/v2/partials/materials/filters/global", response_class=HTMLResponse)
async def materials_filters_global_partial(
    request: Request,
    commodity: str = "",
    q: str = "",
    sub_filters: str = "{}",
    statuses: str = "",
    lifecycle: str = "",
    rohs: str = "",
    condition: str = "",
    has_datasheet: str = "false",
    has_validation_conflict: str = "false",
    has_stock: str = "false",
    has_price: str = "false",
    has_crosses: str = "false",
    internal: str = "all",
    searched_within: str = "any",
    min_searches: str = "0",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Render global facets (lifecycle / RoHS / condition / has-datasheet) with live
    counts.

    Receives the FULL active filter set (same wire params as the results list) so the
    rendered counts match the visible results instead of overstating; each facet's own
    selection is excluded inside get_global_facet_counts (self-exclusion).
    """
    parsed_filters = _parse_filter_json(sub_filters, coerce_numeric=True)
    filters = _parse_card_filter_params(
        statuses,
        lifecycle,
        rohs,
        condition,
        has_datasheet,
        has_validation_conflict,
        has_stock,
        has_price,
        has_crosses,
        internal,
        searched_within,
        min_searches,
    )
    filters["manufacturers"] = _pop_manufacturers(parsed_filters)
    filters["q"] = q or None
    filters["sub_filters"] = parsed_filters or None
    counts = get_global_facet_counts(db, commodity=commodity or None, filters=filters)
    ctx = _base_ctx(request, user, "materials")
    ctx["global_facet_counts"] = counts
    return template_response("htmx/partials/materials/filters/global.html", ctx)


@router.get("/v2/partials/manufacturers/search", response_class=HTMLResponse)
async def manufacturer_search(
    request: Request,
    q: str = "",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Typeahead search for manufacturers by name or alias."""
    from sqlalchemy import Text, cast

    from ..models.sourcing import Manufacturer

    results = []
    if q.strip():
        pattern = f"%{escape_like(q.strip())}%"
        by_name = db.query(Manufacturer).filter(Manufacturer.canonical_name.ilike(pattern, escape="\\")).limit(10).all()
        results = list(by_name)
        if len(results) < 10:
            seen_ids = {r.id for r in results}
            alias_matches = (
                db.query(Manufacturer)
                .filter(
                    Manufacturer.id.notin_(seen_ids),
                    cast(Manufacturer.aliases, Text).ilike(pattern, escape="\\"),
                )
                .limit(10 - len(results))
                .all()
            )
            results.extend(alias_matches)

    ctx = _base_ctx(request, user, "requisitions")
    ctx.update({"results": results, "q": q.strip()})
    return template_response("htmx/partials/manufacturers/search_results.html", ctx)


@router.post("/v2/partials/manufacturers/add", response_class=HTMLResponse)
async def manufacturer_add(
    request: Request,
    name: str = Form(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Add a new manufacturer on the fly from typeahead."""
    from ..models.sourcing import Manufacturer

    name = name.strip()
    if not name:
        return HTMLResponse('<div class="px-3 py-1.5 text-xs text-red-500">Name required</div>')

    existing = db.query(Manufacturer).filter_by(canonical_name=name).first()
    if not existing:
        mfr = Manufacturer(canonical_name=name)
        db.add(mfr)
        db.commit()

    return HTMLResponse(
        f'<div class="px-3 py-1.5 text-xs font-medium text-brand-600" data-mfr-name="{name}">Added: {name}</div>'
    )


@router.get("/v2/partials/materials/filters/tree", response_class=HTMLResponse)
async def materials_filters_tree_partial(
    request: Request,
    commodity: str = "",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Render the commodity category tree for the faceted sidebar."""
    commodity_counts = get_commodity_counts(db)
    # Build display_names dict for template (.get() usage)
    all_subs: list[str] = [sub for subs in COMMODITY_TREE.values() for sub in subs]
    display_names = {sub: get_display_name(sub) for sub in all_subs}
    ctx = _base_ctx(request, user, "materials")
    ctx.update(
        {
            "commodity_tree": COMMODITY_TREE,
            "commodity_counts": commodity_counts,
            "display_names": display_names,
            "active_commodity": commodity.lower().strip() if commodity else "",
        }
    )
    return template_response("htmx/partials/materials/filters/tree.html", ctx)


@router.get("/v2/partials/materials/filters/sub", response_class=HTMLResponse)
async def materials_filters_sub_partial(
    request: Request,
    commodity: str = "",
    sub_filters: str = "{}",
    q: str = "",
    statuses: str = "",
    lifecycle: str = "",
    rohs: str = "",
    condition: str = "",
    has_datasheet: str = "false",
    has_validation_conflict: str = "false",
    has_stock: str = "false",
    has_price: str = "false",
    has_crosses: str = "false",
    internal: str = "all",
    searched_within: str = "any",
    min_searches: str = "0",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Render sub-filters for a selected commodity with live facet counts.

    Receives the FULL active filter set (same wire params as the results list) so facet
    counts reflect active q / brand / confidence / global / sourcing filters instead of
    overstating; spec-filter self-exclusion (OR-within-facet) stays inside
    get_facet_counts pass 2.
    """
    if not commodity.strip():
        # No commodity scope — render the placeholder nudge (skip the facet/coverage
        # service calls; subfilters.html handles the commodity_selected=False branch).
        ctx = _base_ctx(request, user, "materials")
        ctx["commodity_selected"] = False
        return template_response("htmx/partials/materials/filters/subfilters.html", ctx)

    # Parse active filters so facet counts reflect current selection.
    parsed_filters = _parse_filter_json(sub_filters)
    # Card-level narrowing — shared wire-param parsing with the results list, plus the
    # 'manufacturers' entry that rides inside sub_filters (a MaterialCard column, not a
    # spec facet — left in parsed_filters it would zero every facet count).
    card_filters = _parse_card_filter_params(
        statuses,
        lifecycle,
        rohs,
        condition,
        has_datasheet,
        has_validation_conflict,
        has_stock,
        has_price,
        has_crosses,
        internal,
        searched_within,
        min_searches,
    )
    card_filters["manufacturers"] = _pop_manufacturers(parsed_filters)
    card_filters["q"] = q or None

    subfilter_options = get_subfilter_options(db, commodity)
    facet_counts = get_facet_counts(db, commodity, active_filters=parsed_filters or None, card_filters=card_filters)
    ctx = _base_ctx(request, user, "materials")
    ctx.update(
        {
            "subfilter_options": subfilter_options,
            "facet_counts": facet_counts,
            "commodity_selected": True,
            "spec_coverage": get_commodity_spec_coverage(db, commodity),
            "commodity_display": get_display_name(commodity),
        }
    )
    return template_response("htmx/partials/materials/filters/subfilters.html", ctx)


@router.get("/v2/partials/materials/ai-interpret", response_class=HTMLResponse)
async def materials_ai_interpret_partial(
    request: Request,
    q: str = "",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Interpret a natural language query using AI and return pre-selection chip."""
    from ..services.materials_ai_search import get_parent_for_commodity, interpret_search_query

    result = None
    if q and len(q.strip().split()) >= 3:
        result = await interpret_search_query(q)

    ctx = _base_ctx(request, user, "materials")
    ctx["ai_result"] = result
    if result and result.get("commodity"):
        ctx["ai_parent"] = get_parent_for_commodity(result["commodity"])
    else:
        ctx["ai_parent"] = ""
    return template_response("htmx/partials/materials/ai_interpret.html", ctx)


@router.get("/v2/partials/materials/faceted", response_class=HTMLResponse)
async def materials_faceted_partial(
    request: Request,
    commodity: str = "",
    q: str = "",
    sub_filters: str = "{}",
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    verified_only: bool = Query(False),
    statuses: str = Query(""),
    lifecycle: str = Query(""),
    rohs: str = Query(""),
    condition: str = Query(""),
    has_datasheet: str = Query("false"),
    has_validation_conflict: str = Query("false"),
    has_stock: str = Query("false"),
    has_price: str = Query("false"),
    has_crosses: str = Query("false"),
    internal: str = Query("all"),
    searched_within: str = Query("any"),
    min_searches: str = Query("0"),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return faceted-search material list as HTML partial."""
    from ..models.intelligence import MaterialVendorHistory

    parsed_filters = _parse_filter_json(sub_filters, coerce_numeric=True)
    manufacturers = _pop_manufacturers(parsed_filters)

    # Shared degrade-don't-500 parsing — same helper as both sidebar count routes, so
    # the list and the counts can never read the same query string differently.
    card_params = _parse_card_filter_params(
        statuses,
        lifecycle,
        rohs,
        condition,
        has_datasheet,
        has_validation_conflict,
        has_stock,
        has_price,
        has_crosses,
        internal,
        searched_within,
        min_searches,
    )

    materials, total = search_materials_faceted(
        db,
        commodity=commodity or None,
        q=q or None,
        sub_filters=parsed_filters or None,
        manufacturers=manufacturers,
        verified_only=verified_only,
        **card_params,
        limit=limit,
        offset=offset,
    )

    # Attach vendor stats (matching existing materials list pattern)
    card_ids = [m.id for m in materials]
    vendor_stats: dict = {}
    if card_ids:
        stats = (
            db.query(
                MaterialVendorHistory.material_card_id,
                sqlfunc.count(MaterialVendorHistory.id),
                sqlfunc.min(MaterialVendorHistory.last_price),
                sqlfunc.count(sqlfunc.distinct(MaterialVendorHistory.last_currency)),
                sqlfunc.max(MaterialVendorHistory.last_currency),
            )
            .filter(MaterialVendorHistory.material_card_id.in_(card_ids))
            .group_by(MaterialVendorHistory.material_card_id)
            .all()
        )
        # currency shown only when a card's vendor rows are single-currency; mixed → default $
        vendor_stats = {s[0]: (s[1], s[2], s[4] if s[3] == 1 else None) for s in stats}

    # Attach spec chips for display. In commodity context: the selected commodity's
    # is_primary keys (same keys as before; non-scalar/missing values are now SKIPPED
    # instead of rendering dict-reprs or 500ing on raw-scalar entries). Without a
    # commodity: each card's OWN category's primary keys (one batched query — no N+1);
    # whenever that yields no chips (schema-less category OR a card lacking values for
    # every primary key) fall back to the first 3 scalar specs_structured entries; the
    # template renders "label: value" there.
    def _spec_scalar(raw):
        val = raw.get("value") if isinstance(raw, dict) else raw
        return val if isinstance(val, (str, int, float, bool)) else None

    primary_by_cat: dict[str, dict[str, str]] = {}
    if commodity:
        primary_by_cat[commodity.lower().strip()] = {
            s.spec_key: s.display_name
            for s in db.query(CommoditySpecSchema).filter_by(commodity=commodity, is_primary=True).all()
        }
    else:
        card_cats = {(m.category or "").lower().strip() for m in materials if m.category}
        if card_cats:
            schema_rows = (
                db.query(CommoditySpecSchema)
                .filter(CommoditySpecSchema.commodity.in_(card_cats), CommoditySpecSchema.is_primary.is_(True))
                .all()
            )
            for s in schema_rows:
                primary_by_cat.setdefault(s.commodity, {})[s.spec_key] = s.display_name

    # Dual-brand cell: the " · maker" suffix renders only when brand (OEM label) and
    # manufacturer (actual maker) are DIFFERENT COMPANIES. Compare NORMALIZED forms, not
    # raw strings — B1 writes the canonical OEM into brand while manufacturer keeps the
    # raw alias (lossless by design), so an exact-string compare renders tautologies like
    # "Hewlett Packard Enterprise · HP" (the same company twice).
    from ..services.manufacturer_normalizer import normalize_brand_name

    for m in materials:
        vc, bp, cur = vendor_stats.get(m.id, (0, None, None))
        m._vendor_count = vc
        m._best_price = bp
        m._best_currency = cur
        m._show_maker_suffix = bool(
            m.brand
            and m.manufacturer
            and normalize_brand_name(db, m.brand).lower() != normalize_brand_name(db, m.manufacturer).lower()
        )
        specs = m.specs_structured or {}
        card_cat = commodity.lower().strip() if commodity else (m.category or "").lower().strip()
        primary_keys = primary_by_cat.get(card_cat, {})
        chips = [
            {"label": primary_keys[k], "value": _spec_scalar(specs[k])}
            for k in primary_keys
            if k in specs and _spec_scalar(specs[k]) is not None
        ]
        if not commodity and not chips:
            # No schema-known primary values for this card — first 3 scalar entries,
            # labelled by their prettified spec key.
            for k, raw in specs.items():
                val = _spec_scalar(raw)
                if val is None:
                    continue
                chips.append({"label": k.replace("_", " "), "value": val})
                if len(chips) >= 3:
                    break
        m._primary_specs = chips

    # Coverage-aware empty state: a parametric zero-result inside a commodity usually
    # means "not yet spec-enriched", not "no such parts". Coverage is computed only when
    # the nudge could render (zero results + active parametric sub_filters + commodity).
    parametric_active = bool(commodity and parsed_filters)
    spec_coverage = None
    if total == 0 and parametric_active:
        spec_coverage = get_commodity_spec_coverage(db, commodity)

    # FRU crosswalk: when the query hits fru_links (either direction), render the
    # full matrix / "Used in FRUs" section above the card results. This is the
    # destination every "/v2/materials?q=<pn>" FRU deep link promises (the search
    # panel's "View full FRU matrix" CTA and fru_section's part-navigation links) —
    # a crosswalk-only PN matches no material card, so the section must not depend
    # on card results. Both lookups are indexed point reads; non-MPN text queries
    # simply miss and render nothing.
    # The section is ADDITIVE, so the lookups get the same scoped try/except
    # search_history_panel uses — a crosswalk failure degrades to "no FRU section"
    # and must never 500 the whole materials list (the primary surface).
    fru_view = None
    fru_reverse = None
    if q:
        from ..services.fru_matrix_service import get_fru_view, get_reverse_view

        try:
            fru_view = get_fru_view(db, q)
            fru_reverse = get_reverse_view(db, q)
        except Exception:
            logger.exception("materials faceted FRU section failed q={} user={}", q, user.id)
            fru_view = None
            fru_reverse = None

    ctx = _base_ctx(request, user, "materials")
    ctx.update(
        {
            "materials": materials,
            "q": q,
            "total": total,
            "limit": limit,
            "offset": offset,
            "commodity": commodity,
            "commodity_display": get_display_name(commodity) if commodity else "",
            "category": commodity,
            "top_categories": [],
            "interpreted_query": "",
            "faceted": True,
            "parametric_active": parametric_active,
            "spec_coverage": spec_coverage,
            "fru_view": fru_view,
            "fru_usages": fru_reverse.usages if fru_reverse else (),
            "fru_usages_total": fru_reverse.total if fru_reverse else 0,
            "fru_query": q,
        }
    )
    return template_response("htmx/partials/materials/list.html", ctx)


@router.get("/v2/partials/materials/add-form", response_class=HTMLResponse)
async def material_add_form_partial(
    request: Request,
    user: User = Depends(require_buyer),
):
    """Render the Add-part modal form (loaded into #modal-content).

    require_buyer matches POST /api/materials/add — the form must never render for a
    role whose submit would 403 (the workspace also hides the button via
    has_buyer_role).
    NOTE: must stay registered BEFORE /v2/partials/materials/{card_id} — the path
    would otherwise be captured by the card_id route.
    """
    from .materials import render_add_modal

    return render_add_modal(request)


@router.get("/v2/partials/materials/{card_id}/enrich-status", response_class=HTMLResponse)
async def material_enrich_status_partial(
    request: Request,
    card_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Render the enrichment-status badge for the card detail header.

    While the card is still ``unenriched`` the badge polls this route every 15s
    ("Queued for enrichment"). Once enrichment_status leaves ``unenriched`` the route
    answers HTTP 286 — htmx swaps the final badge and STOPS polling.
    """
    from ..constants import MaterialEnrichmentStatus
    from ..models.intelligence import MaterialCard

    card = db.get(MaterialCard, card_id)
    if not card or card.deleted_at is not None:
        # Polling sub-resource, not a navigable page: htmx neither swaps nor cancels
        # an `every 15s` poll on a 4xx, so a 404 would leave a detail view open after
        # the card is deleted hammering this route forever. 286 stops the poll; the
        # empty body clears the badge.
        return HTMLResponse("", status_code=286)

    ctx = _base_ctx(request, user, "materials")
    ctx["card"] = card
    response = template_response("htmx/partials/materials/enrich_status.html", ctx)
    if card.enrichment_status != MaterialEnrichmentStatus.UNENRICHED:
        # 286: htmx's stop-polling status — the final badge still swaps in.
        response.status_code = 286
    return response


@router.post("/v2/partials/materials/{card_id}/conflicts/{key}/accept", response_class=HTMLResponse)
async def material_conflict_accept(
    request: Request,
    card_id: int,
    key: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Accept a validation conflict's evidence value — a human decision.

    Writes the evidence value at manual/100 (set_category for ``category``,
    set_brand/set_manufacturer for the dual-brand columns, record_spec for spec
    keys) and clears that key's conflict entries. An optional ``source`` form field
    selects among multiple evidence entries for the key (de-dupe is per
    (key, source)); without it the highest-(tier, confidence) entry wins. Returns
    the refreshed detail partial.
    """
    from ..models.intelligence import MaterialCard
    from ..services.spec_tiers import clear_validation_conflicts, set_brand, set_category, set_manufacturer
    from ..services.spec_write_service import record_spec

    card = db.get(MaterialCard, card_id)
    if not card or card.deleted_at is not None:
        raise HTTPException(404, "Material card not found")

    entries = [c for c in (card.validation_conflicts or []) if c.get("key") == key]
    if not entries:
        raise HTTPException(404, f"No validation conflict recorded for {key!r}")

    form = await request.form()
    source = str(form.get("source") or "").strip()
    chosen = next((c for c in entries if (c.get("evidence") or {}).get("source") == source), None)
    if chosen is None:
        chosen = max(
            entries,
            key=lambda c: (
                (c.get("evidence") or {}).get("tier") or 0,
                (c.get("evidence") or {}).get("confidence") or 0.0,
            ),
        )
    value = (chosen.get("evidence") or {}).get("value")

    if key == "category":
        wrote = set_category(card, value, "manual", 1.0)
    elif key == "brand":
        wrote = set_brand(card, value, "manual", 1.0)
    elif key == "manufacturer":
        wrote = set_manufacturer(card, value, "manual", 1.0)
    else:
        wrote = record_spec(db, card.id, key, value, source="manual", confidence=1.0)
    if not wrote:
        # The accepted value could not be written — off-vocab category, schema gone
        # after a commodity flip, or enum/numeric rejection. KEEP the conflict entry
        # (it is the only persisted record of the contradiction) and surface the
        # failure instead of silently pretending the decision was applied.
        logger.warning(
            "Material card {} conflict-accept on {!r}: value {!r} could not be written — entry kept",
            card_id,
            key,
            value,
        )
        response = await material_detail_partial(request, card_id, user, db)
        response.headers["HX-Trigger"] = json.dumps(
            {
                "showToast": {
                    "message": (
                        f'Couldn\'t apply "{value}" to {key} — the value no longer fits '
                        "this card's schema. The conflict was kept."
                    ),
                    "type": "warning",
                }
            }
        )
        return response
    clear_validation_conflicts(card, key)
    db.commit()
    logger.info("Material card {} conflict on {!r} accepted ({!r}) by {}", card_id, key, value, user.email)
    return await material_detail_partial(request, card_id, user, db)


@router.get("/v2/partials/materials/fru-lookup", response_class=HTMLResponse)
async def fru_lookup_partial(
    request: Request,
    q: str = Query("", max_length=100),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """FRU crosswalk lookup: render whichever view matches the part number.

    Forward view when q is a known FRU, reverse "Used in FRUs" view when q appears
    as a related PN (11S/model/tray/...), an empty state when neither.
    NOTE: must stay registered BEFORE /v2/partials/materials/{card_id} — the path
    would otherwise be captured by the card_id route.
    """
    from ..services.fru_matrix_service import get_fru_view, get_reverse_view

    reverse = get_reverse_view(db, q) if q else None
    ctx = _base_ctx(request, user, "materials")
    ctx.update(
        {
            "fru_view": get_fru_view(db, q) if q else None,
            "fru_usages": reverse.usages if reverse else (),
            "fru_usages_total": reverse.total if reverse else 0,
            "fru_query": q,
            "show_empty": bool(q),
        }
    )
    return template_response("htmx/partials/materials/fru_section.html", ctx)


@router.get("/v2/partials/materials/{card_id}", response_class=HTMLResponse)
async def material_detail_partial(
    request: Request,
    card_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return material card detail as HTML partial."""
    from ..models.intelligence import MaterialCard
    from ..services.fru_matrix_service import get_fru_view, get_reverse_view

    card = (
        db.query(MaterialCard)
        .filter(
            MaterialCard.id == card_id,
            MaterialCard.deleted_at.is_(None),
        )
        .first()
    )
    if not card:
        raise HTTPException(404, "Material card not found")

    sightings = sightings_for_card(db, card_id, limit=50)
    offers = offers_for_card(db, card_id, limit=50)
    mpn = card.display_mpn or card.normalized_mpn
    reverse = get_reverse_view(db, mpn)
    ctx = _base_ctx(request, user, "materials")
    ctx.update(
        {
            "card": card,
            "sightings": sightings,
            "offers": offers,
            "fru_view": get_fru_view(db, mpn),
            "fru_usages": reverse.usages,
            "fru_usages_total": reverse.total,
        }
    )
    return template_response("htmx/partials/materials/detail.html", ctx)


@router.get(
    "/v2/partials/materials/{card_id}/tab/{tab_name}",
    response_class=HTMLResponse,
)
async def material_tab_partial(
    request: Request,
    card_id: int,
    tab_name: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return a material detail tab partial."""
    from ..models.intelligence import MaterialCard, MaterialVendorHistory

    card = db.get(MaterialCard, card_id)
    if not card:
        return HTMLResponse(
            "<p class='text-gray-400 text-sm py-4 text-center'>Material not found</p>",
            status_code=404,
        )

    ctx = _base_ctx(request, user, "materials")
    ctx["card"] = card

    if tab_name == "vendors":
        ctx["vendors"] = (
            db.query(MaterialVendorHistory)
            .filter_by(material_card_id=card_id)
            .order_by(MaterialVendorHistory.last_seen.desc().nullslast())
            .all()
        )
        return template_response("htmx/partials/materials/tabs/vendors.html", ctx)
    elif tab_name == "customers":
        ctx["customers"] = customer_purchases_for_card(db, card_id, limit=200)
        return template_response("htmx/partials/materials/tabs/customers.html", ctx)
    elif tab_name == "sourcing":
        ctx["requirements"] = requirements_for_card(db, card_id, limit=200)
        return template_response("htmx/partials/materials/tabs/sourcing.html", ctx)
    elif tab_name == "price_history":
        from ..models.price_snapshot import MaterialPriceSnapshot

        ctx["snapshots"] = (
            db.query(MaterialPriceSnapshot)
            .filter_by(material_card_id=card_id)
            .order_by(MaterialPriceSnapshot.recorded_at.desc())
            .limit(200)
            .all()
        )
        return template_response("htmx/partials/materials/tabs/price_history.html", ctx)
    elif tab_name == "files":
        return template_response("htmx/partials/materials/tabs/files.html", ctx)
    else:
        return HTMLResponse(
            "<p class='text-gray-400 text-sm py-4 text-center'>Unknown tab</p>",
            status_code=404,
        )


@router.put("/v2/partials/materials/{card_id}", response_class=HTMLResponse)
async def update_material_card(
    request: Request,
    card_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Update material card fields.

    Returns refreshed detail.
    """
    from ..models.intelligence import MaterialCard

    card = (
        db.query(MaterialCard)
        .filter(
            MaterialCard.id == card_id,
            MaterialCard.deleted_at.is_(None),
        )
        .first()
    )
    if not card:
        raise HTTPException(404, "Material card not found")

    form = await request.form()
    updatable = [
        "description",
        "package_type",
        "lifecycle_status",
        "rohs_status",
        "pin_count",
    ]
    for field in updatable:
        if field in form:
            val = form[field].strip() if form[field] else None
            if field == "pin_count" and val:
                try:
                    val = int(val)
                except (ValueError, TypeError):
                    val = None
            setattr(card, field, val or None)

    # Manufacturer is a PROVENANCED column (dual-brand, migration 097) — NEVER raw
    # setattr: a raw write leaves NULL provenance, ranks at the legacy floor (50), and
    # the next decode (85) / trio re-ingest (95) silently reverts the human's edit.
    # Same contract as routers/materials.py::update_material — through the F1 ladder
    # at manual/100 (canonicalized via the alias table), with the same conflict-
    # clearing semantics as the category path below.
    manufacturer_toast: str | None = None
    if "manufacturer" in form:
        from ..services.manufacturer_normalizer import normalize_brand_name
        from ..services.spec_tiers import clear_validation_conflicts, set_manufacturer

        raw_manufacturer = (str(form["manufacturer"]) if form["manufacturer"] else "").strip()
        if raw_manufacturer:
            # A PUT carrying a non-empty maker is a re-assertion — clear any recorded
            # validation conflict for it (even unchanged: the human looked and
            # confirmed their value), mirroring the category path below.
            clear_validation_conflicts(card, "manufacturer")
            # Canonical-to-CANONICAL comparison (exact match short-circuits the alias
            # lookups): legacy cards store non-canonical aliases ("TI", "HP" — the
            # stored value pre-dates the ladder), and the edit form round-trips the
            # stored value verbatim — comparing canonical(incoming) against the RAW
            # stored value would see "Texas Instruments" != "TI" on every unrelated
            # save and silently re-stamp the maker as manual (tier 100), locking out
            # every future enrichment correction.
            if raw_manufacturer != (card.manufacturer or "") and normalize_brand_name(
                db, raw_manufacturer
            ) != normalize_brand_name(db, card.manufacturer or ""):
                set_manufacturer(card, raw_manufacturer, "manual", 1.0)
            # Canonical-equal → no-op: an unchanged value must NOT be re-stamped as a
            # manual (tier 100) edit just because the user saved another field.
        elif card.manufacturer:
            # Empty/whitespace → no-op: the ladder never blanks a value
            # (set_manufacturer contract — the old raw write could silently blank the
            # maker here). Tell the user instead of silently dropping the edit,
            # mirroring the category blank-rejection toast below.
            manufacturer_toast = f'Manufacturer can\'t be cleared — kept "{card.manufacturer}".'

    # Category NEVER goes through raw setattr: a raw write would leave the OLD
    # provenance columns attached to the NEW value (the next enrichment pass would
    # silently revert the human's correction), skip the stale-commodity facet purge,
    # and persist off-vocab free text. Route it through the F1 ladder instead —
    # "manual" is tier 100, so a deliberate human change always wins, gets provenance
    # stamped, and purges the old commodity's facets.
    category_toast: str | None = None
    if "category" in form:
        from ..services.category_normalizer import normalize_category
        from ..services.spec_tiers import clear_validation_conflicts, set_category

        raw_category = (str(form["category"]) if form["category"] else "").strip()
        canonical = normalize_category(raw_category)
        if canonical is not None:
            # A PUT carrying a canonical category is a re-assertion — clear any
            # recorded validation conflict for it (even unchanged: the human looked
            # and confirmed their value).
            clear_validation_conflicts(card, "category")
        if canonical is not None and canonical != card.category:
            set_category(card, canonical, "manual", 1.0)
        elif canonical is None and raw_category:
            # Off-vocab free text — never persisted (it would be invisible to every
            # commodity filter). Tell the user instead of silently dropping the edit.
            category_toast = (
                f'Category "{raw_category}" is not a recognized commodity — kept '
                f'"{card.category or "none"}". Use a canonical key like hdd, ssd or dram.'
            )
        elif not raw_category and card.category:
            # The ladder never blanks an existing category (set_category contract).
            category_toast = f'Category can\'t be cleared — kept "{card.category}".'
        # canonical == card.category → no-op: an unchanged value must NOT be re-stamped
        # as a manual (tier 100) edit just because the user saved another field.

    db.commit()
    logger.info("Material card {} updated by {}", card_id, user.email)
    response = await material_detail_partial(request, card_id, user, db)
    toast_messages = [m for m in (category_toast, manufacturer_toast) if m]
    if toast_messages:
        # Surface the rejection(s) WITHOUT breaking the partial swap, via the existing
        # showToast HX-Trigger convention bridged to $store.toast (htmx_app.js).
        # HX-Trigger is a single JSON event map, so both rejections share one toast.
        response.headers["HX-Trigger"] = json.dumps(
            {"showToast": {"message": " ".join(toast_messages), "type": "warning"}}
        )
    return response


# ── Quotes partials ───────────────────────────────────────────────────


@router.get("/v2/partials/quotes/{quote_id}", response_class=HTMLResponse)
async def quote_detail_partial(
    request: Request,
    quote_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return quote detail as HTML partial."""
    quote = get_quote_for_user(
        db,
        user,
        quote_id,
        options=[
            joinedload(Quote.customer_site).joinedload(CustomerSite.company),
            joinedload(Quote.requisition),
            joinedload(Quote.created_by),
        ],
    )
    lines = db.query(QuoteLine).filter(QuoteLine.quote_id == quote_id).all()
    offers = (
        db.query(Offer).filter(Offer.requisition_id == quote.requisition_id).order_by(Offer.created_at.desc()).all()
    )
    ctx = _base_ctx(request, user, "quotes")
    ctx.update({"quote": quote, "lines": lines, "offers": offers})
    return template_response("htmx/partials/quotes/detail.html", ctx)


@router.put("/v2/partials/quotes/{quote_id}/lines/{line_id}", response_class=HTMLResponse)
async def update_quote_line(
    request: Request,
    quote_id: int,
    line_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Inline edit a quote line item, return updated row."""
    line = db.get(QuoteLine, line_id)
    if not line or line.quote_id != quote_id:
        raise HTTPException(404, "Line not found")
    # Scope the parent quote through ownership (raises 404 for SALES accessing other users' quotes).
    get_quote_for_user(db, user, line.quote_id)
    form = await request.form()
    if "mpn" in form:
        line.mpn = form["mpn"]
    if "manufacturer" in form:
        line.manufacturer = form["manufacturer"]
    if "qty" in form:
        try:
            line.qty = int(form["qty"])
        except (ValueError, TypeError):
            raise HTTPException(400, "qty must be an integer")
    if "cost_price" in form:
        try:
            line.cost_price = float(form["cost_price"])
        except (ValueError, TypeError):
            raise HTTPException(400, "cost_price must be a number")
    if "sell_price" in form:
        try:
            line.sell_price = float(form["sell_price"])
        except (ValueError, TypeError):
            raise HTTPException(400, "sell_price must be a number")
    if line.sell_price and float(line.sell_price) > 0 and line.cost_price is not None:
        line.margin_pct = round((float(line.sell_price) - float(line.cost_price)) / float(line.sell_price) * 100, 2)
    db.commit()
    ctx = _base_ctx(request, user, "quotes")
    ctx["line"] = line
    return template_response("htmx/partials/quotes/line_row.html", ctx)


@router.delete("/v2/partials/quotes/{quote_id}/lines/{line_id}", response_class=HTMLResponse)
async def delete_quote_line(
    request: Request,
    quote_id: int,
    line_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Delete a quote line item."""
    line = db.get(QuoteLine, line_id)
    if not line or line.quote_id != quote_id:
        raise HTTPException(404, "Line not found")
    # Scope the parent quote through ownership (raises 404 for SALES accessing other users' quotes).
    get_quote_for_user(db, user, line.quote_id)
    db.delete(line)
    db.commit()
    return HTMLResponse("")


@router.post("/v2/partials/quotes/{quote_id}/lines", response_class=HTMLResponse)
async def add_quote_line(
    request: Request,
    quote_id: int,
    mpn: str = Form(...),
    manufacturer: str = Form(""),
    qty: int = Form(1),
    cost_price: float = Form(0),
    sell_price: float = Form(0),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Add a new line item to a quote, return the new row HTML."""
    # Ownership/existence check (raises 404 if the quote isn't visible to the user).
    get_quote_for_user(db, user, quote_id)
    margin_pct = 0.0
    if sell_price > 0:
        margin_pct = round((sell_price - cost_price) / sell_price * 100, 2)
    line = QuoteLine(
        quote_id=quote_id,
        mpn=mpn,
        manufacturer=manufacturer or None,
        qty=qty,
        cost_price=cost_price,
        sell_price=sell_price,
        margin_pct=margin_pct,
    )
    db.add(line)
    db.commit()
    db.refresh(line)
    ctx = _base_ctx(request, user, "quotes")
    ctx["line"] = line
    return template_response("htmx/partials/quotes/line_row.html", ctx)


@router.post("/v2/partials/quotes/{quote_id}/add-offer/{offer_id}", response_class=HTMLResponse)
async def add_offer_to_quote(
    request: Request,
    quote_id: int,
    offer_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Add an offer as a line item to a quote."""
    quote = get_quote_for_user(db, user, quote_id)
    offer = db.get(Offer, offer_id)
    if not offer:
        raise HTTPException(404, "Offer not found")
    if offer.requisition_id is not None and offer.requisition_id != quote.requisition_id:
        raise HTTPException(
            status_code=403,
            detail={"error": "offer does not belong to this quote's requisition"},
        )
    line = QuoteLine(
        quote_id=quote_id,
        offer_id=offer_id,
        mpn=offer.mpn,
        manufacturer=offer.manufacturer,
        qty=offer.qty_available or 0,
        cost_price=float(offer.unit_price) if offer.unit_price else 0,
        sell_price=0,
        margin_pct=0,
    )
    db.add(line)
    db.commit()
    db.refresh(line)
    ctx = _base_ctx(request, user, "quotes")
    ctx["line"] = line
    return template_response("htmx/partials/quotes/line_row.html", ctx)


@router.post("/v2/partials/quotes/{quote_id}/send", response_class=HTMLResponse)
async def send_quote_htmx(
    request: Request,
    quote_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Send the quote to the customer (real email) — returns refreshed detail partial.

    Delegates to the canonical quote-send service so this button actually emails the
    customer (captures Graph ids, writes an outbound ActivityLog, hard-blocks DNC). In
    TESTING the service skips the real Graph call but still marks the quote sent.
    """
    quote = get_quote_for_user(db, user, quote_id)
    testing = os.environ.get("TESTING") == "1"
    # Only acquire a real M365 token outside TESTING — the service skips the Graph send in
    # TESTING, and require_fresh_token (called directly, not via Depends) would 401 in tests.
    token = ""
    if not testing:
        from ..dependencies import require_fresh_token

        token = await require_fresh_token(request, db)
    try:
        await send_quote_email(db, quote, user, token=token, testing=testing)
    except QuoteSendDNCBlocked:
        return HTMLResponse(
            '<div class="rounded bg-rose-50 border border-rose-200 text-rose-700 text-xs px-2 py-1.5">'
            "This recipient is do-not-contact — quote not sent.</div>"
        )
    except QuoteSendError as exc:
        return HTMLResponse(
            '<div class="rounded bg-rose-50 border border-rose-200 text-rose-700 text-xs px-2 py-1.5">'
            f"{html_mod.escape(exc.detail)}</div>"
        )
    return await quote_detail_partial(request, quote_id, user, db)


@router.post("/v2/partials/quotes/{quote_id}/result", response_class=HTMLResponse)
async def quote_result_htmx(
    request: Request,
    quote_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Mark quote result (won/lost) — returns refreshed detail partial."""

    quote = get_quote_for_user(db, user, quote_id)
    form = await request.form()
    result = form.get("result", "")
    if result not in ("won", "lost"):
        raise HTTPException(400, "Result must be 'won' or 'lost'")
    quote.result = result
    require_valid_transition("quote", quote.status, result)
    quote.status = result
    quote.result_at = datetime.now(timezone.utc)
    quote.result_reason = form.get("result_reason", "")
    db.commit()
    logger.info("Quote {} marked as {} by {}", quote.quote_number, result, user.email)
    return await quote_detail_partial(request, quote_id, user, db)


@router.post("/v2/partials/quotes/{quote_id}/revise", response_class=HTMLResponse)
async def revise_quote_htmx(
    request: Request,
    quote_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Create a new revision of the quote — returns the new quote detail."""
    quote = get_quote_for_user(db, user, quote_id)
    new_rev = (quote.revision or 1) + 1
    new_quote = Quote(
        requisition_id=quote.requisition_id,
        customer_site_id=quote.customer_site_id,
        quote_number=f"{quote.quote_number}-R{new_rev}",
        revision=new_rev,
        line_items=quote.line_items or [],
        subtotal=quote.subtotal,
        total_cost=quote.total_cost,
        total_margin_pct=quote.total_margin_pct,
        payment_terms=quote.payment_terms,
        shipping_terms=quote.shipping_terms,
        validity_days=quote.validity_days,
        notes=quote.notes,
        status=QuoteStatus.DRAFT,
        created_by_id=user.id,
    )
    db.add(new_quote)
    db.commit()
    db.refresh(new_quote)
    logger.info("Quote {} revised to rev {} as {}", quote.quote_number, new_rev, new_quote.quote_number)
    return await quote_detail_partial(request, new_quote.id, user, db)


@router.post("/v2/partials/quotes/{quote_id}/apply-markup", response_class=HTMLResponse)
async def apply_markup_htmx(
    request: Request,
    quote_id: int,
    markup_pct: float = Form(25.0),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Apply a markup percentage to all lines in the quote."""
    # Scope ownership check before mutating any lines (raises 404 for SALES on other users' quotes).
    get_quote_for_user(db, user, quote_id)
    lines = db.query(QuoteLine).filter(QuoteLine.quote_id == quote_id).all()
    for line in lines:
        if line.cost_price and float(line.cost_price) > 0:
            multiplier = 1 + (markup_pct / 100)
            line.sell_price = round(float(line.cost_price) * multiplier, 4)
            line.margin_pct = round(markup_pct / multiplier, 2)
    db.commit()
    return await quote_detail_partial(request, quote_id, user, db)


@router.post("/v2/partials/requisitions/{req_id}/add-offers-to-quote", response_class=HTMLResponse)
async def add_offers_to_draft_quote(
    request: Request,
    req_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Add selected offers to an existing draft quote.

    Returns updated quote detail.
    """
    import json as _json

    body = await request.body()
    try:
        data = _json.loads(body)
    except (ValueError, TypeError):
        raise HTTPException(400, "Invalid JSON body")

    try:
        offer_ids = [int(x) for x in data.get("offer_ids", []) if x]
        quote_id = int(data.get("quote_id", 0))
    except (ValueError, TypeError):
        raise HTTPException(400, "offer_ids must be integers and quote_id must be an integer")

    if not offer_ids or not quote_id:
        raise HTTPException(400, "Missing offer_ids or quote_id")

    quote = get_quote_for_user(db, user, quote_id)
    if quote.requisition_id != req_id:
        raise HTTPException(404, "Quote not found")
    if quote.status != QuoteStatus.DRAFT:
        raise HTTPException(400, "Can only add to draft quotes")

    offers = db.query(Offer).filter(Offer.id.in_(offer_ids), Offer.requisition_id == req_id).all()
    for o in offers:
        existing = db.query(QuoteLine).filter(QuoteLine.quote_id == quote_id, QuoteLine.offer_id == o.id).first()
        if existing:
            continue
        sell_price = float(o.unit_price or 0)
        qty = o.qty_available or 1
        line = QuoteLine(
            quote_id=quote.id,
            offer_id=o.id,
            mpn=o.mpn or "",
            manufacturer=o.manufacturer or "",
            qty=qty,
            cost_price=sell_price,
            sell_price=sell_price,
            margin_pct=0.0,
        )
        db.add(line)

    # Recalculate totals
    db.flush()
    all_lines = db.query(QuoteLine).filter(QuoteLine.quote_id == quote.id).all()
    subtotal = sum(float(ln.sell_price or 0) * (ln.qty or 1) for ln in all_lines)
    total_cost = sum(float(ln.cost_price or 0) * (ln.qty or 1) for ln in all_lines)
    quote.subtotal = subtotal
    quote.total_cost = total_cost
    quote.total_margin_pct = ((subtotal - total_cost) / subtotal * 100) if subtotal else 0
    db.commit()

    logger.info("Added {} offers to quote {} by {}", len(offers), quote.quote_number, user.email)
    return HTMLResponse('<span class="text-emerald-600 text-sm">Offers added to quote</span>')


@router.post("/v2/partials/quotes/{quote_id}/build-buy-plan", response_class=HTMLResponse)
async def build_buy_plan_htmx(
    request: Request,
    quote_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Build a buy plan from a won quote.

    Returns buy plan detail partial.
    """
    from ..services.buyplan_builder import build_buy_plan

    quote = get_quote_for_user(db, user, quote_id)
    if quote.status != QuoteStatus.WON:
        raise HTTPException(400, "Quote must be won to build a buy plan")

    try:
        plan = build_buy_plan(quote_id, db)
    except ValueError as e:
        raise HTTPException(400, str(e))

    db.add(plan)
    db.commit()
    db.refresh(plan)

    logger.info("Buy plan #{} built from quote #{} by {}", plan.id, quote_id, user.email)

    # Return buy plan detail partial
    bp_lines = db.query(BuyPlanLine).filter(BuyPlanLine.buy_plan_id == plan.id).all()
    ctx = _base_ctx(request, user, "buy-plans")
    ctx["bp"] = plan
    ctx["lines"] = bp_lines
    ctx["user"] = user
    ctx["is_ops_member"] = _is_ops_member(user, db)
    ctx["top_flag"] = summarize_top_flag(plan.ai_flags)
    return template_response("htmx/partials/buy_plans/detail.html", ctx)


# ── Prospecting partials ──────────────────────────────────────────────


# Statuses shown in the default ("All") view — dismissed/converted are hidden
# unless explicitly selected via the filter pills.
_PROSPECT_DEFAULT_STATUSES = ("suggested", "claimed")

# A background enrichment 'running' longer than this is treated as failed (its worker
# died mid-job) so the detail poller self-heals and stops instead of looping forever.
_ENRICH_STALE_SECONDS = 180


def _enrich_is_stale(started_iso) -> bool:
    """True when a 'running' enrich job started longer than _ENRICH_STALE_SECONDS
    ago."""
    if not started_iso:
        return False
    try:
        started = datetime.fromisoformat(started_iso)
    except (ValueError, TypeError):
        return False
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - started).total_seconds() > _ENRICH_STALE_SECONDS


def _prospect_toast(response, message: str, kind: str = "success") -> None:
    """Attach a showToast HX-Trigger so the Alpine $store.toast surfaces feedback."""
    response.headers["HX-Trigger"] = json.dumps({"showToast": {"message": message, "type": kind}})


def settings_toast(response, message: str, kind: str = "success") -> None:
    """Attach a showToast HX-Trigger for settings mutation responses.

    Called by settings mutation handlers to surface success/error feedback via the
    Alpine $store.toast. Mirrors _prospect_toast but is scoped to settings so later
    tasks can import it cleanly.
    """
    response.headers["HX-Trigger"] = json.dumps({"showToast": {"message": message, "type": kind}})


def _wants_detail(request: Request) -> bool:
    """True when an action came from the detail view (targets #main-content) rather than
    from an in-grid card (targets #prospect-<id>) — so we return the right partial."""
    return request.headers.get("HX-Target") == "main-content"


def _prospect_card_ctx(request: Request, user: User, prospect) -> dict:
    """Context for rendering a single prospect card (snapshot + contact summary maps,
    keyed by id so _card.html renders identically in the grid and in OOB swaps)."""
    ctx = _base_ctx(request, user, "prospecting")
    ctx["prospect"] = prospect
    ctx["snapshots"] = {prospect.id: build_priority_snapshot(prospect)}
    ctx["contact_stats_map"] = {prospect.id: contacts_summary(prospect.contacts_preview)}
    return ctx


def _prospect_detail_ctx(request: Request, user: User, prospect) -> dict:
    """Context for the detail partial — surfaces the buyer-ready snapshot, signal tags,
    contacts, and similar customers the scoring services compute."""
    ctx = _base_ctx(request, user, "prospecting")
    ctx["prospect"] = prospect
    ctx["enrichment"] = prospect.enrichment_data or {}
    ctx["warm_intro"] = (prospect.enrichment_data or {}).get("warm_intro", {})
    ctx["snapshot"] = build_priority_snapshot(prospect)
    ctx["signal_tags"] = build_signal_tags(prospect.readiness_signals)
    ctx["contacts"] = prospect.contacts_preview or []
    ctx["contact_stats"] = contacts_summary(prospect.contacts_preview)
    ctx["similar_customers"] = prospect.similar_customers or []
    # Resume the enrich poller if a background enrichment is in flight.
    ctx["enrich_state"] = "running" if (prospect.enrichment_data or {}).get("enrich_status") == "running" else None
    return ctx


def _prospect_stats_ctx(db: Session) -> dict:
    """Canonical prospecting KPIs (single definition, shared by the stats route and the
    OOB refresh after grid actions).

    "Buyer ready" = is_buyer_ready over SUGGESTED.
    """
    from ..config import settings as _settings

    suggested = db.query(ProspectAccount).filter(ProspectAccount.status == ProspectAccountStatus.SUGGESTED).all()
    claimed = (
        db.query(sqlfunc.count(ProspectAccount.id))
        .filter(ProspectAccount.status == ProspectAccountStatus.CLAIMED)
        .scalar()
        or 0
    )
    screened_out_count = (
        sum(1 for p in suggested if (p.enrichment_data or {}).get("ai_screen", {}).get("verdict") == "screened_out")
        if _settings.ai_screen_enabled
        else 0
    )
    return {
        "total": len(suggested),
        "buyer_ready": sum(1 for p in suggested if build_priority_snapshot(p)["is_buyer_ready"]),
        "call_now": sum(1 for p in suggested if (p.readiness_score or 0) >= 70),
        "claimed": claimed,
        "screened_out": screened_out_count,
    }


def _status_visible_under_filter(new_status: str, flt_status: str) -> bool:
    """Whether a card with `new_status` should remain visible under the active filter.

    Default (empty filter = "All") shows suggested + claimed; an explicit filter shows
    only that status.
    """
    if flt_status:
        return new_status == flt_status
    return new_status in _PROSPECT_DEFAULT_STATUSES


def _prospect_action_response(
    request: Request,
    user: User,
    db: Session,
    prospect,
    *,
    message: str,
    kind: str,
    flt_status: str = "",
) -> HTMLResponse:
    """Build the response for a claim/dismiss/release action.

    Detail-view actions (HX-Target=main-content) return the full refreshed detail. Grid
    actions return `_action_oob.html`: the updated card (omitted → removed when it leaves
    the active filter) plus an OOB refresh of the #prospect-stats panel.
    """
    if _wants_detail(request):
        resp = template_response("htmx/partials/prospecting/detail.html", _prospect_detail_ctx(request, user, prospect))
    else:
        ctx = _prospect_card_ctx(request, user, prospect)
        ctx["status"] = flt_status  # so the re-rendered card's buttons carry the filter forward
        ctx["include_card"] = _status_visible_under_filter(prospect.status, flt_status)
        ctx.update(_prospect_stats_ctx(db))
        resp = template_response("htmx/partials/prospecting/_action_oob.html", ctx)
    _prospect_toast(resp, message, kind)
    return resp


@router.get("/v2/partials/prospecting", response_class=HTMLResponse)
async def prospecting_list_partial(
    request: Request,
    q: str = "",
    status: str = "",
    sort: str = "ai_match_desc",
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    user: User = Depends(require_access(AccessKey.PROSPECTING)),
    db: Session = Depends(get_db),
):
    """Return the prospecting card grid as an HTML partial.

    Sorts: ai_match_desc (default) ranks by trio_match_score DESC then opportunity_score
    DESC then readiness_score DESC; buyer_ready_desc ranks by the composite buyer-ready
    score from build_priority_snapshot; fit_desc and recent_desc sort in SQL.
    Dismissed prospects are hidden unless filtered for.
    """
    base = db.query(ProspectAccount)
    if status:
        base = base.filter(ProspectAccount.status == status)
    else:
        base = base.filter(ProspectAccount.status.in_(_PROSPECT_DEFAULT_STATUSES))
    if q.strip():
        sb = SearchBuilder(q.strip())
        base = base.filter(sb.ilike_filter(ProspectAccount.name, ProspectAccount.domain))

    total = base.count()
    total_pages = max(1, (total + per_page - 1) // per_page)
    offset = (page - 1) * per_page

    if sort == "ai_match_desc":
        from ..config import settings as _settings

        rows = base.all()
        if _settings.ai_screen_enabled:
            screened_out_rows = [
                p for p in rows if (p.enrichment_data or {}).get("ai_screen", {}).get("verdict") == "screened_out"
            ]
            rows = [p for p in rows if (p.enrichment_data or {}).get("ai_screen", {}).get("verdict") != "screened_out"]
        else:
            screened_out_rows = []
        rows.sort(
            key=lambda p: (
                -(p.trio_match_score or 0),
                -(p.opportunity_score or 0),
                -(p.readiness_score or 0),
                (p.name or "").lower(),
            )
        )
        total = len(rows)
        total_pages = max(1, (total + per_page - 1) // per_page)
        prospects = rows[offset : offset + per_page]
    elif sort == "buyer_ready_desc":
        screened_out_rows = []
        # buyer_ready_score is a Python composite (no SQL column), so rank in memory.
        rows = base.all()
        snapshots = {p.id: build_priority_snapshot(p) for p in rows}
        rows.sort(
            key=lambda p: (
                -snapshots[p.id]["buyer_ready_score"],
                -(p.fit_score or 0),
                -(p.readiness_score or 0),
                (p.name or "").lower(),
            )
        )
        prospects = rows[offset : offset + per_page]
    else:
        screened_out_rows = []
        if sort == "fit_desc":
            base = base.order_by(ProspectAccount.fit_score.desc(), ProspectAccount.readiness_score.desc())
        elif sort == "recent_desc":
            base = base.order_by(ProspectAccount.created_at.desc())
        else:
            base = base.order_by(ProspectAccount.readiness_score.desc(), ProspectAccount.fit_score.desc())
        prospects = base.offset(offset).limit(per_page).all()

    snapshots = {p.id: build_priority_snapshot(p) for p in prospects}
    contact_stats_map = {p.id: contacts_summary(p.contacts_preview) for p in prospects}

    # Per-status counts for the filter pills (respect the active search, not the active
    # status filter, so each pill shows its own stable total).
    count_q = db.query(ProspectAccount.status, sqlfunc.count(ProspectAccount.id))
    if q.strip():
        sb = SearchBuilder(q.strip())
        count_q = count_q.filter(sb.ilike_filter(ProspectAccount.name, ProspectAccount.domain))
    status_counts = dict(count_q.group_by(ProspectAccount.status).all())
    all_total = sum(status_counts.get(s, 0) for s in _PROSPECT_DEFAULT_STATUSES)

    from ..config import settings as _list_settings

    ctx = _base_ctx(request, user, "prospecting")
    ctx.update(
        {
            "prospects": prospects,
            "snapshots": snapshots,
            "contact_stats_map": contact_stats_map,
            "q": q,
            "status": status,
            "sort": sort,
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
            "status_counts": status_counts,
            "all_total": all_total,
            "screened_out_prospects": screened_out_rows if sort == "ai_match_desc" else [],
            "ai_screen_enabled": _list_settings.ai_screen_enabled,
        }
    )
    return template_response("htmx/partials/prospecting/list.html", ctx)


# Sprint 8 prospecting static routes — must precede {prospect_id} catch-all
@router.get("/v2/partials/prospecting/stats", response_class=HTMLResponse)
async def prospecting_stats(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return the prospecting stats summary panel.

    "Buyer ready" uses the canonical is_buyer_ready from build_priority_snapshot — the
    same definition the list ranking uses — so the KPI never contradicts the grid.
    """
    return template_response(
        "htmx/partials/prospecting/stats.html",
        {"request": request, **_prospect_stats_ctx(db)},
    )


@router.post("/v2/partials/prospecting/add-domain", response_class=HTMLResponse)
async def add_prospect_domain(
    request: Request,
    user: User = Depends(require_buyer),
    db: Session = Depends(get_db),
):
    """Manually submit a domain to the prospect pool.

    Returns an inline result chip.
    """
    from ..services.prospect_claim import add_prospect_manually

    form = await request.form()
    domain = (form.get("domain") or "").strip()
    if not domain:
        resp = HTMLResponse(
            '<div class="bg-rose-50 border border-rose-200 rounded p-2 text-sm text-rose-700">'
            "Enter a domain (e.g. acme.com).</div>"
        )
        _prospect_toast(resp, "Enter a domain first", "error")
        return resp

    try:
        result = add_prospect_manually(domain, user.id, db)
    except (ValueError, RuntimeError) as exc:
        logger.warning("Manual prospect add failed for {!r}: {}", domain, exc)
        resp = HTMLResponse(
            '<div class="bg-rose-50 border border-rose-200 rounded p-2 text-sm text-rose-700">'
            f"Could not add {html_mod.escape(domain)}.</div>"
        )
        _prospect_toast(resp, "Could not add prospect", "error")
        return resp

    # Service returns a dict ({prospect_id, name, domain, status, is_new}), not an ORM row.
    pid = result["prospect_id"]
    name = html_mod.escape(result.get("name") or domain)
    verb = "Added" if result.get("is_new") else "Already in pool"
    resp = template_response(
        "htmx/partials/prospecting/add_result.html",
        {"request": request, "pid": pid, "name": name, "verb": verb, "is_new": result.get("is_new")},
    )
    _prospect_toast(resp, f"{verb}: {result.get('name') or domain}", "success" if result.get("is_new") else "info")
    return resp


@router.get("/v2/partials/prospecting/{prospect_id}", response_class=HTMLResponse)
async def prospecting_detail_partial(
    request: Request,
    prospect_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return prospect detail as HTML partial."""
    prospect = (
        db.query(ProspectAccount)
        .options(
            joinedload(ProspectAccount.claimed_by_user),
        )
        .filter(ProspectAccount.id == prospect_id)
        .first()
    )
    if not prospect:
        raise HTTPException(404, "Prospect not found")
    return template_response("htmx/partials/prospecting/detail.html", _prospect_detail_ctx(request, user, prospect))


@router.post("/v2/partials/prospecting/{prospect_id}/claim", response_class=HTMLResponse)
async def claim_prospect_htmx(
    request: Request,
    prospect_id: int,
    user: User = Depends(require_buyer),
    db: Session = Depends(get_db),
):
    """Claim a prospect.

    Enforces the site cap + ownership (in the service) and triggers background deep
    enrichment. Returns the refreshed detail or card per the call site.
    """
    from ..services.prospect_claim import claim_prospect, trigger_deep_enrichment_bg
    from ..utils.async_helpers import safe_background_task

    error = None
    try:
        claim_prospect(prospect_id, user.id, db)
    except LookupError:
        raise HTTPException(404, "Prospect not found")
    except ValueError as e:
        error = str(e)

    if not error:
        await safe_background_task(trigger_deep_enrichment_bg(prospect_id), task_name="deep_enrichment_prospect")

    prospect = (
        db.query(ProspectAccount)
        .options(joinedload(ProspectAccount.claimed_by_user))
        .filter(ProspectAccount.id == prospect_id)
        .first()
    )
    if not prospect:
        raise HTTPException(404, "Prospect not found")

    form = await request.form()
    return _prospect_action_response(
        request,
        user,
        db,
        prospect,
        message=error or f"Claimed {prospect.name}",
        kind="error" if error else "success",
        flt_status=form.get("flt_status", ""),
    )


@router.post("/v2/partials/prospecting/{prospect_id}/dismiss", response_class=HTMLResponse)
async def dismiss_prospect_htmx(
    request: Request,
    prospect_id: int,
    user: User = Depends(require_buyer),
    db: Session = Depends(get_db),
):
    """Dismiss a SUGGESTED prospect (claimed prospects use the Release action instead).

    Returns the refreshed detail or card per the call site.
    """
    prospect = db.get(ProspectAccount, prospect_id)
    if not prospect:
        raise HTTPException(404, "Prospect not found")

    form = await request.form()
    flt_status = form.get("flt_status", "")
    error = None
    if prospect.status != ProspectAccountStatus.SUGGESTED:
        error = "Only suggested prospects can be dismissed."
    else:
        reason = (form.get("reason") or "other").strip()[:255]  # dismiss_reason is String(255)
        prospect.status = ProspectAccountStatus.DISMISSED
        prospect.dismissed_by = user.id
        prospect.dismissed_at = datetime.now(timezone.utc)
        prospect.dismiss_reason = reason
        db.commit()

    return _prospect_action_response(
        request,
        user,
        db,
        prospect,
        message=error or f"Dismissed {prospect.name}",
        kind="error" if error else "success",
        flt_status=flt_status,
    )


@router.post("/v2/partials/prospecting/{prospect_id}/release", response_class=HTMLResponse)
async def release_prospect_htmx(
    request: Request,
    prospect_id: int,
    user: User = Depends(require_buyer),
    db: Session = Depends(get_db),
):
    """Release a claimed prospect back to the pool: status -> SUGGESTED, clear the claim,
    and relinquish Company ownership. Only the claimer or an admin may release."""
    from ..services.prospect_claim import release_prospect

    error = None
    try:
        release_prospect(prospect_id, user.id, db, is_admin=(user.role == UserRole.ADMIN))
    except LookupError:
        raise HTTPException(404, "Prospect not found")
    except ValueError as e:
        error = str(e)

    prospect = (
        db.query(ProspectAccount)
        .options(joinedload(ProspectAccount.claimed_by_user))
        .filter(ProspectAccount.id == prospect_id)
        .first()
    )
    if not prospect:
        raise HTTPException(404, "Prospect not found")

    form = await request.form()
    return _prospect_action_response(
        request,
        user,
        db,
        prospect,
        message=error or f"Released {prospect.name} back to the pool",
        kind="error" if error else "success",
        flt_status=form.get("flt_status", ""),
    )


@router.post("/v2/partials/prospecting/{prospect_id}/enrich", response_class=HTMLResponse)
async def enrich_prospect_htmx(
    request: Request,
    prospect_id: int,
    user: User = Depends(require_buyer),
    db: Session = Depends(get_db),
):
    """Kick off enrichment in the BACKGROUND and return a status poller.

    The SAM.gov/news/warm-intro work runs off the request path (run_enrichment_job via
    safe_background_task); the detail page polls /enrich-status until it lands.
    """
    prospect = db.get(ProspectAccount, prospect_id)
    if not prospect:
        raise HTTPException(404, "Prospect not found")

    ed = dict(prospect.enrichment_data or {})
    if ed.get("enrich_status") != "running":
        from ..services.prospect_free_enrichment import run_enrichment_job
        from ..utils.async_helpers import safe_background_task

        ed["enrich_status"] = "running"
        ed["enrich_started_at"] = datetime.now(timezone.utc).isoformat()
        prospect.enrichment_data = ed
        db.commit()
        await safe_background_task(run_enrichment_job(prospect_id), task_name="prospect_enrichment")

    return template_response(
        "htmx/partials/prospecting/enrich_status.html",
        {"request": request, "prospect": prospect, "enrich_state": "running"},
    )


@router.get("/v2/partials/prospecting/{prospect_id}/enrich-status", response_class=HTMLResponse)
async def enrich_status_partial(
    request: Request,
    prospect_id: int,
    user: User = Depends(require_buyer),
    db: Session = Depends(get_db),
):
    """Poll endpoint for background enrichment.

    HTTP 200 while running (htmx keeps polling); HTTP 286 when done/error (htmx swaps
    the final fragment and STOPS).
    """
    prospect = db.get(ProspectAccount, prospect_id)
    if not prospect:
        # Stop the poll rather than 404 — htmx won't cancel an `every 2s` poll on a 4xx.
        return HTMLResponse("", status_code=286)

    ed = prospect.enrichment_data or {}
    state = ed.get("enrich_status") or "done"
    if state == "running" and _enrich_is_stale(ed.get("enrich_started_at")):
        state = "error"  # worker died mid-job — stop the poll
    resp = template_response(
        "htmx/partials/prospecting/enrich_status.html",
        {"request": request, "prospect": prospect, "enrich_state": state},
    )
    if state != "running":
        resp.status_code = 286  # htmx stop-polling status — the final fragment still swaps
        if state == "error":
            _prospect_toast(resp, "Enrichment failed — try again", "warning")
        else:
            _prospect_toast(resp, f"Enriched {prospect.name}", "success")
    return resp


# ── Reclaim endpoint ─────────────────────────────────────────────────


@router.post("/v2/partials/prospects/{prospect_id}/reclaim", response_class=HTMLResponse)
async def reclaim_prospect_htmx(
    request: Request,
    prospect_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Reclaim a swept prospect: re-assign Company owner, dismiss from pool.

    Former owner, admin, or sweep manager only.
    Returns refreshed prospect card or detail with showToast trigger.
    """
    from ..services.prospect_reclamation import reclaim_prospect_account

    error = None
    result = None
    try:
        result = reclaim_prospect_account(
            prospect_id,
            user.id,
            db,
            is_admin=(user.role == UserRole.ADMIN),
        )
    except LookupError:
        raise HTTPException(404, "Prospect not found")
    except RuntimeError:
        raise HTTPException(500, "Session user record not found")
    except ValueError as e:
        error = str(e)

    prospect = db.get(ProspectAccount, prospect_id)
    if not prospect:
        raise HTTPException(404, "Prospect not found")

    form = await request.form()
    flt_status = form.get("flt_status", "")
    msg = error or f"Reclaimed {result['company_name']} — account re-assigned to you"
    return _prospect_action_response(
        request,
        user,
        db,
        prospect,
        message=msg,
        kind="error" if error else "success",
        flt_status=flt_status,
    )


@router.post("/v2/partials/prospects/{prospect_id}/reassign", response_class=HTMLResponse)
async def reassign_prospect_htmx(
    request: Request,
    prospect_id: int,
    to_user_id: int = Form(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Manager/admin reassigns a swept prospect's company to another owner.

    Overrides the Phase 4 reclaim cooldown: dismisses the swept prospect, sets the new
    owner, and clears the cooldown. Manager/admin only. Returns the refreshed prospect
    card/detail with a showToast trigger.
    """
    if not is_manager_or_admin(user):
        raise HTTPException(403, "Only a manager or admin can reassign an account")

    from ..services.prospect_reclamation import reassign_account

    prospect = db.get(ProspectAccount, prospect_id)
    if not prospect:
        raise HTTPException(404, "Prospect not found")
    if not prospect.company_id:
        raise HTTPException(400, "Prospect is not linked to a company; nothing to reassign")

    error = None
    result = None
    try:
        result = reassign_account(prospect.company_id, to_user_id, user, db)
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except LookupError:
        raise HTTPException(404, "Company not found")
    except ValueError as e:
        error = str(e)

    prospect = db.get(ProspectAccount, prospect_id)
    if not prospect:
        raise HTTPException(404, "Prospect not found")

    form = await request.form()
    flt_status = form.get("flt_status", "")
    msg = error or f"Reassigned {result['company_name']}"
    return _prospect_action_response(
        request,
        user,
        db,
        prospect,
        message=msg,
        kind="error" if error else "success",
        flt_status=flt_status,
    )


# ── Settings partials ────────────────────────────────────────────────


@router.get("/v2/partials/settings", response_class=HTMLResponse)
async def settings_partial(
    request: Request,
    tab: str = "connectors",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Settings page — renders index with active tab."""
    ctx = _base_ctx(request, user, "settings")
    ctx["active_tab"] = tab
    ctx["is_admin"] = user.role == UserRole.ADMIN
    # Supervisor-tier flag — gates the Activity Scorecard tab (manager + admin).
    ctx["is_manager"] = is_manager_or_admin(user)
    return template_response("htmx/partials/settings/index.html", ctx)


@router.get("/v2/partials/settings/sources", response_class=HTMLResponse)
async def settings_sources_tab(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Sources tab — redirects to unified Connectors tab."""
    from fastapi.responses import RedirectResponse

    return RedirectResponse("/v2/partials/settings/connectors", status_code=302)


@router.get("/v2/partials/settings/system", response_class=HTMLResponse)
async def settings_system_tab(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """System config tab — admin only.

    Renders the curated typed controls (3 toggles + 1 number input) for the four user-
    facing flags. Effective values come from the Task-10 resolver (DB row wins, else the
    env-backed default) so each control reflects reality. Internal watermark keys are
    surfaced read-only in a collapsed "Job state" disclosure, never as editable
    controls.
    """
    if user.role != UserRole.ADMIN:
        raise HTTPException(403, "Admin only")
    from ..config import settings as app_settings
    from ..routers.admin.system import SYSTEM_JOB_STATE_KEYS, SYSTEM_SETTINGS_META
    from ..services.admin_service import (
        get_all_config,
        get_effective_flag,
        get_effective_int,
    )

    # Resolve each curated setting's effective value, threading the env default so a
    # missing DB row falls back to the same value the background jobs read today.
    env_defaults = {
        "email_mining_enabled": app_settings.email_mining_enabled,
        "proactive_matching_enabled": app_settings.proactive_matching_enabled,
        "activity_tracking_enabled": app_settings.activity_tracking_enabled,
        "inbox_scan_interval_min": app_settings.inbox_scan_interval_min,
    }
    settings_view = []
    for key, meta in SYSTEM_SETTINGS_META.items():
        if meta["type"] == "bool":
            value = get_effective_flag(db, key, env_defaults[key])
        else:
            value = get_effective_int(db, key, env_defaults[key])
        settings_view.append({"key": key, "value": value, **meta})

    # Read-only job-state watermark rows (collapsed disclosure).
    all_config = get_all_config(db)
    job_state = [row for row in all_config if row["key"] in SYSTEM_JOB_STATE_KEYS]

    ctx = _base_ctx(request, user, "settings")
    ctx["system_settings"] = settings_view
    ctx["job_state"] = job_state
    return template_response("htmx/partials/settings/system.html", ctx)


@router.get("/v2/partials/settings/profile", response_class=HTMLResponse)
async def settings_profile_tab(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """User profile tab."""
    from ..services.activity_service import get_inbox_sync_status

    ctx = _base_ctx(request, user, "settings")
    ctx["profile_user"] = user
    ctx["inbox_status"] = get_inbox_sync_status(db, user)
    return template_response("htmx/partials/settings/profile.html", ctx)


async def _run_inbox_scan_now(user: User, db: Session) -> None:
    """Run a real on-demand inbox scan for the current user, unless under TESTING."""
    if os.getenv("TESTING") == "1":
        return  # hermetic tests: do not touch Graph
    from ..jobs.email_jobs import _scan_user_inbox

    try:
        # stay under the HTMX client timeout (app/static/htmx_app.js); scan is idempotent + scheduler-backed
        await asyncio.wait_for(_scan_user_inbox(user, db), timeout=12)
    except asyncio.TimeoutError:
        logger.warning("Manual inbox scan timed out for {}", user.email)


@router.post("/v2/partials/settings/inbox/scan-now", response_class=HTMLResponse)
async def settings_scan_now(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Manual inbox scan from the Settings mailbox-sync card."""
    from ..services.activity_service import get_inbox_sync_status

    await _run_inbox_scan_now(user, db)
    db.refresh(user)
    ctx = _base_ctx(request, user, "settings")
    ctx["inbox_status"] = get_inbox_sync_status(db, user)
    return template_response("htmx/partials/settings/_mailbox_sync_card.html", ctx)


@router.post("/api/user/toggle-8x8", response_class=HTMLResponse)
async def toggle_8x8(
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Toggle 8x8 click-to-call preference for the current user."""
    user.eight_by_eight_enabled = not user.eight_by_eight_enabled
    db.commit()
    state = "enabled" if user.eight_by_eight_enabled else "disabled"
    logger.info("8x8 click-to-call toggled", user_id=user.id, enabled=user.eight_by_eight_enabled)
    return HTMLResponse(
        status_code=200,
        headers={"HX-Trigger": '{"showToast": "8x8 click-to-call ' + state + '"}'},
    )


@router.post("/api/user/profile", response_class=HTMLResponse)
async def update_user_profile(
    request: Request,
    name: str = Form(""),
    extension: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Update the current user's display name and 8x8 extension.

    Validates name (non-empty, ≤255 chars) and extension (≤20 chars). Returns 400 JSON
    on bad input; on success commits and emits a showToast trigger.
    """
    from fastapi.responses import JSONResponse

    name = name.strip()
    extension = extension.strip()

    if not name or len(name) > 255:
        req_id = getattr(request.state, "request_id", "unknown")
        return JSONResponse(
            status_code=400,
            content={"error": "Name is required.", "status_code": 400, "request_id": req_id},
        )
    if len(extension) > 20:
        req_id = getattr(request.state, "request_id", "unknown")
        return JSONResponse(
            status_code=400,
            content={"error": "Extension must be 20 characters or fewer.", "status_code": 400, "request_id": req_id},
        )

    user.name = name
    user.eight_by_eight_extension = extension
    db.commit()
    logger.info("Profile updated", user_id=user.id)
    response = HTMLResponse(status_code=200)
    settings_toast(response, "Profile updated.")
    return response


@router.post("/api/user/toggle-buyplan-email", response_class=HTMLResponse)
async def toggle_buyplan_email(
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Toggle buy-plan email notifications for the current user."""
    user.notify_buyplan_email_enabled = not user.notify_buyplan_email_enabled
    db.commit()
    state = "enabled" if user.notify_buyplan_email_enabled else "disabled"
    logger.info("Buy-plan email notifications toggled", user_id=user.id, enabled=user.notify_buyplan_email_enabled)
    response = HTMLResponse(status_code=200)
    settings_toast(response, f"Buy-plan email notifications {state}.")
    return response


@router.post("/api/user/toggle-new-offer-alert", response_class=HTMLResponse)
async def toggle_new_offer_alert(
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Toggle new-offer alerts for the current user."""
    user.notify_new_offer_alert_enabled = not user.notify_new_offer_alert_enabled
    db.commit()
    state = "enabled" if user.notify_new_offer_alert_enabled else "disabled"
    logger.info("New-offer alerts toggled", user_id=user.id, enabled=user.notify_new_offer_alert_enabled)
    response = HTMLResponse(status_code=200)
    settings_toast(response, f"New-offer alerts {state}.")
    return response


@router.post("/api/user/toggle-resource-alert", response_class=HTMLResponse)
async def toggle_resource_alert(
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Toggle urgent re-source backfill alerts (email + Teams DM) for the current
    user."""
    user.notify_resource_alert_enabled = not user.notify_resource_alert_enabled
    db.commit()
    state = "enabled" if user.notify_resource_alert_enabled else "disabled"
    logger.info("Re-source alerts toggled", user_id=user.id, enabled=user.notify_resource_alert_enabled)
    response = HTMLResponse(status_code=200)
    settings_toast(response, f"Re-source alerts {state}.")
    return response


def _render_data_ops(request: Request, user: User, db: Session):
    """Render the Data Ops tab partial — vendor/company dedup suggestions.

    Each scan is guarded independently. A scan that RAISES sets a per-scan
    ``*_scan_failed`` flag so the template can render a distinct error block instead
    of swallowing the failure into the reassuring "no duplicates found" empty state
    (a crashed scan must never look like a clean dataset). Reused by the merge
    endpoints so a successful merge re-renders the surrounding list and stale pairs
    drop without a manual refresh.
    """
    vendor_dupes: list = []
    company_dupes: list = []
    vendor_scan_failed = False
    company_scan_failed = False
    try:
        from ..vendor_utils import find_vendor_dedup_candidates

        vendor_dupes = find_vendor_dedup_candidates(db, threshold=85, limit=30)
    except Exception as e:
        vendor_scan_failed = True
        logger.warning(f"Vendor dedup scan failed: {e}")
    try:
        from ..company_utils import find_company_dedup_candidates

        company_dupes = find_company_dedup_candidates(db, threshold=85, limit=30)
    except Exception as e:
        company_scan_failed = True
        logger.warning(f"Company dedup scan failed: {e}")

    ctx = _base_ctx(request, user, "settings")
    ctx["vendor_dupes"] = vendor_dupes
    ctx["company_dupes"] = company_dupes
    ctx["vendor_scan_failed"] = vendor_scan_failed
    ctx["company_scan_failed"] = company_scan_failed
    return template_response("htmx/partials/settings/data_ops.html", ctx)


@router.get("/v2/partials/settings/data-ops", response_class=HTMLResponse)
async def settings_data_ops_tab(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Admin data operations tab — vendor/company dedup suggestions."""
    from ..dependencies import is_admin

    if not is_admin(user):
        raise HTTPException(403, "Admin only")

    return _render_data_ops(request, user, db)


@router.get("/v2/partials/settings/api-keys", response_class=HTMLResponse)
async def settings_api_keys_tab(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """API keys tab — redirects to unified Connectors tab."""
    from fastapi.responses import RedirectResponse

    return RedirectResponse("/v2/partials/settings/connectors", status_code=302)


# Retired data providers — excluded from the connectors tab and the Test-all sweep.
# Single source of truth referenced by both _build_connector_groups and connectors_test_all.
_DEAD_CONNECTORS = frozenset({"rocketreach_enrichment", "clearbit_enrichment"})


def _build_connector_field(db, source_name: str, env_var: str) -> dict:
    """Return {is_set, masked} for one env-var credential field."""
    from ..services.credential_service import credential_is_set, get_credential, mask_value

    is_set = credential_is_set(db, source_name, env_var)
    masked = ""
    if is_set:
        plain = get_credential(db, source_name, env_var)
        masked = mask_value(plain) if plain else "••••••••"
    return {"is_set": is_set, "masked": masked}


def _worker_status_row(source_name: str, db):
    """Return the worker-status singleton for a worker-backed source (or None).

    Maps an ApiSource.name (thebrokersite/netcomponents/icsource) to its heartbeat model
    via connector_service.WORKER_BACKED_SOURCES, reading the id=1 singleton.
    """
    from ..models import IcsWorkerStatus, NcWorkerStatus, TbfWorkerStatus
    from ..services import connector_service

    worker_key = connector_service.WORKER_BACKED_SOURCES.get(source_name)
    model = {"tbf": TbfWorkerStatus, "nc": NcWorkerStatus, "ics": IcsWorkerStatus}.get(worker_key)
    if model is None:
        return None
    return db.get(model, 1)


def _enrich_source(source, db) -> dict:
    """Build the per-source context dict for the connectors tab."""
    from ..services import connector_service

    name = source.name
    ct = connector_service.control_type(source)
    keyless = connector_service.is_keyless(source)

    # Credential fields
    env_vars = source.env_vars or []
    creds = {ev: _build_connector_field(db, name, ev) for ev in env_vars}
    credential_set = any(c["is_set"] for c in creds.values())

    # Clay OAuth state
    if name == "clay_enrichment":
        oauth_connected = clay_oauth.is_connected()
        needs_reconnect = clay_oauth.needs_reconnect()
    else:
        oauth_connected = False
        needs_reconnect = False

    # Worker-backed sources: derive status from the worker heartbeat, not a direct API.
    worker = None
    if connector_service.is_worker_backed(source):
        worker = connector_service.worker_health(_worker_status_row(name, db))

    state = connector_service.connector_state(
        source,
        credential_set=credential_set,
        oauth_connected=oauth_connected,
        needs_reconnect=needs_reconnect,
        keyless=keyless,
        worker=worker,
    )

    # Keyless note
    if ct == "keyless":
        if name == "ai_live_web":
            keyless_note = "No key required — uses your Anthropic key."
        else:
            keyless_note = "No key required — switch it on to use it."
    else:
        keyless_note = ""

    # Testability:
    #  - planned: never (no implementation yet)
    #  - worker-backed: never via the API-probe Test button — health is the heartbeat,
    #    not a synchronous search (the worker runs out-of-process on a schedule)
    #  - else: has some form of access
    if ct == "planned" or worker is not None:
        testable = False
    else:
        testable = bool(credential_set or oauth_connected or keyless)

    return {
        "id": source.id,
        "name": name,
        "display_name": source.display_name or name,
        "description": source.description or "",
        "is_active": source.is_active,
        "state": state,
        "control_type": ct,
        "env_vars": env_vars,
        "creds": creds,
        "oauth_connected": oauth_connected,
        "needs_reconnect": needs_reconnect,
        "status": source.status or "pending",
        "last_error": source.last_error or "",
        "last_success": source.last_success,
        "error_count_24h": getattr(source, "error_count_24h", 0) or 0,
        "keyless_note": keyless_note,
        "testable": testable,
        # Worker-backed health (None for direct-API/keyless/oauth sources).
        "worker": worker,
    }


def _build_connector_groups(db, request) -> list[dict]:
    """Return connector_groups list-of-group-dicts for the connectors tab context.

    Each group: {key, label, sources: [enriched source dict]}.
    Sources are bucketed by connector_service.connector_group, emitted in GROUP_ORDER,
    empty groups are dropped. Dead providers (rocketreach, clearbit) are excluded.
    """
    from ..services import connector_service

    sources = db.query(ApiSource).order_by(ApiSource.display_name).all()

    buckets: dict[str, list[dict]] = {key: [] for key, _ in connector_service.GROUP_ORDER}

    for src in sources:
        if src.name in _DEAD_CONNECTORS:
            continue
        group_key = connector_service.connector_group(src)
        if group_key not in buckets:
            group_key = "part_sourcing"
        buckets[group_key].append(_enrich_source(src, db))

    groups = []
    for key, label in connector_service.GROUP_ORDER:
        members = buckets.get(key, [])
        if members:
            groups.append({"key": key, "label": label, "sources": members})

    return groups


@router.get("/v2/partials/settings/connectors", response_class=HTMLResponse)
async def settings_connectors_tab(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Unified Connectors tab — admin only.

    Replaces sources + api-keys tabs.
    """
    if user.role != UserRole.ADMIN:
        raise HTTPException(403, "Admin only")

    ctx = _base_ctx(request, user, "settings")
    ctx["connector_groups"] = _build_connector_groups(db, request)
    return template_response("htmx/partials/settings/connectors.html", ctx)


@router.get("/v2/partials/settings/connector-card/{source_id}", response_class=HTMLResponse)
async def connector_card_partial(
    source_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Single connector card partial — used as the swap unit for toggle/test/save.

    Returns the rendered card macro for one source, or 404 if not found.
    """
    if user.role != UserRole.ADMIN:
        raise HTTPException(403, "Admin only")

    source = db.query(ApiSource).filter(ApiSource.id == source_id).first()
    if not source:
        raise HTTPException(404, f"Connector {source_id!r} not found")

    enriched = _enrich_source(source, db)
    ctx = _base_ctx(request, user, "settings")
    ctx["s"] = enriched
    return template_response("htmx/partials/settings/_connector_card_partial.html", ctx)


@router.post("/v2/partials/settings/connectors/test-all", response_class=HTMLResponse)
async def connectors_test_all(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Run Test for every credentialed + active source, sequentially (don't hammer
    provider APIs), and return an OOB bundle of refreshed cards.

    Sources without credentials / inactive are skipped. Per-source failures are
    tolerated (recorded as Error) and never abort the sweep.
    """
    if user.role != UserRole.ADMIN:
        raise HTTPException(403, "Admin only")

    from ..routers.sources import run_source_test

    sources = db.query(ApiSource).order_by(ApiSource.display_name).all()

    tested: list[dict] = []
    for src in sources:
        if src.name in _DEAD_CONNECTORS or not src.is_active:
            continue
        enriched = _enrich_source(src, db)
        if not enriched["testable"]:
            continue
        try:
            await run_source_test(src, db)
        except Exception as e:  # defensive — run_source_test already swallows
            logger.warning("Test-all probe failed for {}: {}", src.name, e)
        tested.append(_enrich_source(src, db))

    failed = sum(1 for s in tested if s["state"] == "error")
    ctx = _base_ctx(request, user, "settings")
    ctx["tested_sources"] = tested
    ctx["tested_count"] = len(tested)
    ctx["failed_count"] = failed
    return template_response("htmx/partials/settings/_connectors_testall.html", ctx)


@router.post("/v2/partials/admin/vendor-merge", response_class=HTMLResponse)
async def admin_vendor_merge(
    request: Request,
    keep_id: int = Form(...),
    remove_id: int = Form(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Merge two vendor cards via HTMX."""
    from ..dependencies import is_admin

    if not is_admin(user):
        raise HTTPException(403, "Admin only")

    from ..services.vendor_merge_service import merge_vendor_cards as _merge

    try:
        result = _merge(keep_id, remove_id, db)
        db.commit()
    except Exception as e:
        # Align with company-merge: the service raises ValueError on validation, but an
        # unexpected SQLAlchemy error here must surface as a toast, not a 500.
        db.rollback()
        message, kind = f"Vendor merge failed: {e}", "error"
    else:
        kept = db.get(VendorCard, result.get("kept", keep_id))
        kept_name = kept.display_name if kept and kept.display_name else "vendor"
        message = f"Merged into {kept_name}. {result.get('reassigned', 0)} records reassigned."
        kind = "success"

    resp = _render_data_ops(request, user, db)
    settings_toast(resp, message, kind=kind)
    return resp


@router.post("/v2/partials/admin/company-merge", response_class=HTMLResponse)
async def admin_company_merge(
    request: Request,
    keep_id: int = Form(...),
    remove_id: int = Form(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Merge two companies via HTMX."""
    from ..dependencies import is_admin

    if not is_admin(user):
        raise HTTPException(403, "Admin only")

    from ..services.company_merge_service import merge_companies

    try:
        result = merge_companies(keep_id, remove_id, db)
        db.commit()
    except Exception as e:
        db.rollback()
        message, kind = f"Company merge failed: {e}", "error"
    else:
        kept = db.get(Company, result.get("kept", keep_id))
        kept_name = kept.name if kept and kept.name else "company"
        message, kind = f"Merged into {kept_name}.", "success"

    resp = _render_data_ops(request, user, db)
    settings_toast(resp, message, kind=kind)
    return resp


@router.post("/v2/partials/admin/vendor-delete-both", response_class=HTMLResponse)
async def admin_vendor_delete_both(
    request: Request,
    id_a: int = Form(...),
    id_b: int = Form(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Delete BOTH vendor cards in a dedup pair (neither is worth keeping)."""
    from ..dependencies import is_admin

    if not is_admin(user):
        raise HTTPException(403, "Admin only")

    from ..services.vendor_merge_service import delete_vendor_cards

    try:
        result = delete_vendor_cards(id_a, id_b, db)
        db.commit()
    except Exception as e:
        db.rollback()
        message, kind = f"Vendor delete failed: {e}", "error"
    else:
        message = f"Deleted both vendors. {result.get('detached', 0)} records detached."
        kind = "success"

    resp = _render_data_ops(request, user, db)
    settings_toast(resp, message, kind=kind)
    return resp


@router.post("/v2/partials/admin/company-delete-both", response_class=HTMLResponse)
async def admin_company_delete_both(
    request: Request,
    id_a: int = Form(...),
    id_b: int = Form(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Delete BOTH companies in a dedup pair (neither is worth keeping)."""
    from ..dependencies import is_admin

    if not is_admin(user):
        raise HTTPException(403, "Admin only")

    from ..services.company_merge_service import delete_companies

    try:
        result = delete_companies(id_a, id_b, db)
        db.commit()
    except Exception as e:
        db.rollback()
        message, kind = f"Company delete failed: {e}", "error"
    else:
        message = f"Deleted both companies. {result.get('detached', 0)} records detached."
        kind = "success"

    resp = _render_data_ops(request, user, db)
    settings_toast(resp, message, kind=kind)
    return resp


# Mass dedup actions accept a comma-joined "pairs" token list where each token is
# "<id_a>-<id_b>" (the two ids of one candidate pair). Mirrors the requisitions2 /
# customers bulk convention (one hidden field, server-side parse + per-item gate),
# but the dedup unit is a PAIR, not a single row, so the token carries both ids.
_MAX_DEDUP_PAIRS = 200


def _parse_dedup_pairs(raw: str) -> list[tuple[int, int]]:
    """Parse a "a-b,c-d" pair-token string into [(a, b), ...]; skip malformed tokens."""
    pairs: list[tuple[int, int]] = []
    for tok in (raw or "").split(","):
        tok = tok.strip()
        if not tok:
            continue
        a, _, b = tok.partition("-")
        if a.lstrip("-").isdigit() and b.lstrip("-").isdigit():
            pairs.append((int(a), int(b)))
    return pairs


async def _dedup_bulk(request, user, db, entity: str) -> HTMLResponse:
    """Shared body for vendor/company bulk dedup actions (merge | delete | dismiss).

    ``merge`` keeps the FIRST id of each pair (the template emits keeper-first tokens);
    ``delete`` removes both; ``dismiss`` is a view-only clear (no durable state yet — the
    rows just drop from this render and reappear on the next scan). Per-pair failures don't
    abort the batch, but each is logged at error level and the failing pair tokens are
    surfaced in the toast — any failure makes the toast an ``error`` (never green success).
    """
    from ..dependencies import is_admin

    if not is_admin(user):
        raise HTTPException(403, "Admin only")

    form = await request.form()
    action = (form.get("action") or "").strip()
    if action not in {"merge", "delete", "dismiss"}:
        raise HTTPException(400, f"Invalid action {action!r}")

    pairs = _parse_dedup_pairs(form.get("pairs", ""))
    if len(pairs) > _MAX_DEDUP_PAIRS:
        raise HTTPException(400, f"Maximum {_MAX_DEDUP_PAIRS} pairs per bulk action")

    if not pairs or action == "dismiss":
        # Dismiss is purely client-side (the row was already hidden); just re-render.
        resp = _render_data_ops(request, user, db)
        if pairs:
            settings_toast(resp, f"Dismissed {len(pairs)} pair(s) for now.", kind="success")
        return resp

    if entity == "vendor":
        from ..services.vendor_merge_service import delete_vendor_cards, merge_vendor_cards

        merge_fn, delete_fn, noun = merge_vendor_cards, delete_vendor_cards, "vendor"
    else:
        from ..services.company_merge_service import delete_companies, merge_companies

        merge_fn, delete_fn, noun = merge_companies, delete_companies, "company"

    done = 0
    failed_tokens: list[str] = []
    for a, b in pairs:
        try:
            if action == "merge":
                merge_fn(a, b, db)
            else:
                delete_fn(a, b, db)
            db.commit()
            done += 1
        except Exception as e:
            db.rollback()
            failed_tokens.append(f"{a}-{b}")
            logger.error("Bulk {} {}: pair {}-{} failed: {}", noun, action, a, b, e)

    verb = "Merged" if action == "merge" else "Deleted"
    failed = len(failed_tokens)
    message = f"{verb} {done} {noun} pair(s)."
    if failed:
        message += f" {failed} failed: {', '.join(failed_tokens)}."
    resp = _render_data_ops(request, user, db)
    # Any failure surfaces as an error toast — a partial failure must not look green.
    settings_toast(resp, message, kind="error" if failed else "success")
    return resp


@router.post("/v2/partials/admin/vendor-bulk", response_class=HTMLResponse)
async def admin_vendor_bulk(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Bulk merge/delete/dismiss selected vendor dedup pairs."""
    return await _dedup_bulk(request, user, db, "vendor")


@router.post("/v2/partials/admin/company-bulk", response_class=HTMLResponse)
async def admin_company_bulk(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Bulk merge/delete/dismiss selected company dedup pairs."""
    return await _dedup_bulk(request, user, db, "company")


# ── Proactive Part Match ─────────────────────────────────────────────


@router.get("/v2/partials/proactive", response_class=HTMLResponse)
async def proactive_list_partial(
    request: Request,
    tab: str = "matches",
    user: User = Depends(require_access(AccessKey.PROACTIVE)),
    db: Session = Depends(get_db),
):
    """Proactive matches list partial — shows matches and sent offers."""
    from ..services.proactive_service import get_matches_for_user, get_sent_offers

    result = get_matches_for_user(db, user.id, status=ProactiveMatchStatus.NEW)
    groups = result.get("groups", []) if isinstance(result, dict) else result
    match_count = result.get("stats", {}).get("total", 0) if isinstance(result, dict) else 0
    sent = get_sent_offers(db, user.id) if tab == "sent" else []

    ctx = _base_ctx(request, user, "proactive")
    ctx["matches"] = groups
    ctx["sent"] = sent
    ctx["tab"] = tab
    ctx["match_count"] = match_count
    ctx["success_msg"] = request.query_params.get("success_msg", "")
    return template_response("htmx/partials/proactive/list.html", ctx)


@router.post("/v2/partials/proactive/refresh", response_class=HTMLResponse)
async def proactive_refresh(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Trigger a proactive scan then return the matches list partial."""
    from ..services.proactive_matching import run_proactive_scan
    from ..services.proactive_service import get_matches_for_user

    await asyncio.to_thread(run_proactive_scan, db)

    result = get_matches_for_user(db, user.id, status=ProactiveMatchStatus.NEW)
    groups = result.get("groups", []) if isinstance(result, dict) else result
    match_count = result.get("stats", {}).get("total", 0) if isinstance(result, dict) else 0

    ctx = _base_ctx(request, user, "proactive")
    ctx["matches"] = groups
    ctx["sent"] = []
    ctx["tab"] = "matches"
    ctx["match_count"] = match_count
    ctx["success_msg"] = ""
    return template_response("htmx/partials/proactive/list.html", ctx)


@router.post("/v2/partials/proactive/batch-dismiss", response_class=HTMLResponse)
async def proactive_batch_dismiss(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Batch dismiss selected proactive matches and reload the list."""
    from ..models import ProactiveMatch

    form = await request.form()
    match_ids_raw = form.getlist("match_ids")
    match_ids = [int(mid) for mid in match_ids_raw if mid and str(mid).isdigit()]

    if match_ids:
        db.query(ProactiveMatch).filter(
            ProactiveMatch.id.in_(match_ids),
            ProactiveMatch.salesperson_id == user.id,
            ProactiveMatch.status == ProactiveMatchStatus.NEW,
        ).update(
            {"status": ProactiveMatchStatus.DISMISSED, "dismiss_reason": "batch_dismiss"}, synchronize_session=False
        )
        db.commit()

    # Re-render list
    from ..services.proactive_service import get_matches_for_user

    result = get_matches_for_user(db, user.id, status=ProactiveMatchStatus.NEW)
    groups = result.get("groups", []) if isinstance(result, dict) else result
    match_count = result.get("stats", {}).get("total", 0) if isinstance(result, dict) else 0
    ctx = _base_ctx(request, user, "proactive")
    ctx["matches"] = groups
    ctx["sent"] = []
    ctx["tab"] = "matches"
    ctx["match_count"] = match_count
    ctx["success_msg"] = ""
    return template_response("htmx/partials/proactive/list.html", ctx)


@router.post("/v2/proactive/prepare/{site_id}", response_class=HTMLResponse)
async def proactive_prepare_page(
    site_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Full-page prepare/send workflow for proactive offers."""
    import json

    from ..models import ProactiveMatch, SiteContact
    from ..models.crm import CustomerSite as _CS

    form = await request.form()
    match_ids_raw = form.getlist("match_ids")
    match_ids = [int(mid) for mid in match_ids_raw if mid and str(mid).isdigit()]

    if not match_ids:
        from starlette.responses import RedirectResponse

        return RedirectResponse("/v2/proactive", status_code=303)

    matches = (
        db.query(ProactiveMatch)
        .filter(ProactiveMatch.id.in_(match_ids), ProactiveMatch.salesperson_id == user.id)
        .all()
    )
    if not matches:
        from starlette.responses import RedirectResponse

        return RedirectResponse("/v2/proactive", status_code=303)

    site = db.get(_CS, site_id)
    company = site.company if site else None
    contacts = (
        db.query(SiteContact)
        .filter(SiteContact.customer_site_id == site_id)
        .order_by(SiteContact.is_primary.desc(), SiteContact.full_name)
        .all()
    )

    match_data = []
    for m in matches:
        offer = m.offer
        match_data.append(
            {
                "id": m.id,
                "mpn": m.mpn,
                "vendor_name": offer.vendor_name if offer else "",
                "manufacturer": offer.manufacturer if offer else "",
                "qty_available": offer.qty_available if offer else 0,
                "unit_price": float(offer.unit_price) if offer and offer.unit_price else None,
                "margin_pct": m.margin_pct,
                "match_score": m.match_score or 0,
            }
        )

    contact_data = [
        {
            "id": c.id,
            "full_name": c.full_name,
            "email": c.email,
            "title": c.title,
            "is_primary": c.is_primary,
            "has_email": bool(c.email),
        }
        for c in contacts
    ]

    ctx = _base_ctx(request, user, "proactive")
    ctx.update(
        {
            "site_id": site_id,
            "company_name": company.name if company else "Customer",
            "site_name": site.site_name if site else "",
            "matches": match_data,
            "match_ids_json": json.dumps([m["id"] for m in match_data]),
            "contacts": contact_data,
            "error_msg": "",
        }
    )
    return template_response("htmx/partials/proactive/prepare.html", ctx)


@router.post("/v2/partials/proactive/draft", response_class=HTMLResponse)
async def proactive_draft_for_prepare(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """AI-draft a proactive offer email for the prepare page."""
    from ..models import ProactiveMatch, SiteContact
    from ..models.crm import CustomerSite as _CS

    form = await request.form()
    match_ids_raw = form.getlist("match_ids") or (form.get("match_ids", "") or "").split(",")
    match_ids = [int(mid) for mid in match_ids_raw if mid and str(mid).isdigit()]
    contact_ids_raw = form.getlist("contact_ids") or (form.get("contact_ids", "") or "").split(",")
    contact_ids = [int(cid) for cid in contact_ids_raw if cid and str(cid).isdigit()]

    if not match_ids:
        return HTMLResponse('<div class="text-sm text-rose-600">No matches selected.</div>')

    matches = (
        db.query(ProactiveMatch)
        .filter(ProactiveMatch.id.in_(match_ids), ProactiveMatch.salesperson_id == user.id)
        .all()
    )
    if not matches:
        return HTMLResponse('<div class="text-sm text-rose-600">No valid matches found.</div>')

    site_id = matches[0].customer_site_id
    site = db.get(_CS, site_id)
    company = site.company if site else None
    company_name = company.name if company else "Customer"

    # Resolve contact name
    contact_name = None
    if contact_ids:
        primary = db.get(SiteContact, contact_ids[0])
        if primary and primary.full_name:
            _fn_parts = primary.full_name.split()
            contact_name = _fn_parts[0] if _fn_parts else None

    # Parse rep-entered sell prices from form (sell_price_<match_id>)
    draft_sell_prices: dict[str, float] = {}
    for key in form:
        if key.startswith("sell_price_"):
            match_id_str = key[len("sell_price_") :]
            raw_val = form.get(key, "").strip()
            if match_id_str.isdigit() and raw_val:
                try:
                    draft_sell_prices[match_id_str] = float(raw_val)
                except ValueError:
                    pass

    # Build parts list for AI
    parts = []
    for m in matches:
        offer = m.offer
        cost = float(offer.unit_price) if offer and offer.unit_price else 0
        sell = draft_sell_prices.get(str(m.id), cost * 1.3)
        parts.append(
            {
                "mpn": m.mpn,
                "manufacturer": offer.manufacturer if offer else "",
                "qty": offer.qty_available if offer else 0,
                "sell_price": float(sell),
                "condition": offer.condition if offer else "",
                "lead_time": offer.lead_time if offer else "",
                "customer_purchase_count": m.customer_purchase_count or 0,
                "customer_last_purchased_at": (
                    m.customer_last_purchased_at.strftime("%b %Y") if m.customer_last_purchased_at else None
                ),
            }
        )

    salesperson_name = user.name or user.email.split("@")[0]

    try:
        from ..services.proactive_email import draft_proactive_email

        result = await draft_proactive_email(
            company_name=company_name,
            contact_name=contact_name,
            parts=parts,
            salesperson_name=salesperson_name,
        )
        if result:
            subject = result.get("subject", f"Parts Available — {company_name}")
            body = result.get("body", "")
            safe_subject_attr = html_mod.escape(subject)
            subject_json = json.dumps(subject, ensure_ascii=True).replace("</", "<\\/")
            body_json = json.dumps(body, ensure_ascii=True).replace("</", "<\\/")
            return HTMLResponse(f"""
                <input type="hidden" name="ai_subject" id="ai-subject" value="{safe_subject_attr}">
                <input type="hidden" name="ai_body" id="ai-body" value="">
                <script>
                    document.getElementById('subject-input').value = {subject_json};
                    document.getElementById('body-input').value = {body_json};
                </script>
                <div class="text-sm text-emerald-600 flex items-center gap-1">
                    <svg class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
                        <path stroke-linecap="round" stroke-linejoin="round" d="M5 13l4 4L19 7"/>
                    </svg>
                    Draft generated — edit as needed
                </div>
            """)
    except Exception as exc:
        logger.warning("Proactive AI draft failed: {}", exc)

    return HTMLResponse("""
        <div class="text-sm text-amber-600 flex items-center gap-1">
            Auto-draft unavailable. Write your message manually.
            <button type="button"
                    hx-post="/v2/partials/proactive/draft"
                    hx-target="#draft-status"
                    hx-include="[name='match_ids'],[name='contact_ids'],[name^='sell_price_']"
                    class="ml-2 text-brand-600 underline text-xs">Retry</button>
        </div>
    """)


@router.post("/v2/proactive/send", response_class=HTMLResponse)
async def proactive_send_offer(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Send a proactive offer email from the prepare page."""
    form = await request.form()
    match_ids_raw = form.getlist("match_ids") or (form.get("match_ids", "") or "").split(",")
    match_ids = [int(mid) for mid in match_ids_raw if mid and str(mid).isdigit()]
    contact_ids_raw = form.getlist("contact_ids") or (form.get("contact_ids", "") or "").split(",")
    contact_ids = [int(cid) for cid in contact_ids_raw if cid and str(cid).isdigit()]
    subject = form.get("subject", "").strip()
    body = form.get("body", "").strip()

    # Parse rep-entered sell prices keyed as sell_price_<match_id>
    sell_prices: dict[str, float] = {}
    for key in form:
        if key.startswith("sell_price_"):
            match_id_str = key[len("sell_price_") :]
            raw_val = form.get(key, "").strip()
            if match_id_str.isdigit() and raw_val:
                try:
                    sell_prices[match_id_str] = float(raw_val)
                except ValueError:
                    pass  # ignore non-numeric input; service will apply default

    if not match_ids:
        raise HTTPException(400, "No matches selected")
    if not contact_ids:
        raise HTTPException(400, "No contacts selected")

    # Get token
    from ..scheduler import get_valid_token

    token = await get_valid_token(user, db)

    try:
        from ..services.proactive_service import send_proactive_offer

        # Build email HTML from body text
        email_html = None
        if body:
            import html as html_mod

            body_html = html_mod.escape(body).replace("\n", "<br>")
            email_html = f'<div style="font-family:Arial,sans-serif;max-width:700px"><p>{body_html}</p></div>'

        result = await send_proactive_offer(
            db=db,
            user=user,
            token=token or "no-token",
            match_ids=match_ids,
            contact_ids=contact_ids,
            sell_prices=sell_prices,
            subject=subject or None,
            email_html=email_html,
        )

        # Success — reload matches list with success banner
        from ..services.proactive_service import get_matches_for_user

        match_result = get_matches_for_user(db, user.id, status=ProactiveMatchStatus.NEW)
        groups = match_result.get("groups", []) if isinstance(match_result, dict) else match_result
        match_count = match_result.get("stats", {}).get("total", 0) if isinstance(match_result, dict) else 0
        parts_count = len(result.get("line_items", []))
        contacts_count = len(result.get("recipient_emails", []))

        ctx = _base_ctx(request, user, "proactive")
        ctx["matches"] = groups
        ctx["sent"] = []
        ctx["tab"] = "matches"
        ctx["match_count"] = match_count
        ctx["success_msg"] = f"Offer sent to {contacts_count} contact(s) ({parts_count} parts)."
        return template_response("htmx/partials/proactive/list.html", ctx)

    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as exc:
        logger.error("Proactive send failed: {}", exc)
        raise HTTPException(500, "Send failed. Please try again or contact support.")


# ── Sprint 8: Proactive Selling + Prospecting Completion (legacy routes kept for compat) ──


@router.post("/v2/partials/proactive/{match_id}/send", response_class=HTMLResponse)
async def proactive_send_legacy(
    request: Request,
    match_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Send a proactive offer email."""
    from ..models import ProactiveMatch

    match = (
        db.query(ProactiveMatch).filter(ProactiveMatch.id == match_id, ProactiveMatch.salesperson_id == user.id).first()
    )
    if not match:
        raise HTTPException(404, "Match not found")

    form = await request.form()
    body = form.get("body", "").strip()
    if not body:
        raise HTTPException(400, "Email body is required")

    # Mark as sent
    match.status = ProactiveMatchStatus.SENT
    db.commit()
    logger.info("Proactive match {} sent by {}", match_id, user.email)

    # Redirect to list with success message (send_success.html removed in redesign)
    return template_response(
        "htmx/partials/proactive/list.html",
        _base_ctx(request, user, "proactive")
        | {
            "matches": [],
            "sent": [],
            "tab": "matches",
            "match_count": 0,
            "success_msg": f"Offer for {match.mpn} marked as sent",
        },
    )


@router.post("/v2/partials/proactive/{offer_id}/convert", response_class=HTMLResponse)
async def proactive_convert(
    request: Request,
    offer_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Convert a won proactive offer into req+quote+buyplan."""
    from ..models import ProactiveOffer

    offer = db.query(ProactiveOffer).filter(ProactiveOffer.id == offer_id).first()
    if not offer:
        raise HTTPException(404, "Proactive offer not found")
    if offer.salesperson_id and offer.salesperson_id != user.id:
        raise HTTPException(403, "Not your proactive offer")

    try:
        from ..services.proactive_service import convert_proactive_to_win

        result = convert_proactive_to_win(db, offer.id, user)
        return template_response(
            "htmx/partials/proactive/convert_success.html",
            {"request": request, "offer": offer, "result": result},
        )
    except ValueError as exc:
        exc_str = str(exc).lower()
        if "already converted" in exc_str:
            raise HTTPException(409, "This offer has already been converted.")
        raise HTTPException(403, str(exc))
    except Exception as exc:
        logger.error("Proactive conversion failed: {}", exc)
        raise HTTPException(500, "Conversion failed. Please try again.")


@router.get("/v2/partials/proactive/scorecard", response_class=HTMLResponse)
async def proactive_scorecard(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return proactive offers scorecard/metrics panel."""
    try:
        from ..services.proactive_service import get_scorecard

        stats = get_scorecard(db, user.id)
    except (ImportError, RuntimeError, Exception):
        stats = {"total_sent": 0, "total_converted": 0, "conversion_rate": 0, "total_revenue": 0}

    return template_response(
        "htmx/partials/proactive/scorecard.html",
        {"request": request, "stats": stats},
    )


@router.get("/v2/partials/proactive/badge", response_class=HTMLResponse)
async def proactive_badge(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return proactive match count badge for nav sidebar."""
    from ..models import ProactiveMatch

    count = (
        db.query(sqlfunc.count(ProactiveMatch.id))
        .filter(ProactiveMatch.salesperson_id == user.id, ProactiveMatch.status == ProactiveMatchStatus.NEW)
        .scalar()
        or 0
    )
    if count > 0:
        return HTMLResponse(
            f'<span class="ml-auto px-1.5 py-0.5 text-[10px] font-bold text-white bg-emerald-500 rounded-full">{count}</span>'
        )
    return HTMLResponse("")


@router.post("/v2/partials/proactive/do-not-offer", response_class=HTMLResponse)
async def proactive_do_not_offer(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Add an MPN+customer combo to the do-not-offer list (with dedup check)."""
    from ..models.intelligence import ProactiveDoNotOffer
    from ..services.proactive_helpers import is_do_not_offer

    form = await request.form()
    mpn = form.get("mpn", "").strip()
    company_id = form.get("customer_site_id", "") or form.get("company_id", "")

    if not mpn or not company_id:
        raise HTTPException(400, "MPN and company are required")

    try:
        cid = int(company_id)
    except (ValueError, TypeError):
        raise HTTPException(400, "company_id must be an integer")

    # Authz: a do-not-offer rule is scoped to a customer account, so the actor must be
    # able to manage that account — otherwise a cross-account actor could suppress offers
    # for any company by passing an arbitrary company_id in the form.
    company = db.get(Company, cid)
    if not company or not can_manage_account(user, company, db):
        raise HTTPException(403, "Not authorized to manage this account")

    if not is_do_not_offer(db, mpn, cid):
        dno = ProactiveDoNotOffer(
            mpn=mpn.upper(),
            company_id=cid,
            created_by_id=user.id,
        )
        db.add(dno)
        db.commit()
        logger.info("Do-not-offer: {} for company {} by {}", mpn, company_id, user.email)

    # Return an empty collapsed row so the table structure stays valid
    return HTMLResponse('<tr style="display:none" aria-hidden="true"></tr>')


# ── Sprint 9: Materials + Activity + Knowledge ────────────────────────


@router.post("/v2/partials/materials/{material_id}/enrich", response_class=HTMLResponse)
async def enrich_material(
    request: Request,
    material_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Trigger authoritative enrichment for a material card.

    Runs the authoritative ladder (verified -> web -> OEM -> flagged inference) with
    refresh=True so even a terminal card re-enters the ladder, then a status-gated
    structured-spec pass. The Haiku card-enrichment path was removed in SP1
    (2026-06-09).
    """
    from ..constants import MaterialEnrichmentStatus
    from ..models.intelligence import MaterialCard
    from ..services.authoritative_enrichment_service import enrich_cards

    mc = db.get(MaterialCard, material_id)
    if not mc:
        raise HTTPException(404, "Material not found")

    # enrich_cards self-handles ClaudeError / disabled-source outages internally and
    # returns a counts dict (it does NOT raise on a backend outage). Capture it so we can
    # tell the user when nothing actually happened instead of reporting false success.
    enrich_blocked = False
    counts: dict = {}
    try:
        counts = await enrich_cards([material_id], db, refresh=True)
    except Exception as e:
        logger.exception("Enrichment failed for material {}: {}", material_id, e)
        enrich_blocked = True

    # A single card produces exactly one status tally on success. If no real status landed,
    # or a Claude outage / disabled source blocked the run, the card is unchanged.
    status_tallies = sum(int(counts.get(s, 0)) for s in MaterialEnrichmentStatus)
    if counts.get("claude_error") or counts.get("disabled_sources") or status_tallies == 0:
        enrich_blocked = True

    try:
        from ..services.spec_enrichment_service import enrich_card_specs

        await enrich_card_specs([material_id], db, force=True)
    except Exception as e:  # noqa: BLE001 — card-level enrichment may still have succeeded
        logger.warning("Spec enrichment failed for material {}: {}", material_id, e)

    db.refresh(mc)

    response = await material_detail_partial(request, material_id, user, db)
    if enrich_blocked:
        # Surface a user-facing toast WITHOUT breaking the partial swap, via the existing
        # showToast HX-Trigger convention bridged to the global $store.toast (htmx_app.js).
        logger.warning("Enrichment no-op for material {} (counts={}) — surfacing toast", material_id, counts)
        response.headers["HX-Trigger"] = json.dumps(
            {
                "showToast": {
                    "message": "Enrichment couldn't complete — a data source was unavailable. Try again shortly.",
                    "type": "error",
                }
            }
        )
    return response


@router.post("/v2/partials/materials/{material_id}/find-crosses", response_class=HTMLResponse)
async def find_crosses(
    request: Request,
    material_id: int,
    refresh: bool = Form(False),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """On-demand AI search for crosses & substitutes for a single material card.

    Called by: HTMX button on the material detail Crosses section.
    Depends on: claude_json for AI lookup, MaterialCard model.
    """
    from ..models.intelligence import MaterialCard
    from ..utils.claude_client import claude_json as ai_json
    from ..utils.normalization import normalize_mpn_key

    mc = db.get(MaterialCard, material_id)
    if not mc:
        raise HTTPException(404, "Material not found")

    # Return cached results if available (skip on explicit refresh)
    if mc.cross_references and not refresh:
        return template_response(
            "htmx/partials/materials/crosses_section.html",
            {"request": request, "card": mc},
        )

    mpn = mc.display_mpn or mc.normalized_mpn
    mfg = mc.manufacturer or "unknown"
    category = mc.category or "electronic component"

    try:
        import asyncio as _asyncio

        result = await _asyncio.wait_for(
            ai_json(
                f"List all known CROSSES and SUBSTITUTES for this electronic component:\n"
                f"  MPN: {mpn}\n"
                f"  Manufacturer: {mfg}\n"
                f"  Category: {category}\n\n"
                f"Include:\n"
                f"1. Cross-manufacturer equivalents\n"
                f"2. Pin-compatible alternatives / clones\n"
                f"3. Same-family variants (different speed grades, temp ranges, packages)\n"
                f"4. Second-source parts\n\n"
                f"Only include REAL part numbers you are confident exist. Up to 10 results.\n\n"
                f'Respond with JSON: {{"crosses": [{{"mpn": "...", "manufacturer": "..."}}]}}',
                system=(
                    "You are an expert electronic component sourcing engineer. "
                    "List real, verified part numbers only — no guessing."
                ),
                model_tier="smart",
                max_tokens=2048,
            ),
            timeout=30.0,
        )

        crosses = result.get("crosses", []) if isinstance(result, dict) else []
        # Deduplicate: exclude the card's own MPN (both display and normalized forms)
        own_mpns = {normalize_mpn_key(mc.normalized_mpn or ""), normalize_mpn_key(mc.display_mpn or "")} - {""}
        crosses = [
            c for c in crosses if isinstance(c, dict) and c.get("mpn") and normalize_mpn_key(c["mpn"]) not in own_mpns
        ]

        mc.cross_references = crosses
        db.commit()

    except Exception as exc:
        logger.warning("Cross-reference search failed for material {}: {}", material_id, exc)
        db.rollback()
        # Return error inside the same section ID so retry works
        return template_response(
            "htmx/partials/materials/crosses_section.html",
            {"request": request, "card": mc, "error": "Cross-reference search failed. Please try again."},
        )

    # Return the updated crosses section
    return template_response(
        "htmx/partials/materials/crosses_section.html",
        {"request": request, "card": mc},
    )


@router.get("/v2/partials/materials/{material_id}/insights", response_class=HTMLResponse)
async def material_insights(
    request: Request,
    material_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return MPN insights panel for a material card."""
    from ..models.intelligence import MaterialCard

    mc = db.query(MaterialCard).filter(MaterialCard.id == material_id).first()
    if not mc:
        raise HTTPException(404, "Material not found")

    # Get related offers for pricing data
    offers = (
        (
            db.query(Offer)
            .filter(Offer.normalized_mpn == mc.normalized_mpn, Offer.unit_price.isnot(None))
            .order_by(Offer.created_at.desc())
            .limit(20)
            .all()
        )
        if mc.normalized_mpn
        else []
    )

    return template_response(
        "htmx/partials/materials/insights.html",
        {"request": request, "material": mc, "offers": offers},
    )


@router.get("/v2/partials/knowledge", response_class=HTMLResponse)
async def knowledge_list(
    request: Request,
    q: str = "",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return knowledge base entries list."""
    from ..models.knowledge import KnowledgeEntry

    query = db.query(KnowledgeEntry)
    if q.strip():
        sb = SearchBuilder(q.strip())
        query = query.filter(sb.ilike_filter(KnowledgeEntry.content))
    entries = query.order_by(KnowledgeEntry.created_at.desc()).limit(50).all()

    return template_response(
        "htmx/partials/knowledge/list.html",
        {"request": request, "entries": entries, "q": q},
    )


@router.post("/v2/partials/knowledge", response_class=HTMLResponse)
async def create_knowledge_entry(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Create a knowledge base entry."""
    from ..models.knowledge import KnowledgeEntry

    form = await request.form()
    content = form.get("content", "").strip()
    if not content:
        raise HTTPException(400, "Content is required")

    entry = KnowledgeEntry(
        entry_type=form.get("entry_type", "note").strip(),
        content=content,
        source="manual",
        created_by=user.id,
    )
    db.add(entry)
    db.commit()
    logger.info("Knowledge entry {} created by {}", entry.id, user.email)

    return await knowledge_list(request=request, user=user, db=db)


# ── Sprint 10: Admin + Import Completion ──────────────────────────────


@router.get("/v2/partials/admin/api-health", response_class=HTMLResponse)
async def admin_api_health(
    request: Request,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Return connector health dashboard."""
    try:
        from ..services.connector_health import get_health_dashboard

        health = get_health_dashboard(db)
    except (ImportError, RuntimeError, Exception):
        health = {"connectors": [], "overall_status": "unknown"}

    return template_response(
        "htmx/partials/admin/api_health.html",
        {"request": request, "health": health},
    )


@router.post("/v2/partials/vendors/import-stock", response_class=HTMLResponse)
async def import_vendor_stock_list(
    request: Request,
    user: User = Depends(require_buyer),
    db: Session = Depends(get_db),
):
    """Ingest a vendor stock-list upload (CSV/TSV/XLSX) from the Vendors-page modal.

    Thin wrapper over the shared ``stock_list_ingest`` service (same ingest the JSON
    ``POST /api/materials/import-stock`` endpoint uses). Returns an HTML result banner that
    HTMX swaps into ``#vendor-stock-result``.
    """
    from ..services.stock_list_ingest import (
        StockListValidationError,
        ingest_stock_list,
        maybe_trigger_vendor_enrichment,
        validate_metadata,
    )

    form = await request.form()
    file = form.get("file")
    if not file:
        return template_response(
            "htmx/partials/vendors/stock_import_result.html",
            {"request": request, "error": "A stock-list file is required."},
        )

    filename = file.filename or "upload.csv"
    vendor_name = form.get("vendor_name") or ""
    try:
        # Cheap checks (type + vendor) first — reject before buffering the body.
        validate_metadata(filename, vendor_name)
        content = await file.read()
        result = ingest_stock_list(
            db,
            filename=filename,
            content=content,
            vendor_name=vendor_name,
            vendor_website=(form.get("vendor_website") or ""),
        )
    except StockListValidationError as exc:
        return template_response(
            "htmx/partials/vendors/stock_import_result.html",
            {"request": request, "error": exc.message},
        )

    await maybe_trigger_vendor_enrichment(db, result)
    logger.info(
        "Vendor stock-list upload by {}: vendor={!r} imported={} skipped={}",
        user.email,
        result.vendor_name,
        result.imported_rows,
        result.skipped_rows,
    )

    return template_response(
        "htmx/partials/vendors/stock_import_result.html",
        {
            "request": request,
            "error": None,
            "vendor_name": result.vendor_name,
            "imported_rows": result.imported_rows,
            "skipped_rows": result.skipped_rows,
            "total_rows": result.total_rows,
            "warnings": result.warnings,
        },
    )


@router.get("/v2/partials/admin/data-ops", response_class=HTMLResponse)
async def admin_data_ops(
    request: Request,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Return admin data operations panel."""
    from ..models.intelligence import MaterialCard

    vendor_count = db.query(sqlfunc.count(VendorCard.id)).scalar() or 0
    company_count = db.query(sqlfunc.count(Company.id)).filter(Company.is_active.is_(True)).scalar() or 0
    material_count = db.query(sqlfunc.count(MaterialCard.id)).scalar() or 0

    return template_response(
        "htmx/partials/admin/data_ops.html",
        {
            "request": request,
            "vendor_count": vendor_count,
            "company_count": company_count,
            "material_count": material_count,
        },
    )


@router.get("/v2/partials/parts", response_class=HTMLResponse)
async def parts_list_partial(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
    q: str = "",
    requisition_name: str = "",
    customer: str = "",
    brand: str = "",
    status: str = "",
    owner: int = 0,
    date_from: str = "",
    date_to: str = "",
    include_archived: bool | None = None,
    sort: str = "created",
    dir: str = "desc",
    offset: int = 0,
    limit: int = 50,
):
    """Return parts (requirements) list as HTML partial with filters and sorting."""

    # Clamp pagination params
    offset = max(0, offset)
    limit = max(1, min(200, limit))

    # Validate + parse date filters into datetimes (bind real datetimes, not
    # raw strings, so UTCDateTime columns receive UTC-aware values)
    date_from_dt = None
    date_to_dt = None
    if date_from:
        try:
            date_from_dt = datetime.strptime(date_from, "%Y-%m-%d")
        except ValueError:
            date_from = ""
    if date_to:
        try:
            date_to_dt = datetime.strptime(date_to, "%Y-%m-%d")
        except ValueError:
            date_to = ""

    query = db.query(Requirement).join(Requisition, Requirement.requisition_id == Requisition.id)

    # Archive visibility logic:
    # - status=archived  → show only archived parts (any requisition status)
    # - include_archived → show everything (no filtering)
    # - default          → exclude archived parts AND archived requisitions
    if status == "archived":
        query = query.filter(Requirement.sourcing_status == SourcingStatus.ARCHIVED)
    elif not include_archived:
        query = query.filter(
            Requisition.status.in_(
                [
                    RequisitionStatus.OPEN,
                ]
            )
        )
        query = query.filter(Requirement.sourcing_status != SourcingStatus.ARCHIVED)

    # Filters
    if q:
        sb_q = SearchBuilder(q)
        query = query.filter(
            sb_q.ilike_filter(
                Requirement.primary_mpn,
                Requirement.customer_pn,
                Requirement.brand,
                Requirement.substitutes_text,
                Requisition.name,
                Requisition.customer_name,
            )
        )
    if requisition_name:
        query = query.filter(SearchBuilder(requisition_name).ilike_filter(Requisition.name))
    if customer:
        query = query.filter(SearchBuilder(customer).ilike_filter(Requisition.customer_name))
    if brand:
        query = query.filter(SearchBuilder(brand).ilike_filter(Requirement.brand))
    if status and status != "archived":
        query = query.filter(Requirement.sourcing_status == status)
    if owner:
        query = query.filter(Requisition.claimed_by_id == owner)
    if date_from_dt:
        query = query.filter(Requirement.created_at >= date_from_dt)
    if date_to_dt:
        query = query.filter(Requirement.created_at <= date_to_dt)

    total = query.count()

    # Sorting
    sort_map = {
        "mpn": Requirement.primary_mpn,
        "brand": Requirement.brand,
        "qty": Requirement.target_qty,
        "target_price": Requirement.target_price,
        "status": Requirement.sourcing_status,
        "requisition": Requisition.name,
        "customer": Requisition.customer_name,
        "created": Requirement.created_at,
    }
    sort_col = sort_map.get(sort) or Requirement.created_at
    query = query.order_by(sort_col.desc() if dir == "desc" else sort_col.asc())
    query = query.offset(offset).limit(limit)

    requirements = query.options(joinedload(Requirement.requisition).joinedload(Requisition.claimed_by)).all()

    # Aggregate offer count + best price per requirement
    req_ids = [r.id for r in requirements]
    offer_stats = {}
    if req_ids:
        stats = (
            db.query(
                Offer.requirement_id,
                sqlfunc.count(Offer.id).label("cnt"),
                sqlfunc.min(case((Offer.status == OfferStatus.ACTIVE, Offer.unit_price), else_=None)).label("best"),
            )
            .filter(Offer.requirement_id.in_(req_ids))
            .group_by(Offer.requirement_id)
            .all()
        )
        for row in stats:
            offer_stats[row.requirement_id] = {"count": row.cnt, "best_price": row.best}

    # Build display-MPN → material card ID mapping for click-through links
    from ..models.intelligence import MaterialCard
    from ..utils.normalization import normalize_mpn, normalize_mpn_key

    norm_to_mpns: dict[str, list[str]] = {}
    for r in requirements:
        raw_mpns = []
        if r.primary_mpn:
            raw_mpns.append(r.primary_mpn)
        for sub in r.substitutes or []:
            raw = sub["mpn"] if isinstance(sub, dict) else sub
            if raw:
                raw_mpns.append(raw)
        for raw in raw_mpns:
            display = normalize_mpn(raw) or raw.upper()
            nk = normalize_mpn_key(display)
            if nk:
                norm_to_mpns.setdefault(nk, []).append(display)

    sub_card_map: dict[str, int] = {}
    if norm_to_mpns:
        cards = (
            db.query(MaterialCard.normalized_mpn, MaterialCard.id)
            .filter(MaterialCard.normalized_mpn.in_(norm_to_mpns.keys()))
            .filter(MaterialCard.deleted_at.is_(None))
            .all()
        )
        for card_norm, card_id in cards:
            for display_mpn in norm_to_mpns[card_norm]:
                sub_card_map[display_mpn] = card_id

    # Team users for owner filter
    users_list = db.query(User).filter(User.is_active.is_(True)).order_by(User.name).all()

    # Spotlight markers: requirement rows carrying new confirmed offers the user hasn't seen.
    from ..services.alerts import markers_for_tab

    alert_markers = markers_for_tab(db, user, "requisitions")

    ctx = _base_ctx(request, user, "requisitions")
    ctx.update(
        {
            "requirements": requirements,
            "offer_stats": offer_stats,
            "alert_markers": alert_markers,
            "q": q,
            "requisition_name": requisition_name,
            "customer": customer,
            "brand": brand,
            "status": status,
            "owner": owner,
            "date_from": date_from,
            "date_to": date_to,
            "include_archived": include_archived,
            "sort": sort,
            "dir": dir,
            "offset": offset,
            "limit": limit,
            "total": total,
            "users": users_list,
            "user_role": user.role,
            "sub_card_map": sub_card_map,
        }
    )
    return template_response("htmx/partials/parts/list.html", ctx)


@router.get("/v2/partials/parts/{requirement_id}/tab/offers", response_class=HTMLResponse)
async def part_tab_offers(
    requirement_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return offers table for a specific part number."""
    req = db.execute(
        select(Requirement).options(joinedload(Requirement.requisition)).where(Requirement.id == requirement_id)
    ).scalar_one_or_none()
    if not req:
        raise HTTPException(404, "Part not found")

    offers = db.query(Offer).filter(Offer.requirement_id == requirement_id).order_by(Offer.created_at.desc()).all()

    vendor_tier_map = get_vendor_tier_map(db, requirement_id)

    ctx = _base_ctx(request, user, "requisitions")
    ctx.update({"requirement": req, "offers": offers, "vendor_tier_map": vendor_tier_map})
    return template_response("htmx/partials/parts/tabs/offers.html", ctx)


@router.get("/v2/partials/parts/{requirement_id}/tab/sourcing", response_class=HTMLResponse)
async def part_tab_sourcing(
    requirement_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return vendor-level sighting summaries for a specific part number."""
    req = db.get(Requirement, requirement_id)
    if not req:
        raise HTTPException(404, "Part not found")

    summaries = (
        db.query(VendorSightingSummary)
        .filter(VendorSightingSummary.requirement_id == requirement_id)
        .order_by(VendorSightingSummary.score.desc(), VendorSightingSummary.id.desc())
        .all()
    )
    vendor_tier_map = get_vendor_tier_map(db, requirement_id)

    # Raw sightings grouped by vendor for popover breakdowns
    raw_sightings = (
        db.query(Sighting).filter(Sighting.requirement_id == requirement_id).order_by(Sighting.score.desc()).all()
    )
    raw_by_vendor: dict[str, list] = {}
    for s in raw_sightings:
        vn = (s.vendor_name or "unknown").strip()
        raw_by_vendor.setdefault(vn, []).append(s)

    # Derive vendor outreach statuses
    from app.services.sighting_status import compute_vendor_statuses

    vendor_statuses = compute_vendor_statuses(requirement_id, req.requisition_id, db)

    ctx = _base_ctx(request, user, "requisitions")
    ctx.update(
        {
            "requirement": req,
            "summaries": summaries,
            "vendor_tier_map": vendor_tier_map,
            "raw_sightings_by_vendor": raw_by_vendor,
            "vendor_statuses": vendor_statuses,
        }
    )
    return template_response("htmx/partials/parts/tabs/sourcing.html", ctx)


@router.get("/v2/partials/parts/{requirement_id}/tab/req-details", response_class=HTMLResponse)
async def part_tab_req_details(
    requirement_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return the Req Details tab showing parent requisition fields and sibling
    parts."""
    req = db.get(Requirement, requirement_id)
    if not req:
        raise HTTPException(404, "Part not found")

    requisition = req.requisition
    sibling_parts = (
        db.query(Requirement)
        .options(joinedload(Requirement.offers))
        .filter(Requirement.requisition_id == requisition.id)
        .order_by(Requirement.primary_mpn)
        .all()
    )
    users_list = db.query(User).filter(User.is_active.is_(True)).order_by(User.name).all()

    ctx = _base_ctx(request, user, "requisitions")
    ctx.update(
        {
            "requirement": req,
            "requisition": requisition,
            "sibling_parts": sibling_parts,
            "users": users_list,
        }
    )
    return template_response("htmx/partials/parts/tabs/req_details.html", ctx)


@router.get("/v2/partials/parts/{requirement_id}/tab/quotes", response_class=HTMLResponse)
async def part_tab_quotes(
    requirement_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Cross-requisition quote history for the selected part: every quote LINE
    whose MPN matches this part (primary + substitutes) OR whose material_card
    matches this part's canonical card — across ALL requisitions/customers."""
    requirement = db.get(Requirement, requirement_id)
    if not requirement:
        raise HTTPException(404, "Part not found")

    from ..utils.normalization import parse_substitute_mpns

    mpns: set[str] = set()
    if requirement.primary_mpn:
        mpns.add(requirement.primary_mpn.upper())
    for sub in parse_substitute_mpns(requirement.substitutes or [], requirement.primary_mpn or ""):
        if sub.get("mpn"):
            mpns.add(sub["mpn"].upper())

    conds = []
    if mpns:
        conds.append(sqlfunc.upper(QuoteLine.mpn).in_(mpns))
    if requirement.material_card_id:
        conds.append(QuoteLine.material_card_id == requirement.material_card_id)

    quote_lines = []
    if conds:
        quote_lines = (
            db.query(QuoteLine)
            .join(Quote, QuoteLine.quote_id == Quote.id)
            .options(joinedload(QuoteLine.quote).joinedload(Quote.requisition))
            .filter(or_(*conds))
            .order_by(Quote.created_at.desc().nullslast())
            .all()
        )
    ctx = _base_ctx(request, user, "requisitions")
    ctx.update({"requirement": requirement, "quote_lines": quote_lines})
    return template_response("htmx/partials/parts/tabs/quotes.html", ctx)


@router.get("/v2/partials/parts/{requirement_id}/header", response_class=HTMLResponse)
async def part_header(
    requirement_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return the part detail header strip (display mode)."""
    req = db.get(Requirement, requirement_id)
    if not req:
        raise HTTPException(404, "Part not found")

    ctx = _base_ctx(request, user, "requisitions")
    ctx["requirement"] = req
    return template_response("htmx/partials/parts/header.html", ctx)


_PART_HEADER_EDITABLE = {
    "brand",
    "condition",
    "description",
    "manufacturer",
    "target_qty",
    "target_price",
    "sourcing_status",
    "notes",
    "substitutes",
}
_CONDITION_CHOICES = ["New", "Used", "Refurbished", "Any"]


@router.get("/v2/partials/parts/{requirement_id}/header/edit/{field}", response_class=HTMLResponse)
async def part_header_edit_cell(
    requirement_id: int,
    field: str,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return an inline edit input for a single header field."""
    if field not in _PART_HEADER_EDITABLE:
        return HTMLResponse("Invalid field", status_code=400)

    req = db.get(Requirement, requirement_id)
    if not req:
        raise HTTPException(404, "Part not found")

    from markupsafe import escape

    context = request.query_params.get("context", "header")
    current = getattr(req, field, "") or ""
    safe_current = escape(current)
    cell_id = f"hdr-{field}"
    cancel_url = f"/v2/partials/parts/{requirement_id}/header"
    save_url = f"/v2/partials/parts/{requirement_id}/header"
    swap_target = "#part-header-wrap"
    if context == "tab":
        cell_id = f"reqd-{field}"
        cancel_url = f"/v2/partials/parts/{requirement_id}/tab/req-details"
        swap_target = "#part-detail"

    if field == "sourcing_status":
        statuses = ["open", "sourcing", "offered", "quoted", "won", "lost", "archived"]
        options = "".join(
            f'<option value="{s}" {"selected" if s == current else ""}>{s.capitalize()}</option>' for s in statuses
        )
        return HTMLResponse(
            f'<select name="value" id="{cell_id}" '
            f'hx-patch="{save_url}" hx-target="#part-header-wrap" hx-swap="innerHTML" '
            f'hx-vals=\'{{"field": "{field}"}}\' '
            f'class="text-xs px-1.5 py-0.5 rounded border border-brand-300 focus:ring-1 focus:ring-brand-500" '
            f"@keydown.escape=\"htmx.ajax('GET', '{cancel_url}', {{target: '#part-header-wrap', swap: 'innerHTML'}})\">"
            f"{options}</select>",
        )

    if field == "condition":
        options = "".join(
            f'<option value="{c}" {"selected" if c == current else ""}>{c}</option>' for c in _CONDITION_CHOICES
        )
        return HTMLResponse(
            f'<select name="value" id="{cell_id}" '
            f'hx-patch="{save_url}" hx-target="#part-header-wrap" hx-swap="innerHTML" '
            f'hx-vals=\'{{"field": "{field}"}}\' '
            f'class="text-xs px-1.5 py-0.5 rounded border border-brand-300 focus:ring-1 focus:ring-brand-500" '
            f"@keydown.escape=\"htmx.ajax('GET', '{cancel_url}', {{target: '#part-header-wrap', swap: 'innerHTML'}})\">"
            f"{options}</select>",
        )

    if field == "substitutes":
        import json as _json_enc

        subs_json = _json_enc.dumps(req.substitutes if req.substitutes else [])
        subs_json_escaped = subs_json.replace("'", "&#39;").replace('"', "&quot;")
        html = (
            f'<div id="{cell_id}"'
            f' x-data="{{ subs: JSON.parse($el.dataset.subs), saving: false }}"'
            f' data-subs="{subs_json_escaped}">'
            # hidden input serialises rows to JSON on submit
            f'<input type="hidden" name="value" x-bind:value="JSON.stringify(subs)">'
            f'<input type="hidden" name="field" value="{field}">'
            # sub rows
            f'<template x-for="(sub, idx) in subs" :key="idx">'
            f'<div class="flex gap-1 items-center mb-1">'
            f'<input :name="\'sub_mpn_\' + idx" x-model="sub.mpn" placeholder="Sub MPN"'
            f' class="px-2 py-0.5 text-xs font-mono border border-brand-300 rounded focus:ring-1 focus:ring-brand-500 w-32">'
            f'<input :name="\'sub_mfr_\' + idx" x-model="sub.manufacturer" placeholder="Manufacturer"'
            f' class="px-2 py-0.5 text-xs border border-brand-300 rounded focus:ring-1 focus:ring-brand-500 w-36">'
            f'<button type="button" @click="subs.splice(idx, 1)"'
            f' class="text-gray-400 hover:text-red-500 text-sm leading-none px-1">×</button>'
            f"</div>"
            f"</template>"
            f'<div class="flex items-center gap-2 mt-1">'
            f"<button type=\"button\" @click=\"subs.push({{mpn: '', manufacturer: ''}})\""
            f' class="text-[10px] text-brand-500 hover:text-brand-600 font-medium">+ Add</button>'
            f'<button type="button" :disabled="saving"'
            f" @click=\"saving=true; htmx.ajax('PATCH', '{save_url}', {{target: '#part-header-wrap', swap: 'innerHTML', values: {{field: '{field}', value: JSON.stringify(subs)}}}})\""
            f' class="text-[10px] px-2 py-0.5 bg-brand-500 text-white rounded hover:bg-brand-600 font-medium">Save</button>'
            f'<button type="button"'
            f" @click=\"htmx.ajax('GET', '{cancel_url}', {{target: '#part-header-wrap', swap: 'innerHTML'}})\""
            f' class="text-[10px] text-gray-500 hover:text-gray-700">Cancel</button>'
            f"</div>"
            f"</div>"
        )
        return HTMLResponse(html)

    if field == "description":
        return HTMLResponse(
            f'<div id="{cell_id}" class="w-full">'
            f'<textarea name="value" rows="2" '
            f'class="w-full text-xs px-1.5 py-0.5 rounded border border-brand-300 focus:ring-1 focus:ring-brand-500 resize-y" '
            f"@keydown.escape=\"htmx.ajax('GET', '{cancel_url}', {{target: '{swap_target}', swap: 'innerHTML'}})\" "
            f"autofocus>{safe_current}</textarea>"
            f'<div class="flex items-center gap-2 mt-1">'
            f'<button type="button" '
            f"onclick=\"htmx.ajax('PATCH', '{save_url}', {{target: '{swap_target}', swap: 'innerHTML', "
            f"values: {{field: '{field}', value: this.closest('div').parentElement.querySelector('textarea').value}}}})\" "
            f'class="text-[10px] px-2 py-0.5 bg-brand-500 text-white rounded hover:bg-brand-600 font-medium">Save</button>'
            f'<button type="button" '
            f"onclick=\"htmx.ajax('GET', '{cancel_url}', {{target: '{swap_target}', swap: 'innerHTML'}})\" "
            f'class="text-[10px] text-gray-500 hover:text-gray-700">Cancel</button>'
            f"</div></div>",
        )

    input_type = "number" if field in ("target_qty", "target_price") else "text"
    step = ' step="0.0001"' if field == "target_price" else ""

    return HTMLResponse(
        f'<input type="{input_type}" name="value" id="{cell_id}" value="{safe_current}" '
        f'hx-patch="{save_url}" hx-target="#part-header-wrap" hx-swap="innerHTML" '
        f'hx-vals=\'{{"field": "{field}"}}\' '
        f"hx-trigger=\"keyup[key=='Enter']\" "
        f"@keydown.escape=\"htmx.ajax('GET', '{cancel_url}', {{target: '#part-header-wrap', swap: 'innerHTML'}})\" "
        f'class="text-sm px-1.5 py-0.5 rounded border border-brand-300 focus:ring-1 focus:ring-brand-500 w-24"'
        f"{step} autofocus />",
    )


@router.patch("/v2/partials/parts/{requirement_id}/header", response_class=HTMLResponse)
async def part_header_save(
    requirement_id: int,
    request: Request,
    field: str = Form(...),
    value: str = Form(default=""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Save an inline header field edit and return the refreshed header."""
    if field not in _PART_HEADER_EDITABLE:
        return HTMLResponse("Invalid field", status_code=400)

    req = db.get(Requirement, requirement_id)
    if not req:
        raise HTTPException(404, "Part not found")
    require_requisition_access(db, req.requisition_id, user, label="Part")

    if field == "sourcing_status":
        from app.services.requirement_status import transition_requirement

        try:
            ok = transition_requirement(req, value, db, user)
        except ValueError:
            ok = False
        if not ok:
            logger.warning(
                "Status transition rejected: {} → {} for part {}", req.sourcing_status, value, requirement_id
            )
    elif field == "target_qty":
        try:
            req.target_qty = int(value) if value else None
        except (ValueError, TypeError):
            req.target_qty = None
    elif field == "target_price":
        from decimal import Decimal, InvalidOperation

        try:
            req.target_price = Decimal(value) if value else None
        except InvalidOperation:
            req.target_price = None
    elif field == "manufacturer":
        req.manufacturer = value.strip() if value else ""
    elif field == "substitutes":
        import json as _json

        from ..search_service import resolve_material_card
        from ..utils.normalization import parse_substitute_mpns

        try:
            subs_data = _json.loads(value) if value else []
        except (ValueError, TypeError):
            subs_data = []
        req.substitutes = parse_substitute_mpns(subs_data, req.primary_mpn)
        for sub in req.substitutes:
            resolve_material_card(sub["mpn"], db, manufacturer=sub.get("manufacturer", ""))
    else:
        setattr(req, field, value.strip() if value else None)

    db.commit()
    logger.info("Part {} header field '{}' updated by {}", requirement_id, field, user.email)

    ctx = _base_ctx(request, user, "requisitions")
    ctx["requirement"] = req
    response = template_response("htmx/partials/parts/header.html", ctx)
    response.headers["HX-Trigger"] = json.dumps({"part-updated": {"id": requirement_id}})
    return response


# ── Inline table-cell editing ────────────────────────────────────────

_CELL_EDITABLE = {"sourcing_status", "target_qty", "target_price"}
_SPEC_LABELS = {
    "customer_pn": "Customer PN",
    "condition": "Condition",
    "date_codes": "Date Codes",
    "packaging": "Packaging",
    "firmware": "Firmware",
    "hardware_codes": "Hardware",
}


@router.get("/v2/partials/parts/{requirement_id}/cell/edit/{field}", response_class=HTMLResponse)
async def part_cell_edit(
    requirement_id: int,
    field: str,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return an inline edit widget for a single table cell."""
    if field not in _CELL_EDITABLE:
        return HTMLResponse("Invalid field", status_code=400)

    req = db.get(Requirement, requirement_id)
    if not req:
        raise HTTPException(404, "Part not found")

    cell_id = f"cell-{field}-{requirement_id}"
    ctx = _base_ctx(request, user, "requisitions")
    ctx.update({"requirement": req, "field": field, "cell_id": cell_id})
    return template_response("htmx/partials/parts/cell_edit.html", ctx)


@router.get("/v2/partials/parts/{requirement_id}/cell/display/{field}", response_class=HTMLResponse)
async def part_cell_display(
    requirement_id: int,
    field: str,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return display-mode table cell (used for Escape cancel)."""
    if field not in _CELL_EDITABLE:
        return HTMLResponse("Invalid field", status_code=400)

    req = db.get(Requirement, requirement_id)
    if not req:
        raise HTTPException(404, "Part not found")

    cell_id = f"cell-{field}-{requirement_id}"
    ctx = _base_ctx(request, user, "requisitions")
    ctx.update({"requirement": req, "field": field, "cell_id": cell_id})
    return template_response("htmx/partials/parts/cell_display.html", ctx)


@router.patch("/v2/partials/parts/{requirement_id}/cell", response_class=HTMLResponse)
async def part_cell_save(
    requirement_id: int,
    request: Request,
    field: str = Form(...),
    value: str = Form(default=""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Save an inline table-cell edit and return the display-mode cell."""
    if field not in _CELL_EDITABLE:
        return HTMLResponse("Invalid field", status_code=400)

    req = db.get(Requirement, requirement_id)
    if not req:
        raise HTTPException(404, "Part not found")
    require_requisition_access(db, req.requisition_id, user, label="Part")

    if field == "sourcing_status":
        from app.services.requirement_status import transition_requirement

        try:
            transition_requirement(req, value, db, user)
        except ValueError:
            logger.warning(
                "Cell status transition rejected: {} → {} for part {}", req.sourcing_status, value, requirement_id
            )
    elif field == "target_qty":
        try:
            req.target_qty = int(value) if value else None
        except (ValueError, TypeError):
            req.target_qty = None
    elif field == "target_price":
        from decimal import Decimal, InvalidOperation

        try:
            req.target_price = Decimal(value) if value else None
        except InvalidOperation:
            req.target_price = None

    db.commit()
    logger.info("Part {} cell '{}' updated by {}", requirement_id, field, user.email)

    cell_id = f"cell-{field}-{requirement_id}"
    ctx = _base_ctx(request, user, "requisitions")
    ctx.update({"requirement": req, "field": field, "cell_id": cell_id})
    response = template_response("htmx/partials/parts/cell_display.html", ctx)
    response.headers["HX-Trigger"] = json.dumps({"part-updated": {"id": requirement_id}})
    return response


@router.get("/v2/partials/parts/{requirement_id}/edit-spec/{field}", response_class=HTMLResponse)
async def part_spec_edit(
    requirement_id: int,
    field: str,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return inline edit form for a requirement spec field."""
    if field not in _SPEC_LABELS:
        return HTMLResponse("Invalid field", status_code=400)

    req = db.get(Requirement, requirement_id)
    if not req:
        raise HTTPException(404, "Part not found")

    if req.sourcing_status == SourcingStatus.ARCHIVED:
        return HTMLResponse("Cannot edit archived part", status_code=403)

    ctx = _base_ctx(request, user, "requisitions")
    ctx.update(
        {
            "requirement": req,
            "field": field,
            "field_label": _SPEC_LABELS[field],
            "field_value": getattr(req, field, None) or "",
            "condition_choices": _CONDITION_CHOICES,
        }
    )
    return template_response("htmx/partials/parts/spec_edit.html", ctx)


@router.patch("/v2/partials/parts/{requirement_id}/save-spec", response_class=HTMLResponse)
async def part_spec_save(
    requirement_id: int,
    request: Request,
    field: str = Form(...),
    value: str = Form(default=""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Save an inline spec field edit and return the updated display."""
    if field not in _SPEC_LABELS:
        return HTMLResponse("Invalid field", status_code=400)

    req = db.get(Requirement, requirement_id)
    if not req:
        raise HTTPException(404, "Part not found")
    require_requisition_access(db, req.requisition_id, user, label="Part")

    if req.sourcing_status == SourcingStatus.ARCHIVED:
        return HTMLResponse("Cannot edit archived part", status_code=403)

    clean = (value or "").strip() or None
    setattr(req, field, clean)
    db.commit()

    ctx = _base_ctx(request, user, "requisitions")
    ctx.update({"field_value": clean})
    response = template_response("htmx/partials/parts/spec_display.html", ctx)
    response.headers["HX-Trigger"] = json.dumps(
        {
            "part-updated": {"id": requirement_id},
            "showToast": {"message": f"{_SPEC_LABELS[field]} updated", "type": "success"},
        }
    )
    return response


@router.get("/v2/partials/parts/{requirement_id}/tab/activity", response_class=HTMLResponse)
async def part_tab_activity(
    requirement_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return activity timeline for the parent requisition of this part."""
    from ..models.intelligence import ActivityLog

    req = db.get(Requirement, requirement_id)
    if not req:
        raise HTTPException(404, "Part not found")

    activities = (
        db.query(ActivityLog)
        .filter(ActivityLog.requisition_id == req.requisition_id)
        .order_by(ActivityLog.created_at.desc())
        .limit(50)
        .all()
    )

    ctx = _base_ctx(request, user, "requisitions")
    ctx.update({"requirement": req, "activities": activities})
    return template_response("htmx/partials/parts/tabs/activity.html", ctx)


@router.get("/v2/partials/parts/{requirement_id}/tab/comms", response_class=HTMLResponse)
async def part_tab_comms(
    requirement_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return communications tab — notes and tasks for this part."""
    req = db.execute(
        select(Requirement).options(joinedload(Requirement.requisition)).where(Requirement.id == requirement_id)
    ).scalar_one_or_none()
    if not req:
        raise HTTPException(404, "Part not found")

    tasks = (
        db.query(RequisitionTask)
        .options(joinedload(RequisitionTask.assignee), joinedload(RequisitionTask.creator))
        .filter(
            (RequisitionTask.requisition_id == req.requisition_id)
            & ((RequisitionTask.requirement_id == requirement_id) | (RequisitionTask.requirement_id.is_(None)))
        )
        .order_by(
            RequisitionTask.status.asc(),  # pending before done
            RequisitionTask.due_at.asc().nullslast(),
            RequisitionTask.created_at.desc(),
        )
        .all()
    )

    users_list = db.query(User).filter(User.is_active.is_(True)).order_by(User.name).all()

    ctx = _base_ctx(request, user, "requisitions")
    ctx.update({"requirement": req, "tasks": tasks, "users": users_list, "today": date.today()})
    return template_response("htmx/partials/parts/tabs/comms.html", ctx)


@router.get("/v2/partials/parts/{requirement_id}/tab/notes", response_class=HTMLResponse)
async def part_tab_notes(
    requirement_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return sales notes tab for a requirement."""
    req = db.get(Requirement, requirement_id)
    if not req:
        raise HTTPException(404, "Part not found")
    ctx = _base_ctx(request, user, "requisitions")
    ctx["requirement"] = req
    return template_response("htmx/partials/parts/tabs/notes.html", ctx)


@router.patch("/v2/partials/parts/{requirement_id}/notes", response_class=HTMLResponse)
async def save_part_notes(
    requirement_id: int,
    request: Request,
    sale_notes: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Save sales notes for a requirement."""
    req = db.get(Requirement, requirement_id)
    if not req:
        raise HTTPException(404, "Part not found")
    require_requisition_access(db, req.requisition_id, user, label="Part")
    old_sale_notes = req.sale_notes
    req.sale_notes = sale_notes.strip() or None
    if (req.sale_notes or "") != (old_sale_notes or ""):
        _log_activity(
            db,
            activity_type=ActivityType.SALES_NOTE,
            requisition_id=req.requisition_id,
            requirement_id=req.id,
            user_id=user.id,
            description="Sales note updated",
            details={"requirement_id": req.id},
        )
    db.commit()
    ctx = _base_ctx(request, user, "requisitions")
    ctx["requirement"] = req
    return template_response("htmx/partials/parts/tabs/notes.html", ctx)


@router.post("/v2/partials/parts/{requirement_id}/tasks", response_class=HTMLResponse)
async def create_part_task(
    requirement_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Create a task for a specific part number."""
    req = db.get(Requirement, requirement_id)
    if not req:
        raise HTTPException(404, "Part not found")
    require_requisition_access(db, req.requisition_id, user, label="Part")

    form = await request.form()
    title = (form.get("title") or "").strip()
    if not title:
        raise HTTPException(422, "Title is required")

    task = RequisitionTask(
        requisition_id=req.requisition_id,
        requirement_id=requirement_id,
        title=title,
        description=(form.get("notes") or "").strip() or None,
        assigned_to_id=_safe_int(form.get("assigned_to")),
        created_by=user.id,
        due_at=form.get("due_date") or None,
        status=TaskStatus.TODO,
        source="manual",
    )
    db.add(task)
    db.commit()
    logger.info("Task '{}' created for requirement {} by {}", title, requirement_id, user.email)

    # Return refreshed comms tab
    return await part_tab_comms(requirement_id, request, user, db)


@router.post("/v2/partials/parts/tasks/{task_id}/done", response_class=HTMLResponse)
async def mark_task_done(
    task_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Mark a task as done.

    Only the assignee may complete the task.
    """
    try:
        task = task_service.complete_task(db, task_id, user.id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    if task is None:
        raise HTTPException(404, "Task not found")

    logger.info("Task {} marked done by {}", task_id, user.email)

    # Return refreshed comms tab for the requirement
    req_id = task.requirement_id
    if req_id:
        return await part_tab_comms(req_id, request, user, db)

    # Fallback — return just the updated task row
    return HTMLResponse('<div class="text-sm text-green-600">Task completed</div>')


@router.post("/v2/partials/parts/tasks/{task_id}/reopen", response_class=HTMLResponse)
async def reopen_task(
    task_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Reopen a completed task.

    Only the assignee may reopen the task.
    """
    try:
        task = task_service.reopen_task(db, task_id, user.id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    if task is None:
        raise HTTPException(404, "Task not found")

    logger.info("Task {} reopened by {}", task_id, user.email)

    req_id = task.requirement_id
    if req_id:
        return await part_tab_comms(req_id, request, user, db)
    return HTMLResponse('<div class="text-sm text-amber-600">Task reopened</div>')


# ── Archive system ────────────────────────────────────────────────────


@router.patch("/v2/partials/parts/{requirement_id}/archive", response_class=HTMLResponse)
async def archive_single_part(
    requirement_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Archive a single part (requirement) by setting sourcing_status to archived."""
    part = db.get(Requirement, requirement_id)
    if not part:
        raise HTTPException(404, "Part not found")
    require_requisition_access(db, part.requisition_id, user, label="Part")

    part.sourcing_status = SourcingStatus.ARCHIVED
    db.commit()
    logger.info("Part {} archived by {}", requirement_id, user.email)

    response = await parts_list_partial(request=request, user=user, db=db)
    response.headers["HX-Trigger"] = json.dumps({"part-archived": {"id": requirement_id}})
    return response


@router.patch("/v2/partials/parts/{requirement_id}/unarchive", response_class=HTMLResponse)
async def unarchive_single_part(
    requirement_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Unarchive a single part — restores sourcing_status to open."""
    part = db.get(Requirement, requirement_id)
    if not part:
        raise HTTPException(404, "Part not found")
    require_requisition_access(db, part.requisition_id, user, label="Part")

    part.sourcing_status = SourcingStatus.OPEN
    db.commit()
    logger.info("Part {} unarchived by {}", requirement_id, user.email)

    return await parts_list_partial(request=request, user=user, db=db)


@router.post("/v2/partials/parts/bulk-archive", response_class=HTMLResponse)
async def bulk_archive(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Bulk-archive parts and/or requisitions.

    Body: {"requirement_ids": [], "requisition_ids": []}.
    """
    body = await request.json()
    requirement_ids = body.get("requirement_ids", [])
    requisition_ids = body.get("requisition_ids", [])

    # Ownership guard (no-op for buyer/manager/admin; 404 for a restricted non-owner)
    for _rid in requisition_ids:
        require_requisition_access(db, _rid, user)
    if requirement_ids:
        for _r in db.query(Requirement).filter(Requirement.id.in_(requirement_ids)).all():
            require_requisition_access(db, _r.requisition_id, user, label="Requirement")

    # Bulk-update parts in a single query instead of N+1
    if requirement_ids:
        db.query(Requirement).filter(
            Requirement.id.in_(requirement_ids),
        ).update({"sourcing_status": SourcingStatus.ARCHIVED}, synchronize_session="fetch")

    # Archive every part belonging to the named requisitions (part-level
    # sourcing_status — there is no requisition-level archive/hide flag).
    if requisition_ids:
        db.query(Requirement).filter(
            Requirement.requisition_id.in_(requisition_ids),
        ).update({"sourcing_status": SourcingStatus.ARCHIVED}, synchronize_session="fetch")

    db.commit()
    logger.info("Bulk archive by {}: {} parts, {} requisitions", user.email, len(requirement_ids), len(requisition_ids))

    return await parts_list_partial(request=request, user=user, db=db)


@router.post("/v2/partials/parts/bulk-unarchive", response_class=HTMLResponse)
async def bulk_unarchive(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Bulk-unarchive parts and/or requisitions.

    Body: {"requirement_ids": [], "requisition_ids": []}.
    """
    body = await request.json()
    requirement_ids = body.get("requirement_ids", [])
    requisition_ids = body.get("requisition_ids", [])

    # Ownership guard (no-op for buyer/manager/admin; 404 for a restricted non-owner)
    for _rid in requisition_ids:
        require_requisition_access(db, _rid, user)
    if requirement_ids:
        for _r in db.query(Requirement).filter(Requirement.id.in_(requirement_ids)).all():
            require_requisition_access(db, _r.requisition_id, user, label="Requirement")

    # Bulk-update parts in a single query instead of N+1
    if requirement_ids:
        db.query(Requirement).filter(
            Requirement.id.in_(requirement_ids),
            Requirement.sourcing_status == SourcingStatus.ARCHIVED,
        ).update({"sourcing_status": SourcingStatus.OPEN}, synchronize_session="fetch")

    # Restore every archived part belonging to the named requisitions
    # (part-level sourcing_status — there is no requisition-level archive flag).
    if requisition_ids:
        db.query(Requirement).filter(
            Requirement.requisition_id.in_(requisition_ids),
            Requirement.sourcing_status == SourcingStatus.ARCHIVED,
        ).update({"sourcing_status": SourcingStatus.OPEN}, synchronize_session="fetch")

    db.commit()
    logger.info(
        "Bulk unarchive by {}: {} parts, {} requisitions", user.email, len(requirement_ids), len(requisition_ids)
    )

    return await parts_list_partial(request=request, user=user, db=db)


# ── Trouble Tickets ──────────────────────────────────────────────────────


@router.get("/v2/partials/trouble-tickets/workspace", response_class=HTMLResponse)
async def trouble_tickets_workspace(request: Request, user: User = Depends(require_admin)):
    """Trouble Tickets workspace — loaded into #settings-content (admin-only
    console)."""
    return template_response(
        "htmx/partials/tickets/workspace.html",
        {**_base_ctx(request, user, "tickets")},
    )


def _build_ticket_list_context(db: Session, status: str | None) -> dict:
    """Query + group report_button tickets for the list partial.

    Shared by trouble_tickets_list and error_reports.analyze_tickets so both
    render the same grouped view. A logical ``status == "open"`` expands to the
    (submitted, in_progress) set so in-progress tickets stay visible under the
    "Open" pill; any other truthy status is an exact match; falsy means "all".

    Called by: trouble_tickets_list, error_reports.analyze_tickets.
    Depends on: TroubleTicket / RootCauseGroup models.
    """
    from app.models.root_cause_group import RootCauseGroup
    from app.models.trouble_ticket import TroubleTicket

    q = (
        db.query(TroubleTicket)
        .options(joinedload(TroubleTicket.root_cause_group), joinedload(TroubleTicket.submitter))
        .filter(TroubleTicket.source == TicketSource.REPORT_BUTTON)
    )
    if status == "open":
        q = q.filter(TroubleTicket.status.in_([TicketStatus.SUBMITTED, TicketStatus.IN_PROGRESS]))
    elif status:
        q = q.filter(TroubleTicket.status == status)
    q = q.order_by(desc(TroubleTicket.created_at))
    tickets = q.limit(200).all()
    total = len(tickets)

    # Build group lookup only from group IDs present in results
    group_ids = {t.root_cause_group_id for t in tickets if t.root_cause_group_id}
    groups = (
        db.query(RootCauseGroup).filter(RootCauseGroup.id.in_(group_ids)).order_by(RootCauseGroup.title).all()
        if group_ids
        else []
    )
    grouped: dict = {}
    ungrouped = []
    for t in tickets:
        if t.root_cause_group_id:
            grouped.setdefault(t.root_cause_group_id, []).append(t)
        else:
            ungrouped.append(t)

    return {
        "total": total,
        "groups": groups,
        "grouped": grouped,
        "ungrouped": ungrouped,
        "current_status": status or "",
    }


@router.get("/v2/partials/trouble-tickets/list", response_class=HTMLResponse)
async def trouble_tickets_list(
    request: Request,
    status: str = "",
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Trouble Tickets list partial — grouped by root cause, filterable by status."""
    return template_response(
        "htmx/partials/tickets/list.html",
        {**_base_ctx(request, user, "tickets"), **_build_ticket_list_context(db, status)},
    )


@router.get("/v2/partials/trouble-tickets/{ticket_id}", response_class=HTMLResponse)
async def trouble_ticket_detail(
    request: Request,
    ticket_id: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Trouble Ticket detail partial — swapped into #main-content (admin-only
    console)."""
    from app.models.trouble_ticket import TroubleTicket

    ticket = (
        db.query(TroubleTicket)
        .options(joinedload(TroubleTicket.root_cause_group), joinedload(TroubleTicket.submitter))
        .filter(TroubleTicket.id == ticket_id)
        .first()
    )
    if not ticket:
        raise HTTPException(404, "Ticket not found")
    return template_response(
        "htmx/partials/tickets/detail.html",
        {**_base_ctx(request, user, "tickets"), "ticket": ticket},
    )


# ── Step 5: Account/Contact Tasks ────────────────────────────────────────────


@router.get("/v2/partials/customers/{company_id}/tasks", response_class=HTMLResponse)
async def account_tasks_partial(
    request: Request,
    company_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Render the open-tasks list for an account."""
    from app.services.task_service import get_open_tasks_for_company

    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")
    tasks = get_open_tasks_for_company(db, company_id)
    ctx = _base_ctx(request, user, "customers")
    ctx["company_id"] = company_id
    ctx["company_tasks"] = tasks
    return template_response("htmx/partials/customers/_account_tasks.html", ctx)


@router.get("/v2/partials/customers/{company_id}/tasks/add-form", response_class=HTMLResponse)
async def account_task_add_form(
    request: Request,
    company_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Render the inline add-task form for an account."""
    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")
    ctx = _base_ctx(request, user, "customers")
    ctx["company_id"] = company_id
    return template_response("htmx/partials/customers/_account_task_form.html", ctx)


@router.post("/v2/partials/customers/{company_id}/tasks", response_class=HTMLResponse)
async def create_account_task(
    request: Request,
    company_id: int,
    title: str = Form(""),
    due_at: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Create a task scoped to an account; return refreshed task list."""
    from datetime import date
    from datetime import timezone as _tz

    from app.services.task_service import create_company_task, get_open_tasks_for_company

    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")
    if not can_manage_account(user, company, db):
        raise HTTPException(403, "Only the account owner or an admin can create tasks for this account")
    if not title.strip():
        return HTMLResponse('<p class="text-xs text-rose-600">Title is required.</p>')
    due_dt = None
    if due_at.strip():
        try:
            d = date.fromisoformat(due_at.strip())
            due_dt = datetime.combine(d, datetime.min.time()).replace(tzinfo=_tz.utc)
        except ValueError:
            return HTMLResponse('<p class="text-xs text-rose-600">Invalid date.</p>')
    create_company_task(
        db,
        company_id=company_id,
        title=title.strip(),
        due_at=due_dt,
        created_by=user.id,
        assigned_to_id=user.id,
    )
    tasks = get_open_tasks_for_company(db, company_id)
    ctx = _base_ctx(request, user, "customers")
    ctx["company_id"] = company_id
    ctx["company_tasks"] = tasks
    return template_response("htmx/partials/customers/_account_tasks.html", ctx)


@router.get(
    "/v2/partials/customers/{company_id}/contacts/{contact_id}/tasks/add-form",
    response_class=HTMLResponse,
)
async def contact_task_add_form(
    request: Request,
    company_id: int,
    contact_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Render the inline add-task form for a contact."""
    contact = (
        db.query(SiteContact)
        .join(CustomerSite, SiteContact.customer_site_id == CustomerSite.id)
        .filter(SiteContact.id == contact_id, CustomerSite.company_id == company_id)
        .first()
    )
    if not contact:
        raise HTTPException(404, "Contact not found")
    ctx = _base_ctx(request, user, "customers")
    ctx["company_id"] = company_id
    ctx["contact_id"] = contact_id
    return template_response("htmx/partials/customers/_contact_task_form.html", ctx)


@router.post(
    "/v2/partials/customers/{company_id}/contacts/{contact_id}/tasks",
    response_class=HTMLResponse,
)
async def create_contact_task_endpoint(
    request: Request,
    company_id: int,
    contact_id: int,
    title: str = Form(""),
    due_at: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Create a task scoped to a contact; return refreshed contact task list."""
    from datetime import date
    from datetime import timezone as _tz

    from app.services.task_service import create_contact_task, get_open_tasks_for_contact

    # Scoped-join IDOR guard: contact must belong to this company
    contact = (
        db.query(SiteContact)
        .join(CustomerSite, SiteContact.customer_site_id == CustomerSite.id)
        .filter(SiteContact.id == contact_id, CustomerSite.company_id == company_id)
        .first()
    )
    if not contact:
        raise HTTPException(404, "Contact not found")
    company = db.get(Company, company_id)
    if company:
        if not can_manage_account(user, company, db):
            raise HTTPException(403, "Only the account owner or an admin can create tasks for this account")
    if not title.strip():
        return HTMLResponse('<p class="text-xs text-rose-600">Title is required.</p>')
    due_dt = None
    if due_at.strip():
        try:
            d = date.fromisoformat(due_at.strip())
            due_dt = datetime.combine(d, datetime.min.time()).replace(tzinfo=_tz.utc)
        except ValueError:
            return HTMLResponse('<p class="text-xs text-rose-600">Invalid date.</p>')
    create_contact_task(
        db,
        site_contact_id=contact_id,
        title=title.strip(),
        due_at=due_dt,
        created_by=user.id,
        assigned_to_id=user.id,
    )
    tasks = get_open_tasks_for_contact(db, contact_id)
    ctx = _base_ctx(request, user, "customers")
    ctx["contact"] = contact
    ctx["contact_tasks"] = tasks
    ctx["company_id"] = company_id
    ctx["site_id"] = contact.customer_site_id
    return template_response("htmx/partials/customers/_contact_tasks.html", ctx)


@router.get(
    "/v2/partials/customers/{company_id}/contacts/{contact_id}/tasks",
    response_class=HTMLResponse,
)
async def contact_tasks_partial(
    request: Request,
    company_id: int,
    contact_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Render the open-tasks list for a contact (used as cancel target in edit form)."""
    from app.services.task_service import get_open_tasks_for_contact

    contact = (
        db.query(SiteContact)
        .join(CustomerSite, SiteContact.customer_site_id == CustomerSite.id)
        .filter(SiteContact.id == contact_id, CustomerSite.company_id == company_id)
        .first()
    )
    if not contact:
        raise HTTPException(404, "Contact not found")
    tasks = get_open_tasks_for_contact(db, contact_id)
    ctx = _base_ctx(request, user, "customers")
    ctx["contact"] = contact
    ctx["contact_tasks"] = tasks
    ctx["company_id"] = company_id
    ctx["site_id"] = contact.customer_site_id
    return template_response("htmx/partials/customers/_contact_tasks.html", ctx)


@router.post("/v2/partials/tasks/{task_id}/complete", response_class=HTMLResponse)
async def complete_task_endpoint(
    request: Request,
    task_id: int,
    from_my_day: bool = False,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Mark a CRM task done (CRM account/contact or vendor card/contact). No activity
    log is created.

    Permissive auth: the caller only needs require_user — any logged-in user may mark
    a vendor task done (vendor tasks carry no ownership gate at complete time).

    Returns the refreshed parent task list (account, contact, or vendor card). When
    from_my_day=true, returns an empty fragment so the row removes itself via outerHTML
    swap on the My Day worklist.
    """
    from app.services.task_service import (
        complete_crm_task,
        get_open_tasks_for_company,
        get_open_tasks_for_contact,
    )

    task = db.get(RequisitionTask, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    try:
        complete_crm_task(db, task_id, user.id, is_admin=(user.role == UserRole.ADMIN))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    # My Day context: caller handles its own row removal via outerHTML swap.
    if from_my_day:
        return HTMLResponse("")
    # Re-render the appropriate parent container
    if task.company_id:
        tasks = get_open_tasks_for_company(db, task.company_id)
        ctx = _base_ctx(request, user, "customers")
        ctx["company_id"] = task.company_id
        ctx["company_tasks"] = tasks
        return template_response("htmx/partials/customers/_account_tasks.html", ctx)
    if task.site_contact_id:
        contact = db.get(SiteContact, task.site_contact_id)
        tasks = get_open_tasks_for_contact(db, task.site_contact_id)
        ctx = _base_ctx(request, user, "customers")
        ctx["contact"] = contact
        ctx["contact_tasks"] = tasks
        ctx["company_id"] = contact.customer_site.company_id if contact and contact.customer_site else 0
        ctx["site_id"] = task.site_contact_id
        return template_response("htmx/partials/customers/_contact_tasks.html", ctx)
    if task.vendor_card_id:
        from app.services.task_service import get_open_tasks_for_vendor_card

        vendor_tasks = get_open_tasks_for_vendor_card(db, task.vendor_card_id)
        ctx = _base_ctx(request, user, "vendors")
        ctx["vendor_id"] = task.vendor_card_id
        ctx["vendor_tasks"] = vendor_tasks
        return template_response("htmx/partials/vendors/tabs/_vendor_tasks.html", ctx)
    if task.vendor_contact_id:
        from app.models.vendors import VendorContact as _VendorContact
        from app.services.task_service import get_open_tasks_for_vendor_card

        vc = db.get(_VendorContact, task.vendor_contact_id)
        if vc:
            vendor_tasks = get_open_tasks_for_vendor_card(db, vc.vendor_card_id)
            ctx = _base_ctx(request, user, "vendors")
            ctx["vendor_id"] = vc.vendor_card_id
            ctx["vendor_tasks"] = vendor_tasks
            return template_response("htmx/partials/vendors/tabs/_vendor_tasks.html", ctx)
        # VendorContact was deleted — return a safe non-blank acknowledgement.
        return HTMLResponse('<p class="text-xs text-gray-400">Task updated.</p>')
    # Fallback: requisition task — just return empty fragment
    return HTMLResponse("")


@router.delete("/v2/partials/tasks/{task_id}", response_class=HTMLResponse)
async def delete_task_endpoint(
    request: Request,
    task_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Delete a CRM task. Same authz gate as complete_task_endpoint.

    Returns the refreshed parent task list (account or contact).
    """
    from app.services.task_service import (
        delete_task,
        get_open_tasks_for_company,
        get_open_tasks_for_contact,
    )

    task = db.get(RequisitionTask, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    is_vendor_task = task.vendor_card_id is not None or task.vendor_contact_id is not None
    is_crm_task = task.company_id is not None or task.site_contact_id is not None
    if not is_crm_task and not is_vendor_task:
        raise HTTPException(400, "Not a CRM task")
    from app.services.task_service import _is_crm_task_authorized

    # Vendor task delete requires admin; customer task uses the full authz gate.
    if is_vendor_task and not is_crm_task:
        if user.role != UserRole.ADMIN:
            raise HTTPException(403, "Only admins can delete vendor tasks")
    elif not _is_crm_task_authorized(db, task, user.id, is_admin=(user.role == UserRole.ADMIN)):
        raise HTTPException(403, "You are not allowed to delete this task")
    # Capture parent refs before deletion
    company_id = task.company_id
    site_contact_id = task.site_contact_id
    vendor_card_id = task.vendor_card_id
    vendor_contact_id = task.vendor_contact_id
    delete_task(db, task_id)
    logger.info("Task {} deleted by user {}", task_id, user.id)
    if company_id:
        tasks = get_open_tasks_for_company(db, company_id)
        ctx = _base_ctx(request, user, "customers")
        ctx["company_id"] = company_id
        ctx["company_tasks"] = tasks
        return template_response("htmx/partials/customers/_account_tasks.html", ctx)
    if site_contact_id:
        contact = db.get(SiteContact, site_contact_id)
        tasks = get_open_tasks_for_contact(db, site_contact_id)
        ctx = _base_ctx(request, user, "customers")
        ctx["contact"] = contact
        ctx["contact_tasks"] = tasks
        ctx["company_id"] = contact.customer_site.company_id if contact and contact.customer_site else 0
        ctx["site_id"] = site_contact_id
        return template_response("htmx/partials/customers/_contact_tasks.html", ctx)
    if vendor_card_id:
        from app.services.task_service import get_open_tasks_for_vendor_card

        vendor_tasks = get_open_tasks_for_vendor_card(db, vendor_card_id)
        ctx = _base_ctx(request, user, "vendors")
        ctx["vendor_id"] = vendor_card_id
        ctx["vendor_tasks"] = vendor_tasks
        return template_response("htmx/partials/vendors/tabs/_vendor_tasks.html", ctx)
    if vendor_contact_id:
        from app.models.vendors import VendorContact as _VendorContact
        from app.services.task_service import get_open_tasks_for_vendor_card

        vc = db.get(_VendorContact, vendor_contact_id)
        if vc:
            vendor_tasks = get_open_tasks_for_vendor_card(db, vc.vendor_card_id)
            ctx = _base_ctx(request, user, "vendors")
            ctx["vendor_id"] = vc.vendor_card_id
            ctx["vendor_tasks"] = vendor_tasks
            return template_response("htmx/partials/vendors/tabs/_vendor_tasks.html", ctx)
        # VendorContact was deleted — return a safe non-blank acknowledgement.
        return HTMLResponse('<p class="text-xs text-gray-400">Task deleted.</p>')
    return HTMLResponse("")


@router.get("/v2/partials/tasks/{task_id}/edit-form", response_class=HTMLResponse)
async def task_edit_form(
    request: Request,
    task_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return the inline edit form for an existing CRM task (prefilled)."""
    task = db.get(RequisitionTask, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    is_vendor_task = task.vendor_card_id is not None or task.vendor_contact_id is not None
    if not task.company_id and not task.site_contact_id and not is_vendor_task:
        raise HTTPException(400, "Not a CRM task")
    from app.services.task_service import _is_crm_task_authorized

    if not _is_crm_task_authorized(db, task, user.id, is_admin=(user.role == UserRole.ADMIN)):
        raise HTTPException(403, "You are not allowed to edit this task")
    # Vendor task: resolve vendor_id (vendor_card_id direct, or via vendor_contact)
    if is_vendor_task:
        from app.models.vendors import VendorContact as _VendorContact

        vendor_id = task.vendor_card_id
        if not vendor_id and task.vendor_contact_id:
            vc = db.get(_VendorContact, task.vendor_contact_id)
            if vc:
                vendor_id = vc.vendor_card_id
        ctx = _base_ctx(request, user, "vendors")
        ctx["task"] = task
        ctx["vendor_id"] = vendor_id or 0
        return template_response("htmx/partials/vendors/tabs/_vendor_task_edit_form.html", ctx)
    # Resolve the real company_id: account task has it directly; for a contact task
    # we walk contact → site → company so the cancel button has a valid URL.
    real_company_id = task.company_id
    if not real_company_id and task.site_contact_id:
        contact = db.get(SiteContact, task.site_contact_id)
        if contact and contact.customer_site:
            real_company_id = contact.customer_site.company_id
    ctx = _base_ctx(request, user, "customers")
    ctx["task"] = task
    ctx["company_id"] = real_company_id or 0
    return template_response("htmx/partials/customers/_task_edit_form.html", ctx)


@router.post("/v2/partials/tasks/{task_id}/edit", response_class=HTMLResponse)
async def edit_task_endpoint(
    request: Request,
    task_id: int,
    title: str = Form(""),
    due_at: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Update title and/or due_at on a CRM task; return refreshed parent list.

    Authz: same gate as complete/delete — assignee, creator, account owner, or admin.
    """
    from datetime import date
    from datetime import timezone as _tz

    from app.services.task_service import (
        _is_crm_task_authorized,
        get_open_tasks_for_company,
        get_open_tasks_for_contact,
    )

    task = db.get(RequisitionTask, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    _is_vendor = task.vendor_card_id is not None or task.vendor_contact_id is not None
    if not task.company_id and not task.site_contact_id and not _is_vendor:
        raise HTTPException(400, "Not a CRM task")
    if not _is_crm_task_authorized(db, task, user.id, is_admin=(user.role == UserRole.ADMIN)):
        raise HTTPException(403, "You are not allowed to edit this task")
    if not title.strip():
        return HTMLResponse('<p class="text-xs text-rose-600">Title is required.</p>')
    # Parse due_at: empty string → explicit clear (None); non-empty → parse.
    due_dt = None
    if due_at.strip():
        try:
            d = date.fromisoformat(due_at.strip())
            due_dt = datetime.combine(d, datetime.min.time()).replace(tzinfo=_tz.utc)
        except ValueError:
            return HTMLResponse('<p class="text-xs text-rose-600">Invalid date format.</p>')
    # Set both controlled fields directly so an empty due_at clears the existing value.
    # (update_task skips None values to avoid mass-assignment; bypass that for explicit edits.)
    task.title = title.strip()
    task.due_at = due_dt
    db.commit()
    db.refresh(task)
    logger.info("Task {} edited by user {}", task_id, user.id)
    # Re-render the parent container
    task = db.get(RequisitionTask, task_id)
    company_id = task.company_id if task else None
    site_contact_id = task.site_contact_id if task else None
    vendor_card_id_edit = task.vendor_card_id if task else None
    vendor_contact_id_edit = task.vendor_contact_id if task else None
    if company_id:
        tasks = get_open_tasks_for_company(db, company_id)
        ctx = _base_ctx(request, user, "customers")
        ctx["company_id"] = company_id
        ctx["company_tasks"] = tasks
        return template_response("htmx/partials/customers/_account_tasks.html", ctx)
    if site_contact_id:
        contact = db.get(SiteContact, site_contact_id)
        tasks = get_open_tasks_for_contact(db, site_contact_id)
        ctx = _base_ctx(request, user, "customers")
        ctx["contact"] = contact
        ctx["contact_tasks"] = tasks
        ctx["company_id"] = contact.customer_site.company_id if contact and contact.customer_site else 0
        ctx["site_id"] = site_contact_id
        return template_response("htmx/partials/customers/_contact_tasks.html", ctx)
    if vendor_card_id_edit:
        from app.services.task_service import get_open_tasks_for_vendor_card

        vendor_tasks = get_open_tasks_for_vendor_card(db, vendor_card_id_edit)
        ctx = _base_ctx(request, user, "vendors")
        ctx["vendor_id"] = vendor_card_id_edit
        ctx["vendor_tasks"] = vendor_tasks
        return template_response("htmx/partials/vendors/tabs/_vendor_tasks.html", ctx)
    if vendor_contact_id_edit:
        from app.models.vendors import VendorContact as _VendorContact
        from app.services.task_service import get_open_tasks_for_vendor_card

        vc = db.get(_VendorContact, vendor_contact_id_edit)
        if vc:
            vendor_tasks = get_open_tasks_for_vendor_card(db, vc.vendor_card_id)
            ctx = _base_ctx(request, user, "vendors")
            ctx["vendor_id"] = vc.vendor_card_id
            ctx["vendor_tasks"] = vendor_tasks
            return template_response("htmx/partials/vendors/tabs/_vendor_tasks.html", ctx)
    return HTMLResponse("")


@router.post("/v2/partials/tasks/{task_id}/snooze", response_class=HTMLResponse)
async def snooze_task_endpoint(
    request: Request,
    task_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Push a CRM task's due_at forward by one week (or set to tomorrow if no due_at).

    Authz: same gate as edit/complete — assignee, creator, account owner, or admin.
    Returns the refreshed parent task list (account, contact, or vendor card).
    """
    from app.services.task_service import (
        _is_crm_task_authorized,
        get_open_tasks_for_company,
        get_open_tasks_for_contact,
        snooze_task,
    )

    task = db.get(RequisitionTask, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    _is_vendor = task.vendor_card_id is not None or task.vendor_contact_id is not None
    if not task.company_id and not task.site_contact_id and not _is_vendor:
        raise HTTPException(400, "Not a CRM task")
    if not _is_crm_task_authorized(db, task, user.id, is_admin=(user.role == UserRole.ADMIN)):
        raise HTTPException(403, "You are not allowed to snooze this task")
    snooze_task(db, task_id)
    logger.info("Task {} snoozed by user {}", task_id, user.id)
    # Re-render the parent container (same logic as edit_task_endpoint)
    task = db.get(RequisitionTask, task_id)
    company_id = task.company_id if task else None
    site_contact_id = task.site_contact_id if task else None
    vendor_card_id_snooze = task.vendor_card_id if task else None
    vendor_contact_id_snooze = task.vendor_contact_id if task else None
    if company_id:
        tasks = get_open_tasks_for_company(db, company_id)
        ctx = _base_ctx(request, user, "customers")
        ctx["company_id"] = company_id
        ctx["company_tasks"] = tasks
        return template_response("htmx/partials/customers/_account_tasks.html", ctx)
    if site_contact_id:
        contact = db.get(SiteContact, site_contact_id)
        tasks = get_open_tasks_for_contact(db, site_contact_id)
        ctx = _base_ctx(request, user, "customers")
        ctx["contact"] = contact
        ctx["contact_tasks"] = tasks
        ctx["company_id"] = contact.customer_site.company_id if contact and contact.customer_site else 0
        ctx["site_id"] = site_contact_id
        return template_response("htmx/partials/customers/_contact_tasks.html", ctx)
    if vendor_card_id_snooze:
        from app.services.task_service import get_open_tasks_for_vendor_card

        vendor_tasks = get_open_tasks_for_vendor_card(db, vendor_card_id_snooze)
        ctx = _base_ctx(request, user, "vendors")
        ctx["vendor_id"] = vendor_card_id_snooze
        ctx["vendor_tasks"] = vendor_tasks
        return template_response("htmx/partials/vendors/tabs/_vendor_tasks.html", ctx)
    if vendor_contact_id_snooze:
        from app.models.vendors import VendorContact as _VendorContact
        from app.services.task_service import get_open_tasks_for_vendor_card

        vc = db.get(_VendorContact, vendor_contact_id_snooze)
        if vc:
            vendor_tasks = get_open_tasks_for_vendor_card(db, vc.vendor_card_id)
            ctx = _base_ctx(request, user, "vendors")
            ctx["vendor_id"] = vc.vendor_card_id
            ctx["vendor_tasks"] = vendor_tasks
            return template_response("htmx/partials/vendors/tabs/_vendor_tasks.html", ctx)
    return HTMLResponse("")


# ---------------------------------------------------------------------------
# Vendor task routes
# ---------------------------------------------------------------------------


@router.get("/v2/partials/vendors/{vendor_id}/tasks", response_class=HTMLResponse)
async def vendor_tasks_partial(
    request: Request,
    vendor_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Render the open-tasks list for a vendor card."""
    from app.services.task_service import get_open_tasks_for_vendor_card

    vendor = get_vendor_card_or_404(db, vendor_id)
    tasks = get_open_tasks_for_vendor_card(db, vendor_id)
    ctx = _base_ctx(request, user, "vendors")
    ctx["vendor_id"] = vendor_id
    ctx["vendor"] = vendor
    ctx["vendor_tasks"] = tasks
    return template_response("htmx/partials/vendors/tabs/_vendor_tasks.html", ctx)


@router.get("/v2/partials/vendors/{vendor_id}/tasks/add-form", response_class=HTMLResponse)
async def vendor_task_add_form(
    request: Request,
    vendor_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Render the inline add-task form for a vendor card."""
    get_vendor_card_or_404(db, vendor_id)
    ctx = _base_ctx(request, user, "vendors")
    ctx["vendor_id"] = vendor_id
    return template_response("htmx/partials/vendors/tabs/_vendor_task_form.html", ctx)


@router.post("/v2/partials/vendors/{vendor_id}/tasks", response_class=HTMLResponse)
async def create_vendor_task_endpoint(
    request: Request,
    vendor_id: int,
    title: str = Form(""),
    due_at: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Create a task scoped to a vendor; return refreshed task list."""
    from datetime import date as _date
    from datetime import timezone as _tz

    from app.services.task_service import create_vendor_task, get_open_tasks_for_vendor_card

    vendor = get_vendor_card_or_404(db, vendor_id)
    if not title.strip():
        return HTMLResponse('<p class="text-xs text-rose-600">Title is required.</p>')
    due_dt = None
    if due_at.strip():
        try:
            d = _date.fromisoformat(due_at.strip())
            due_dt = datetime.combine(d, datetime.min.time()).replace(tzinfo=_tz.utc)
        except ValueError:
            return HTMLResponse('<p class="text-xs text-rose-600">Invalid date.</p>')
    create_vendor_task(
        db,
        vendor_card_id=vendor_id,
        title=title.strip(),
        due_at=due_dt,
        created_by=user.id,
        assigned_to_id=user.id,
    )
    tasks = get_open_tasks_for_vendor_card(db, vendor_id)
    ctx = _base_ctx(request, user, "vendors")
    ctx["vendor_id"] = vendor_id
    ctx["vendor"] = vendor
    ctx["vendor_tasks"] = tasks
    return template_response("htmx/partials/vendors/tabs/_vendor_tasks.html", ctx)


@router.get(
    "/v2/partials/customers/{company_id}/activity/add-note-form",
    response_class=HTMLResponse,
)
async def activity_add_note_form(
    request: Request,
    company_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Render the inline add-note form for the account Activity tab."""
    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")
    if not can_manage_account(user, company, db):
        raise HTTPException(403, "You are not allowed to add notes for this account")
    ctx = _base_ctx(request, user, "customers")
    ctx["company_id"] = company_id
    return template_response("htmx/partials/customers/_add_note_form.html", ctx)


@router.post(
    "/v2/partials/customers/{company_id}/activity/add-note",
    response_class=HTMLResponse,
)
async def activity_add_note(
    request: Request,
    company_id: int,
    notes: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Log a manual note against a company and return the refreshed Activity tab.

    A note does NOT advance the outbound cadence clock (cadence-neutral: direction=None
    → bump_clocks_from_activity early-returns without touching last_outbound_at).
    """
    from app.services.activity_service import log_company_note

    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")
    if not can_manage_account(user, company, db):
        raise HTTPException(403, "You are not allowed to add notes for this account")
    if not notes.strip():
        return HTMLResponse('<p class="text-xs text-rose-600">Note text is required.</p>')
    log_company_note(
        user_id=user.id,
        company_id=company_id,
        contact_name=None,
        notes=notes.strip(),
        db=db,
    )
    db.commit()
    # Re-render the full activity tab by delegating to the existing tab handler
    return await company_tab(
        request=request,
        company_id=company_id,
        tab="activity",
        site_id=None,
        user=user,
        db=db,
    )


# ── Vendor activity add-note ─────────────────────────────────────────────


@router.get(
    "/v2/partials/vendors/{vendor_id}/activity/add-note-form",
    response_class=HTMLResponse,
)
async def vendor_activity_add_note_form(
    request: Request,
    vendor_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Render the inline add-note form for the vendor Activity tab."""
    vendor = get_vendor_card_or_404(db, vendor_id)
    ctx = _base_ctx(request, user, "vendors")
    ctx["vendor_id"] = vendor.id
    return template_response("htmx/partials/vendors/_add_note_form.html", ctx)


@router.post(
    "/v2/partials/vendors/{vendor_id}/activity/add-note",
    response_class=HTMLResponse,
)
async def vendor_activity_add_note(
    request: Request,
    vendor_id: int,
    notes: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Log a manual note against a vendor and return the refreshed Activity tab.

    Cadence-neutral: direction=None so bump_clocks_from_activity does not advance
    last_outbound_at.
    """
    from app.services.activity_service import log_vendor_note

    vendor = get_vendor_card_or_404(db, vendor_id)
    if not notes.strip():
        return HTMLResponse('<p class="text-xs text-rose-600">Note text is required.</p>')
    log_vendor_note(
        user_id=user.id,
        vendor_card_id=vendor.id,
        vendor_contact_id=None,
        contact_name=None,
        notes=notes.strip(),
        db=db,
        bump_last_activity=False,
    )
    db.commit()
    # Re-render the full activity tab by delegating to the existing tab handler
    return await vendor_tab(
        request=request,
        vendor_id=vendor_id,
        tab="activity",
        user=user,
        db=db,
    )


# ── My Day ──────────────────────────────────────────────────────────────


@router.get("/v2/partials/my-day", response_class=HTMLResponse)
async def my_day_partial(
    request: Request,
    user: User = Depends(require_access(AccessKey.MY_DAY)),
    db: Session = Depends(get_db),
):
    """Tasks page — a filterable worklist of every system task assigned to me.

    (Formerly "My Day", which also carried a follow-up-accounts call-down section;
    that account cadence now lives in CRM, so this page is tasks-only.)

    Reuses task_service.get_my_tasks (which supports the ``status`` filter and excludes
    done by default); ``priority`` and ``due`` are applied here since the helper does not
    support them. The template groups the rows by urgency (Overdue → Due soon → Later →
    No due date). The filter bar's hx-get carries an EXPLICIT hx-target on the inner
    results container (so it never inherits #main-content and replaces the whole page).

    Called by: /v2/my-day full-page shell and nav hx-get, plus the filter-bar selects.
    Depends on: task_service.get_my_tasks.
    """
    from ..services.task_service import get_my_tasks as _get_my_tasks

    now = datetime.now(timezone.utc)
    status = request.query_params.get("status", "").strip()
    priority = request.query_params.get("priority", "").strip()
    due = request.query_params.get("due", "").strip()

    # status flows through the helper (it filters at the query level + defaults to open).
    # get_my_tasks already orders due_at-asc (nulls last), then created_at — soonest first.
    tasks = _get_my_tasks(db, user.id, status=status or None)

    # priority is an int 1-3 (3=high, 2=med, 1=low) — applied here (helper has no filter).
    if priority in ("1", "2", "3"):
        want = int(priority)
        tasks = [t for t in tasks if t.priority == want]

    # due bucket — helper has no due filter, so apply it here against now.
    if due == "overdue":
        tasks = [t for t in tasks if t.due_at is not None and t.due_at < now]
    elif due == "today":
        tasks = [t for t in tasks if t.due_at is not None and t.due_at.date() == now.date()]
    elif due == "upcoming":
        tasks = [t for t in tasks if t.due_at is not None and t.due_at >= now]
    elif due == "none":
        tasks = [t for t in tasks if t.due_at is None]

    ctx = _base_ctx(request, user, "my-day")
    ctx["tasks"] = tasks
    ctx["now_utc"] = now
    ctx["filter_status"] = status
    ctx["filter_priority"] = priority
    ctx["filter_due"] = due
    # Filter-bar changes target the inner #tasks-results container — return the
    # results-only fragment so the filter bar (and its selected values) stay put.
    if request.headers.get("HX-Target") == "tasks-results":
        return template_response("htmx/partials/tasks/_results.html", ctx)
    return template_response("htmx/partials/tasks/list.html", ctx)
