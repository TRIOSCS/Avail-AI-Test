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
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse
from loguru import logger
from sqlalchemy import case, desc, exists, or_, select
from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session, joinedload, selectinload

from ..constants import (
    ActivityType,
    AttributionStatus,
    BuyPlanStatus,
    Channel,
    ContactStatus,
    OfferStatus,
    ProactiveMatchStatus,
    ProspectAccountStatus,
    QuoteStatus,
    RequisitionStatus,
    SourcingStatus,
    TaskStatus,
    TicketSource,
    UserRole,
)
from ..database import get_db
from ..dependencies import get_user, has_buyer_role, require_admin, require_buyer, require_user
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
from ..models.enrichment import ProspectContact
from ..models.faceted_search import CommoditySpecSchema
from ..models.prospect_account import ProspectAccount
from ..models.vendor_sighting_summary import VendorSightingSummary
from ..models.vendors import VendorContact
from ..scoring import classify_lead, explain_lead, score_unified
from ..services import task_service
from ..services.activity_service import log_activity as _log_activity
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
from ..services.freeform_parser_service import parse_freeform_rfq
from ..services.part_history_service import (
    customer_purchases_for_card,
    offers_for_card,
    requirements_for_card,
    sightings_for_card,
)
from ..services.prospect_priority import build_priority_snapshot, build_signal_tags, contacts_summary
from ..services.sighting_ingest import sighting_from_row
from ..services.status_machine import require_valid_transition
from ..services.vendor_unavailability import apply_to_fresh_sightings, maybe_release_on_offer
from ..template_env import template_response, templates
from ..utils.search_builder import SearchBuilder
from ..utils.sql_helpers import escape_like
from ._lookup_helpers import get_requisition_or_404, get_vendor_card_or_404
from .auth import _password_login_enabled

router = APIRouter(tags=["htmx-views"])
_DASH = "\u2014"  # em-dash for template fallbacks

# Vite manifest for asset fingerprinting — read once at import time.
_MANIFEST_PATH = Path("app/static/dist/.vite/manifest.json")
_vite_manifest: dict = {}
if _MANIFEST_PATH.exists():
    _vite_manifest = json.loads(_MANIFEST_PATH.read_text())


def _vite_assets() -> dict:
    """Return Vite asset URLs for templates.

    Keys: js_file, css_files.
    """
    entry = _vite_manifest.get("htmx_app.js", {})
    js_file = entry.get("file", "assets/htmx_app.js")
    css_files = entry.get("css", [])
    # Also add standalone styles entry if not already in css list
    styles_entry = _vite_manifest.get("styles.css", {})
    if styles_entry.get("file") and styles_entry["file"] not in css_files:
        css_files = [styles_entry["file"]] + css_files
    return {"js_file": js_file, "css_files": css_files}


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


def _base_ctx(request: Request, user: User, current_view: str = "") -> dict:
    """Shared template context for all views."""
    assets = _vite_assets()
    return {
        "request": request,
        "user_name": user.name if user else "",
        "user_email": user.email if user else "",
        "is_admin": user.role == UserRole.ADMIN if user else False,
        "current_view": current_view,
        "vite_js": assets["js_file"],
        "vite_css": assets["css_files"],
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


@router.get("/v2", response_class=HTMLResponse)
@router.get("/v2/requisitions", response_class=HTMLResponse)
@router.get("/v2/requisitions/{req_id:int}", response_class=HTMLResponse)
@router.get("/v2/search", response_class=HTMLResponse)
@router.get("/v2/vendors", response_class=HTMLResponse)
@router.get("/v2/vendors/{vendor_id:int}", response_class=HTMLResponse)
@router.get("/v2/customers", response_class=HTMLResponse)
@router.get("/v2/customers/{company_id:int}", response_class=HTMLResponse)
@router.get("/v2/buy-plans", response_class=HTMLResponse)
@router.get("/v2/buy-plans/{bp_id:int}", response_class=HTMLResponse)
@router.get("/v2/excess", response_class=HTMLResponse)
@router.get("/v2/excess/{list_id:int}", response_class=HTMLResponse)
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
        "buy-plans",
        "excess",
        "quotes",
        "prospecting",
        "proactive",
        "settings",
        "materials",
        "follow-ups",
        "trouble-tickets",
        "crm",
        "vendors",
        "customers",
        "search",
        "sightings",
        "requisitions",
    )
    current_view = next((seg for seg in _VIEW_SEGMENTS if f"/{seg}" in path), "requisitions")

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
    elif current_view == "search":
        # Deep-link the Part Dossier: ?mpn= rides along to /v2/partials/search so a
        # bookmarked /v2/search?mpn=<PN> paints the dossier on first load.
        mpn_qs = request.query_params.get("mpn", "").strip()
        partial_url = f"/v2/partials/search?mpn={quote(mpn_qs)}" if mpn_qs else "/v2/partials/search"
    else:
        partial_url = f"/v2/partials/{current_view}"
    # Detail views: a trailing numeric id (/{view}/{id}) overrides the list partial with
    # the detail partial. Each split key equals the current_view, so at most one applies.
    _DETAIL_VIEWS = (
        "requisitions",
        "vendors",
        "customers",
        "buy-plans",
        "excess",
        "quotes",
        "prospecting",
        "trouble-tickets",
    )
    if current_view in _DETAIL_VIEWS and f"/{current_view}/" in path:
        parts = path.split(f"/{current_view}/")
        if len(parts) > 1 and parts[1].isdigit():
            partial_url = f"/v2/partials/{current_view}/{parts[1]}"

    ctx = _base_ctx(request, user, current_view)
    ctx["partial_url"] = partial_url
    return template_response("htmx/base_page.html", ctx)


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

    results = fast_search(q, db)
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

    results = await ai_search(q, db)
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

    results = fast_search(q, db) if q else {"best_match": None, "groups": {}, "total_count": 0}
    return template_response(
        "htmx/partials/search/full_results.html",
        {**_base_ctx(request, user), "results": results, "query": q},
    )


# ── Parts workspace (split-panel entry point) ─────────────────────────


@router.get("/v2/partials/parts/workspace", response_class=HTMLResponse)
async def parts_workspace_partial(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return the split-panel parts workspace shell."""
    ctx = _base_ctx(request, user, "requisitions")
    return template_response("htmx/partials/parts/workspace.html", ctx)


# ── Requisition partials ────────────────────────────────────────────────


@router.get("/v2/partials/requisitions", response_class=HTMLResponse)
async def requisitions_list_partial(
    request: Request,
    q: str = "",
    status: str = "",
    owner: int = Query(0, ge=0),
    urgency: str = "",
    date_from: str = "",
    date_to: str = "",
    sort: str = "created_at",
    sort_dir: Literal["asc", "desc"] = Query("desc", alias="dir"),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return requisitions list as HTML partial with filters and sorting."""
    query = (
        db.query(Requisition)
        .filter(Requisition.is_scratch.is_(False))
        .options(
            joinedload(Requisition.creator),
            joinedload(Requisition.requirements),
            joinedload(Requisition.offers),
        )
    )

    search_term = q.strip()
    if search_term:
        sb = SearchBuilder(search_term)
        safe = f"%{sb.safe}%"
        mpn_match = exists(
            select(Requirement.id).where(
                Requirement.requisition_id == Requisition.id,
                or_(
                    Requirement.primary_mpn.ilike(safe, escape="\\"),
                    Requirement.customer_pn.ilike(safe, escape="\\"),
                    Requirement.substitutes_text.ilike(safe, escape="\\"),
                ),
            )
        )
        query = query.filter(
            or_(
                sb.ilike_filter(Requisition.name, Requisition.customer_name),
                mpn_match,
            )
        )
    if status:
        query = query.filter(Requisition.status == status)
    if owner:
        query = query.filter(Requisition.created_by == owner)
    if urgency:
        query = query.filter(Requisition.urgency == urgency)
    if date_from:
        try:
            dt = datetime.fromisoformat(date_from)
            query = query.filter(Requisition.created_at >= dt)
        except ValueError:
            pass
    if date_to:
        try:
            dt = datetime.fromisoformat(date_to)
            query = query.filter(Requisition.created_at <= dt)
        except ValueError:
            pass

    # Sales users only see their own
    if user.role == UserRole.SALES:
        query = query.filter(Requisition.created_by == user.id)

    total = query.count()

    # Sorting — whitelist of sortable columns, including subqueries for computed counts
    req_count_sub = (
        select(sqlfunc.count(Requirement.id))
        .where(Requirement.requisition_id == Requisition.id)
        .correlate(Requisition)
        .scalar_subquery()
        .label("req_count_sort")
    )
    offer_count_sub = (
        select(sqlfunc.count(Offer.id))
        .where(Offer.requisition_id == Requisition.id)
        .correlate(Requisition)
        .scalar_subquery()
        .label("offer_count_sort")
    )
    # ASAP sorts before all dates (most urgent); nullslast() handles NULLs
    deadline_sort = case(
        (Requisition.deadline == "ASAP", "0000-00-00"),
        else_=Requisition.deadline,
    )
    sort_col_map = {
        "name": Requisition.name,
        "customer_name": Requisition.customer_name,
        "status": Requisition.status,
        "urgency": Requisition.urgency,
        "created_at": Requisition.created_at,
        "deadline": deadline_sort,
        "updated_at": Requisition.updated_at,
        "req_count": req_count_sub,
        "offer_count": offer_count_sub,
    }
    sort_col = sort_col_map.get(sort)
    if sort_col is None:
        logger.warning("Unknown sort key '{}', falling back to created_at", sort)
        sort_col = Requisition.created_at
        sort = "created_at"
    # nullslast: NULLs always sort to the bottom regardless of direction
    order = sort_col.desc().nullslast() if sort_dir == "desc" else sort_col.asc().nullslast()
    reqs = query.order_by(order).offset(offset).limit(limit).all()

    # Attach counts + match reason when searching
    for req in reqs:
        req.req_count = len(req.requirements) if req.requirements else 0
        req.offer_count = len(req.offers) if req.offers else 0
        req.match_reason = None
        req.matched_mpn = None
        if search_term:
            term_lower = search_term.lower()
            if req.name and term_lower in req.name.lower():
                req.match_reason = "name"
            elif req.customer_name and term_lower in req.customer_name.lower():
                req.match_reason = "customer"
            else:
                matched_mpn = next(
                    (
                        r.primary_mpn
                        for r in (req.requirements or [])
                        if (r.primary_mpn and term_lower in r.primary_mpn.lower())
                        or (r.customer_pn and term_lower in r.customer_pn.lower())
                    ),
                    None,
                )
                if matched_mpn:
                    req.match_reason = "part"
                    req.matched_mpn = matched_mpn

    # Match stats for search scope indicators
    match_counts = None
    if search_term:
        match_counts = {"name": 0, "customer": 0, "part": 0}
        for req in reqs:
            reason = req.match_reason
            if reason and reason in match_counts:
                match_counts[reason] += 1

    # Fetch team users for owner dropdown (non-sales only)
    users = []
    if user.role != UserRole.SALES:
        users = db.query(User).order_by(User.name).all()

    from ..services.activity_service import get_inbox_sync_status

    ctx = _base_ctx(request, user, "requisitions")
    ctx.update(
        {
            "requisitions": reqs,
            "q": q,
            "match_counts": match_counts,
            "status": status,
            "owner": owner,
            "urgency": urgency,
            "date_from": date_from,
            "date_to": date_to,
            "sort": sort,
            "dir": sort_dir,
            "total": total,
            "limit": limit,
            "offset": offset,
            "users": users,
            "user_role": user.role,
            "inbox_status": get_inbox_sync_status(user),
        }
    )
    return template_response("htmx/partials/requisitions/list.html", ctx)


@router.get("/v2/partials/requisitions/create-form", response_class=HTMLResponse)
async def requisition_create_form(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return the create requisition modal form."""
    ctx = _base_ctx(request, user, "requisitions")
    return template_response("htmx/partials/requisitions/unified_modal.html", ctx)


@router.get("/v2/partials/requisitions/import-form", response_class=HTMLResponse)
async def requisition_import_form(
    request: Request,
    user: User = Depends(require_user),
):
    """Return the import requisition modal form."""
    ctx = _base_ctx(request, user, "requisitions")
    return template_response("htmx/partials/requisitions/unified_modal.html", ctx)


@router.post("/v2/partials/requisitions/import-parse", response_class=HTMLResponse)
async def requisition_import_parse(
    request: Request,
    name: str = Form(...),
    customer_name: str = Form(""),
    customer_site_id: str = Form(""),
    deadline: str = Form(""),
    urgency: str = Form("normal"),
    raw_text: str = Form(""),
    file: UploadFile | None = File(None),
    user: User = Depends(require_user),
):
    """Parse pasted text or uploaded file with AI, return editable preview."""
    # Extract text from file if uploaded
    text = raw_text.strip()
    if file and file.filename:
        content = await file.read()
        fname = file.filename.lower()
        if fname.endswith((".xlsx", ".xls")):
            from io import BytesIO

            import openpyxl

            wb = openpyxl.load_workbook(BytesIO(content), read_only=True, data_only=True)
            rows = []
            for ws in wb.worksheets:
                for row in ws.iter_rows(values_only=True):
                    cells = [str(c) if c is not None else "" for c in row]
                    if any(cells):
                        rows.append("\t".join(cells))
            text = "\n".join(rows)
        elif fname.endswith(".csv"):
            text = content.decode("utf-8", errors="replace")
        else:
            text = content.decode("utf-8", errors="replace")

    json_mode = request.query_params.get("format") == "json"

    if not text:
        if json_mode:
            from fastapi.responses import JSONResponse

            return JSONResponse({"error": "No data provided", "requirements": []})
        return HTMLResponse(
            '<div class="p-4 text-center text-sm text-rose-600 bg-rose-50 rounded-lg border border-rose-200">'
            "No data provided. Paste text or upload a file."
            "</div>"
        )

    # AI parse
    result = await parse_freeform_rfq(text)
    requirements = result.get("requirements", []) if result else []

    # Use AI-extracted name/customer as fallback if user left them blank
    if not name.strip() and result:
        name = result.get("name", "Untitled")
    if not customer_name.strip() and result:
        customer_name = result.get("customer_name", "")

    if json_mode:
        from fastapi.responses import JSONResponse

        return JSONResponse(
            {
                "requirements": requirements,
                "inferred_name": name,
                "inferred_customer": customer_name,
            }
        )

    ctx = _base_ctx(request, user, "requisitions")
    ctx.update(
        {
            "requirements": requirements,
            "req_name": name,
            "customer_name": customer_name,
            "customer_site_id": customer_site_id,
            "deadline": deadline,
            "urgency": urgency,
            "count": len(requirements),
        }
    )
    return template_response("htmx/partials/requisitions/unified_modal.html", ctx)


@router.post("/v2/partials/requisitions/import-save", response_class=HTMLResponse)
async def requisition_import_save(
    request: Request,
    name: str = Form(...),
    customer_name: str = Form(""),
    customer_site_id: str = Form(""),
    deadline: str = Form(""),
    urgency: str = Form("normal"),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Save AI-parsed requirements as a new requisition."""
    from app.utils.normalization import normalize_mpn_key

    form = await request.form()

    # Collect requirement rows from indexed form fields
    requirements = []
    idx = 0
    while f"reqs[{idx}].primary_mpn" in form:
        mpn = form.get(f"reqs[{idx}].primary_mpn", "").strip()
        if mpn:
            requirements.append(
                {
                    "primary_mpn": mpn,
                    "target_qty": int(form.get(f"reqs[{idx}].target_qty", "1") or "1"),
                    "brand": form.get(f"reqs[{idx}].brand", "").strip() or None,
                    "target_price": float(form.get(f"reqs[{idx}].target_price") or "0") or None,
                    "condition": form.get(f"reqs[{idx}].condition", "new").strip(),
                    "customer_pn": form.get(f"reqs[{idx}].customer_pn", "").strip() or None,
                    "date_codes": form.get(f"reqs[{idx}].date_codes", "").strip() or None,
                    "packaging": form.get(f"reqs[{idx}].packaging", "").strip() or None,
                    "manufacturer": form.get(f"reqs[{idx}].manufacturer", "").strip(),
                    "substitutes": [
                        s.strip() for s in form.get(f"reqs[{idx}].substitutes", "").split(",") if s.strip()
                    ],
                    "firmware": form.get(f"reqs[{idx}].firmware", "").strip() or None,
                    "hardware_codes": form.get(f"reqs[{idx}].hardware_codes", "").strip() or None,
                    "description": form.get(f"reqs[{idx}].description", "").strip() or None,
                    "package_type": form.get(f"reqs[{idx}].package_type", "").strip() or None,
                    "revision": form.get(f"reqs[{idx}].revision", "").strip() or None,
                    "need_by_date": form.get(f"reqs[{idx}].need_by_date", "").strip() or None,
                    "sale_notes": form.get(f"reqs[{idx}].sale_notes", "").strip() or None,
                }
            )
        idx += 1

    if not requirements:
        return HTMLResponse(
            '<div class="p-4 text-center text-sm text-rose-600 bg-rose-50 rounded-lg border border-rose-200">'
            "No valid parts to save."
            "</div>"
        )

    # Create requisition
    site_id = int(customer_site_id) if customer_site_id.strip() else None
    req = Requisition(
        name=name.strip() or "Untitled",
        customer_name=customer_name.strip() or None,
        customer_site_id=site_id,
        deadline=deadline.strip() or None,
        urgency=urgency,
        status=RequisitionStatus.ACTIVE,
        created_by=user.id,
        claimed_by_id=user.id,
    )
    db.add(req)
    db.flush()

    # Create requirements
    from ..search_service import resolve_material_card

    added = len(requirements)
    created_reqs = []
    for item in requirements:
        mpn = item["primary_mpn"]
        card = resolve_material_card(mpn, db)
        r = Requirement(
            requisition_id=req.id,
            primary_mpn=mpn,
            normalized_mpn=normalize_mpn_key(mpn),
            material_card_id=card.id if card else None,
            target_qty=item["target_qty"],
            target_price=item.get("target_price"),
            brand=item.get("brand"),
            manufacturer=item.get("manufacturer", ""),
            condition=item.get("condition", ""),
            substitutes=item.get("substitutes", []),
            customer_pn=item.get("customer_pn", ""),
            date_codes=item.get("date_codes", ""),
            packaging=item.get("packaging", ""),
            firmware=item.get("firmware", ""),
            hardware_codes=item.get("hardware_codes", ""),
            description=item.get("description"),
            package_type=item.get("package_type"),
            revision=item.get("revision"),
            need_by_date=item.get("need_by_date"),
            sale_notes=item.get("sale_notes", ""),
        )
        db.add(r)
        created_reqs.append(r)
        for sub in item.get("substitutes", []):
            sub_mpn = sub["mpn"] if isinstance(sub, dict) else sub
            sub_mfr = sub.get("manufacturer", "") if isinstance(sub, dict) else ""
            resolve_material_card(sub_mpn, db, manufacturer=sub_mfr)

    db.commit()

    # Return success — close modal + refresh parts list + toast
    safe_added = int(added)  # safe: server-computed int
    return HTMLResponse(
        "<div hx-trigger='load' hx-get='/v2/partials/parts' hx-target='#parts-list' hx-swap='innerHTML'>"
        "</div>"
        "<script>"
        "window.dispatchEvent(new CustomEvent('close-modal'));"
        f"Alpine.store('toast').message = 'Requisition created with {safe_added} parts';"
        "Alpine.store('toast').type = 'success';"
        "Alpine.store('toast').show = true;"
        "</script>"
    )


@router.post("/v2/partials/customers/lookup", response_class=HTMLResponse)
async def customer_lookup(
    request: Request,
    company_name: str = Form(...),
    location: str = Form(""),
    user: User = Depends(require_user),
):
    """AI-powered company lookup using Claude with web search."""
    from app.utils.claude_client import claude_json
    from app.utils.claude_errors import ClaudeError, ClaudeUnavailableError

    search_query = company_name.strip()
    if location.strip():
        search_query += f", {location.strip()}"

    try:
        result = await claude_json(
            prompt=f"Search the web for this company: {search_query}\n\n"
            f"Find their official website, main phone number, and physical address.\n\n"
            f"Return ONLY a JSON object with these fields:\n"
            f'{{"company_name": "...", "website": "...", "phone": "...", '
            f'"address_line1": "...", "city": "...", "state": "...", "zip": "...", "country": "..."}}\n\n'
            f"Use empty strings for any field you cannot verify from search results. "
            f"Do NOT guess or make up information — only include data you found online.",
            system="You look up company information using web search. "
            "ONLY return data you can verify from search results. "
            "If you cannot find a phone number or address, return empty strings — never guess.",
            model_tier="smart",
            max_tokens=512,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}],
            timeout=45,
        )
    except (ClaudeUnavailableError, ClaudeError):
        result = None

    if not result:
        return HTMLResponse(
            '<p class="text-xs text-rose-500 mt-1">Could not look up company. Enter details manually.</p>'
        )

    # Render an approval card — escape all AI-provided strings for XSS safety
    # html_mod.escape() for HTML display context
    name = html_mod.escape(result.get("company_name", company_name))
    website = html_mod.escape(result.get("website", ""))
    phone = html_mod.escape(result.get("phone", ""))
    addr_parts = [
        p
        for p in [
            result.get("address_line1", ""),
            result.get("city", ""),
            (result.get("state", "") + " " + result.get("zip", "")).strip(),
            result.get("country", ""),
        ]
        if p
    ]
    address_display = html_mod.escape(", ".join(addr_parts))

    # json.dumps() for values embedded in JavaScript — handles quotes,
    # backslashes, </script> injection, etc.  Produces a quoted string
    # like "O\u0027Brien Corp" that is safe inside JS.
    name_js = json.dumps(result.get("company_name", company_name))
    website_js = json.dumps(result.get("website", ""))
    phone_js = json.dumps(result.get("phone", ""))
    addr1_js = json.dumps(result.get("address_line1", ""))
    city_js = json.dumps(result.get("city", ""))
    state_js = json.dumps(result.get("state", ""))
    zip_js = json.dumps(result.get("zip", ""))
    country_js = json.dumps(result.get("country", "US"))

    html_out = f"""
    <div class="mt-2 p-3 bg-emerald-50 border border-emerald-200 rounded-lg text-xs space-y-1">
      <div class="flex items-center justify-between">
        <span class="font-semibold text-emerald-700">Found: {name}</span>
      </div>
      {"<div class='text-gray-600'>🌐 " + website + "</div>" if website else ""}
      {"<div class='text-gray-600'>📞 " + phone + "</div>" if phone else ""}
      {"<div class='text-gray-600'>📍 " + address_display + "</div>" if address_display else ""}
      <div class="flex gap-2 mt-2">
        <button type="button" onclick="(async function(btn){{
            btn.disabled=true; btn.textContent='Saving...';
            var fd=new FormData();
            fd.append('company_name',{name_js});
            fd.append('website',{website_js});
            fd.append('phone',{phone_js});
            fd.append('address_line1',{addr1_js});
            fd.append('city',{city_js});
            fd.append('state',{state_js});
            fd.append('zip',{zip_js});
            fd.append('country',{country_js});
            try{{
              var r=await fetch('/v2/partials/customers/quick-create',{{method:'POST',body:fd}});
              var html=await r.text();
              var el=btn.closest('.space-y-1');
              el.replaceChildren();
              el.insertAdjacentHTML('afterbegin',html);
              var meta=el.querySelector('[data-site-id]');
              if(meta)document.dispatchEvent(new CustomEvent('customer-created',{{
                detail:{{siteId:meta.dataset.siteId,displayName:meta.dataset.display}}
              }}));
            }}catch(e){{console.error('quick-create failed:',e);btn.textContent='Failed — retry';btn.disabled=false;}}
          }})(this)"
                class="px-3 py-1 text-xs font-semibold bg-emerald-600 text-white rounded hover:bg-emerald-700">
          Use This Customer
        </button>
      </div>
    </div>
    """
    return HTMLResponse(html_out)


@router.post("/v2/partials/customers/quick-create", response_class=HTMLResponse)
async def customer_quick_create(
    request: Request,
    company_name: str = Form(...),
    website: str = Form(""),
    phone: str = Form(""),
    address_line1: str = Form(""),
    city: str = Form(""),
    state: str = Form(""),
    zip: str = Form(""),
    country: str = Form("US"),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Create Company + default site from AI lookup, return JS to select it in
    picker."""
    from app.cache.decorators import invalidate_prefix

    # Check for duplicates
    existing = db.query(Company).filter(Company.name.ilike(company_name.strip())).first()
    if existing:
        site = existing.sites[0] if existing.sites else None
        site_id = site.id if site else ""
        display = html_mod.escape(f"{existing.name} — {site.site_name}" if site else existing.name)
        return HTMLResponse(
            f'<div class="mt-1 p-2 bg-amber-50 border border-amber-200 rounded text-xs text-amber-700">'
            f"Customer already exists. Selected automatically."
            f"</div>"
            f'<span class="hidden" data-site-id="{site_id}" data-display="{display}"></span>'
        )

    # Create company
    domain = ""
    if website:
        from urllib.parse import urlparse

        parsed = urlparse(website if "://" in website else f"https://{website}")
        domain = parsed.netloc.lower().replace("www.", "")

    company = Company(
        name=company_name.strip(),
        website=website.strip() or None,
        domain=domain or None,
        phone=phone.strip() or None,
        hq_city=city.strip() or None,
        hq_state=state.strip() or None,
        hq_country=country.strip() or "US",
        source="ai_lookup",
        is_active=True,
    )
    db.add(company)
    db.flush()

    # Create default site
    site_name = city.strip() or "HQ"
    site = CustomerSite(
        company_id=company.id,
        site_name=site_name,
        address_line1=address_line1.strip() or None,
        city=city.strip() or None,
        state=state.strip() or None,
        zip=zip.strip() or None,
        country=country.strip() or "US",
        contact_phone=phone.strip() or None,
    )
    db.add(site)
    db.commit()

    invalidate_prefix("companies_typeahead")
    invalidate_prefix("company_list")

    display = html_mod.escape(f"{company.name} — {site.site_name}")

    return HTMLResponse(
        f'<div class="mt-1 p-2 bg-emerald-50 border border-emerald-200 rounded text-xs text-emerald-700">'
        f"Created: {display}"
        f"</div>"
        f'<span class="hidden" data-site-id="{site.id}" data-display="{display}"></span>'
    )


@router.get("/v2/partials/requisitions/{req_id}", response_class=HTMLResponse)
async def requisition_detail_partial(
    request: Request,
    req_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return requisition detail as HTML partial with tabs."""
    req = (
        db.query(Requisition)
        .options(
            joinedload(Requisition.creator),
            joinedload(Requisition.requirements).selectinload(Requirement.sightings),
            joinedload(Requisition.offers),
        )
        .filter(Requisition.id == req_id)
        .first()
    )
    if not req:
        raise HTTPException(404, "Requisition not found")

    requirements = req.requirements or []
    for r in requirements:
        r.sighting_count = len(r.sightings) if r.sightings else 0

    req.offer_count = len(req.offers) if req.offers else 0

    # Fetch users for tasks tab assignee dropdown
    users = db.query(User).order_by(User.name).all()

    ctx = _base_ctx(request, user, "requisitions")
    ctx.update({"req": req, "requirements": requirements, "users": users})
    return template_response("htmx/partials/requisitions/detail.html", ctx)


@router.post("/v2/partials/requisitions/create", response_class=HTMLResponse)
async def requisition_create(
    request: Request,
    name: str = Form(...),
    customer_name: str = Form(""),
    deadline: str = Form(""),
    urgency: str = Form("normal"),
    parts_text: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Create a new requisition and return the new row for HTMX prepend."""
    req = Requisition(
        name=name,
        customer_name=customer_name or None,
        deadline=deadline or None,
        urgency=urgency,
        status=RequisitionStatus.ACTIVE,
        created_by=user.id,
        claimed_by_id=user.id,
    )
    db.add(req)
    db.flush()

    # Parse parts text (format: "MPN, Qty" per line)
    from ..search_service import resolve_material_card
    from ..utils.normalization import normalize_mpn_key

    part_count = 0
    if parts_text.strip():
        for line in parts_text.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            parts = [p.strip() for p in line.split(",")]
            mpn = parts[0] if parts else ""
            qty = 1
            if len(parts) > 1:
                try:
                    qty = int(parts[1].strip().replace(",", ""))
                except ValueError:
                    qty = 1
            if mpn:
                card = resolve_material_card(mpn, db)
                r = Requirement(
                    requisition_id=req.id,
                    primary_mpn=mpn,
                    normalized_mpn=normalize_mpn_key(mpn),
                    material_card_id=card.id if card else None,
                    target_qty=qty,
                    sourcing_status=SourcingStatus.OPEN,
                )
                db.add(r)
                part_count += 1

    db.commit()
    db.refresh(req)
    logger.info("Created requisition {} with {} parts from text", req.id, part_count)

    # Attach counts for the row partial
    req.req_count = part_count
    req.offer_count = 0

    ctx = _base_ctx(request, user, "requisitions")
    ctx["req"] = req
    response = template_response("htmx/partials/requisitions/req_row.html", ctx)
    response.headers["HX-Trigger"] = "showToast"
    return response


@router.post("/v2/partials/requisitions/{req_id}/requirements", response_class=HTMLResponse)
async def add_requirement(
    request: Request,
    req_id: int,
    primary_mpn: str = Form(...),
    manufacturer: str = Form(""),
    target_qty: int = Form(1),
    brand: str = Form(""),
    substitutes: str = Form(""),
    target_price: float | None = Form(None),
    condition: str = Form(""),
    date_codes: str = Form(""),
    firmware: str = Form(""),
    hardware_codes: str = Form(""),
    packaging: str = Form(""),
    notes: str = Form(""),
    customer_pn: str = Form(""),
    need_by_date: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Add a requirement to a requisition, return the new row HTML."""
    from datetime import date as date_type

    from ..utils.normalization import parse_substitute_mpns

    if not manufacturer.strip():
        raise HTTPException(422, "Manufacturer is required")

    req = get_requisition_or_404(db, req_id)

    form_data = await request.form()
    sub_mpns = form_data.getlist("sub_mpn")
    sub_mfrs = form_data.getlist("sub_manufacturer")
    subs_raw = [{"mpn": m.strip(), "manufacturer": mfr.strip()} for m, mfr in zip(sub_mpns, sub_mfrs) if m.strip()]
    sub_list = parse_substitute_mpns(subs_raw, primary_mpn)

    from ..search_service import resolve_material_card
    from ..utils.normalization import normalize_mpn_key

    card = resolve_material_card(primary_mpn, db)
    r = Requirement(
        requisition_id=req_id,
        primary_mpn=primary_mpn,
        normalized_mpn=normalize_mpn_key(primary_mpn),
        material_card_id=card.id if card else None,
        target_qty=target_qty,
        brand=brand or None,
        manufacturer=manufacturer.strip(),
        substitutes=sub_list,
        target_price=target_price,
        condition=condition or None,
        date_codes=date_codes or None,
        firmware=firmware or None,
        hardware_codes=hardware_codes or None,
        packaging=packaging or None,
        notes=notes or None,
        customer_pn=customer_pn or None,
        need_by_date=_parse_date_safe(need_by_date, date_type),
        sourcing_status=SourcingStatus.OPEN,
    )
    db.add(r)
    for sub in sub_list:
        resolve_material_card(sub["mpn"], db, manufacturer=sub.get("manufacturer", ""))
    db.commit()
    db.refresh(r)

    # Return the new row via template for HTMX append
    r.sighting_count = 0
    ctx = _base_ctx(request, user, "requisitions")
    ctx["r"] = r
    ctx["req"] = req
    return template_response("htmx/partials/requisitions/tabs/req_row.html", ctx)


@router.post("/v2/partials/requisitions/{req_id}/search-all", response_class=HTMLResponse)
async def requisition_search_all(
    request: Request,
    req_id: int,
    background_tasks: BackgroundTasks,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Trigger search for all requirements in a requisition, then refresh parts
    table."""
    req = get_requisition_or_404(db, req_id)
    requirements = db.query(Requirement).filter(Requirement.requisition_id == req_id).all()
    if not requirements:
        return HTMLResponse(
            "<div id='parts-table-wrapper'><p class='text-sm text-gray-500 p-4'>No requirements to search.</p></div>"
        )

    # Run searches in background
    import os

    if not os.environ.get("TESTING"):
        requirement_ids = [r.id for r in requirements]

        async def _bg_search(req_ids: list[int]):
            from app.database import SessionLocal
            from app.search_service import search_requirement as do_search

            bg_db = SessionLocal()
            try:
                for rid in req_ids:
                    try:
                        req_obj = bg_db.get(Requirement, rid)
                        if req_obj:
                            await do_search(req_obj, bg_db)
                    except Exception:
                        logger.warning("Manual search failed for requirement {}", rid, exc_info=True)
            finally:
                bg_db.close()

        background_tasks.add_task(_bg_search, requirement_ids)

    # Return the parts table with a searching indicator
    requirements = (
        db.query(Requirement)
        .options(selectinload(Requirement.sightings))
        .filter(Requirement.requisition_id == req_id)
        .all()
    )
    for r in requirements:
        r.sighting_count = len(r.sightings) if r.sightings else 0

    ctx = _base_ctx(request, user, "requisitions")
    ctx["req"] = req
    ctx["requirements"] = requirements
    ctx["search_triggered"] = True
    resp = template_response("htmx/partials/requisitions/tabs/parts.html", ctx)
    return resp


@router.get("/v2/partials/requisitions/{req_id}/tab/{tab}", response_class=HTMLResponse)
async def requisition_tab(
    request: Request,
    req_id: int,
    tab: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return a specific tab partial for requisition detail."""
    req = get_requisition_or_404(db, req_id)

    valid_tabs = {"parts", "offers", "quotes", "buy_plans", "tasks", "activity", "responses"}
    if tab not in valid_tabs:
        raise HTTPException(404, f"Unknown tab: {tab}")

    ctx = _base_ctx(request, user, "requisitions")
    ctx["req"] = req

    if tab == "parts":
        requirements = (
            db.query(Requirement)
            .options(selectinload(Requirement.sightings))
            .filter(Requirement.requisition_id == req_id)
            .all()
        )
        for r in requirements:
            r.sighting_count = len(r.sightings) if r.sightings else 0
        ctx["requirements"] = requirements
        return template_response("htmx/partials/requisitions/tabs/parts.html", ctx)

    elif tab == "offers":
        offers = (
            db.query(Offer).filter(Offer.requisition_id == req_id).order_by(Offer.created_at.desc().nullslast()).all()
        )
        # Check for existing draft quote to show "Add to Quote" button
        draft_quote = (
            db.query(Quote)
            .filter(Quote.requisition_id == req_id, Quote.status == QuoteStatus.DRAFT)
            .order_by(Quote.created_at.desc())
            .first()
        )
        ctx["offers"] = offers
        ctx["draft_quote"] = draft_quote
        return template_response("htmx/partials/requisitions/tabs/offers.html", ctx)

    elif tab == "quotes":
        quotes = (
            db.query(Quote).filter(Quote.requisition_id == req_id).order_by(Quote.created_at.desc().nullslast()).all()
        )
        ctx["quotes"] = quotes
        return template_response("htmx/partials/requisitions/tabs/quotes.html", ctx)

    elif tab == "buy_plans":
        buy_plans = (
            db.query(BuyPlan)
            .options(joinedload(BuyPlan.lines))
            .filter(BuyPlan.requisition_id == req_id)
            .order_by(BuyPlan.created_at.desc().nullslast())
            .all()
        )
        ctx["buy_plans"] = buy_plans
        return template_response("htmx/partials/requisitions/tabs/buy_plans.html", ctx)

    elif tab == "tasks":
        tasks = (
            db.query(RequisitionTask)
            .options(joinedload(RequisitionTask.assignee))
            .filter(RequisitionTask.requisition_id == req_id)
            .order_by(RequisitionTask.priority.desc(), RequisitionTask.created_at.desc().nullslast())
            .all()
        )
        users = db.query(User).order_by(User.name).all()
        ctx["tasks"] = tasks
        ctx["users"] = users
        return template_response("htmx/partials/requisitions/tabs/tasks.html", ctx)

    elif tab == "responses":
        # Fetch vendor responses for this requisition
        from ..models.offers import VendorResponse

        responses = (
            db.query(VendorResponse)
            .filter(VendorResponse.requisition_id == req_id)
            .order_by(VendorResponse.received_at.desc().nullslast())
            .all()
        )
        ctx["responses"] = responses
        return template_response("htmx/partials/requisitions/tabs/responses.html", ctx)

    else:  # activity
        from ..services.activity_service import get_requisition_activities

        show_all = request.query_params.get("show_all") == "1"
        ctx["activities"] = get_requisition_activities(req_id, db, meaningful_only=not show_all)
        ctx["show_all"] = show_all
        ctx["req"] = req
        return template_response("htmx/partials/requisitions/tabs/activity.html", ctx)


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


def _parse_date_safe(val, date_cls):
    """Safely parse an ISO date/datetime string, returning None on failure."""
    if not val:
        return None
    try:
        return date_cls.fromisoformat(val)
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

    valid_actions = {"archive", "activate", "assign"}
    if action not in valid_actions:
        raise HTTPException(400, f"Invalid action: {action}")

    reqs = db.query(Requisition).filter(Requisition.id.in_(ids)).all()

    if action == "archive":
        for r in reqs:
            r.status = RequisitionStatus.ARCHIVED
    elif action == "activate":
        for r in reqs:
            r.status = RequisitionStatus.ACTIVE
    elif action == "assign":
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
    from ..dependencies import get_req_for_user

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
    from ..dependencies import get_req_for_user

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


@router.post("/v2/partials/requisitions/{req_id}/action/{action_name}", response_class=HTMLResponse)
async def requisition_row_action(
    request: Request,
    req_id: int,
    action_name: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Execute a row-level action (archive, activate, claim, unclaim, won, lost,
    clone)."""
    from ..dependencies import get_req_for_user

    valid_actions = {"archive", "activate", "claim", "unclaim", "won", "lost", "clone"}
    if action_name not in valid_actions:
        return HTMLResponse("Invalid action", status_code=400)

    req = get_req_for_user(db, user, req_id, options=[])
    if not req:
        return HTMLResponse("Not found", status_code=404)

    msg = "Action completed"
    form = await request.form()

    if action_name in ("archive", "activate", "won", "lost"):
        from ..services.requisition_state import transition

        target = {"archive": "archived", "activate": "active"}.get(action_name, action_name)
        try:
            transition(req, target, user, db)
            msg = f"'{req.name}' → {target}"
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
        essentials_data,
        normalize_offer_condition,
        validate_essentials,
    )
    from ..utils.normalization import normalize_mpn_key
    from ..vendor_utils import normalize_vendor_name

    # Gate: validate the submitted essentials before persisting. On a missing essential,
    # return the existing inline 400 error and do not create the offer.
    norm_condition = normalize_offer_condition(form.get("condition")) or (form.get("condition") or None)
    gate_errors = validate_essentials(
        norm_condition,
        essentials_data(
            manufacturer=form.get("manufacturer"),
            packaging=form.get("packaging"),
            usage=form.get("usage"),
            refurbished_by=form.get("refurbished_by"),
            refurb_process=form.get("refurb_process"),
            cert_doc=form.get("cert_doc"),
            part_condition=form.get("part_condition"),
        ),
    )
    if gate_errors:
        return HTMLResponse(
            f'<div class="text-sm text-rose-600 p-2">{"; ".join(gate_errors)}</div>',
            status_code=400,
        )

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
        essentials_data,
        normalize_offer_condition,
        validate_essentials,
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

    # Gate: validate the effective essentials before persisting. On a missing essential,
    # return the existing inline 400 error and do not commit.
    _q = offer.qualification or {}
    gate_errors = validate_essentials(
        offer.condition,
        essentials_data(
            manufacturer=offer.manufacturer,
            packaging=offer.packaging,
            usage=_q.get("usage"),
            refurbished_by=_q.get("refurbished_by"),
            refurb_process=_q.get("refurb_process"),
            cert_doc=_q.get("cert_doc"),
            part_condition=_q.get("part_condition"),
        ),
    )
    if gate_errors:
        return HTMLResponse(
            f'<div class="text-sm text-rose-600 p-2">{"; ".join(gate_errors)}</div>',
            status_code=400,
        )
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
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Promote a pending_review offer to active and return refreshed queue."""
    offer = db.get(Offer, offer_id)
    if not offer:
        raise HTTPException(404, "Offer not found")
    if offer.status != "pending_review":
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
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Reject a pending_review offer and return refreshed queue."""
    offer = db.get(Offer, offer_id)
    if not offer:
        raise HTTPException(404, "Offer not found")
    if offer.status != "pending_review":
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


@router.post("/v2/partials/requisitions/{req_id}/rfq-send", response_class=HTMLResponse)
async def rfq_send(
    request: Request,
    req_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Send RFQs via Graph API, falling back to DB-only in test mode."""
    import os

    from ..models.offers import Contact as RfqContact

    req = get_requisition_or_404(db, req_id)

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

    form = await request.form()
    body = (form.get("body") or "").strip()

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
    user: User = Depends(require_user),
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

    sources = db.query(ApiSource).filter(ApiSource.status != "disabled").all()
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


# ── Vendor partials ─────────────────────────────────────────────────────


@router.get("/v2/partials/vendors", response_class=HTMLResponse)
async def vendors_list_partial(
    request: Request,
    q: str = "",
    hide_blacklisted: bool = True,
    sort: str = "sighting_count",
    dir: str = "desc",
    my_only: bool = False,
    limit: int = Query(30, ge=1, le=100),
    offset: int = Query(0, ge=0),
    hx_target: str = Query("#main-content", alias="hx_target"),
    push_url_base: str = Query("/v2/vendors", alias="push_url_base"),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return vendor list as HTML partial with blacklisted toggle and sorting."""
    hx_target, push_url_base = _sanitize_hx_params(hx_target, push_url_base, "/v2/vendors")
    from ..models.strategic import StrategicVendor

    query = db.query(VendorCard)

    # Filter to user's strategic vendors if "My Vendors" tab is active
    if my_only:
        my_vendor_ids = (
            db.query(StrategicVendor.vendor_card_id)
            .filter(StrategicVendor.user_id == user.id, StrategicVendor.released_at.is_(None))
            .subquery()
        )
        query = query.filter(VendorCard.id.in_(my_vendor_ids))

    if hide_blacklisted:
        query = query.filter(VendorCard.is_blacklisted.is_(False))

    if q.strip():
        from sqlalchemy import Text, cast

        sb = SearchBuilder(q.strip())
        term = f"%{escape_like(q.strip())}%"
        query = query.filter(
            or_(
                sb.ilike_filter(VendorCard.display_name, VendorCard.domain),
                cast(VendorCard.brand_tags, Text).ilike(term, escape="\\"),
                cast(VendorCard.commodity_tags, Text).ilike(term, escape="\\"),
            )
        )

    total = query.count()

    # Sorting — outbound_asc uses the generalized order_by_clock (VendorCard clocks)
    now_utc = datetime.now(timezone.utc)
    if sort == "outbound_asc":
        vendors = _order_by_clock(query, "outbound", model=VendorCard).offset(offset).limit(limit).all()
    else:
        sort_col_map = {
            "display_name": VendorCard.display_name,
            "sighting_count": VendorCard.sighting_count,
            "overall_win_rate": VendorCard.overall_win_rate,
            "hq_country": VendorCard.hq_country,
            "industry": VendorCard.industry,
        }
        sort_col = sort_col_map.get(sort, VendorCard.sighting_count)
        order = sort_col.desc().nullslast() if dir == "desc" else sort_col.asc().nullslast()
        vendors = query.order_by(order).offset(offset).limit(limit).all()

    # Attach cadence_state to each vendor (tier=None → standard/30d target)
    for v in vendors:
        v.cadence_state = _cadence_state(None, v.last_outbound_at, now_utc)

    ctx = _base_ctx(request, user, "vendors")
    ctx.update(
        {
            "vendors": vendors,
            "q": q,
            "hide_blacklisted": hide_blacklisted,
            "sort": sort,
            "dir": dir,
            "total": total,
            "limit": limit,
            "offset": offset,
            "my_only": my_only,
            "hx_target": hx_target,
            "push_url_base": push_url_base,
            "now_utc": now_utc,
        }
    )
    return template_response("htmx/partials/vendors/list.html", ctx)


@router.get("/v2/partials/vendors/find-by-part", response_class=HTMLResponse)
async def find_by_part_partial(
    request: Request,
    mpn: str = "",
    hx_target: str = Query("#main-content", alias="hx_target"),
    push_url_base: str = Query("/v2/vendors", alias="push_url_base"),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Search vendors by MPN via MaterialVendorHistory."""
    from ..models.intelligence import MaterialCard, MaterialVendorHistory
    from ..utils.normalization import normalize_mpn

    hx_target, push_url_base = _sanitize_hx_params(hx_target, push_url_base, "/v2/vendors")

    results = []
    norm_mpn = normalize_mpn(mpn) if mpn.strip() else None

    if norm_mpn:
        rows = (
            db.query(MaterialVendorHistory, VendorCard)
            .join(MaterialCard, MaterialVendorHistory.material_card_id == MaterialCard.id)
            .outerjoin(VendorCard, VendorCard.normalized_name == MaterialVendorHistory.vendor_name_normalized)
            .filter(MaterialCard.normalized_mpn == norm_mpn)
            .order_by(
                MaterialVendorHistory.times_seen.desc(),
                VendorCard.response_rate.desc().nullslast(),
                VendorCard.total_pos.desc().nullslast(),
                VendorCard.avg_response_hours.asc().nullslast(),
            )
            .limit(30)
            .all()
        )
        for mvh, vc in rows:
            results.append(
                {
                    "vendor_name": mvh.vendor_name,
                    "vendor_id": vc.id if vc else None,
                    "times_seen": mvh.times_seen or 0,
                    "last_price": mvh.last_price,
                    "last_qty": mvh.last_qty,
                    "last_seen": mvh.last_seen,
                    "win_rate": vc.overall_win_rate if vc else None,
                    "avg_response_hours": vc.avg_response_hours if vc else None,
                    "is_affinity": False,
                }
            )

    # If few MVH results, try vendor affinity matching
    if norm_mpn and len(results) < 10:
        try:
            from app.services.vendor_affinity_service import find_vendor_affinity

            affinity_matches = find_vendor_affinity(norm_mpn, db)

            # Add affinity matches that aren't already in results
            existing_vendors = {r["vendor_name"].lower() for r in results}
            for match in affinity_matches:
                vname = match.get("vendor_name", "")
                if vname.lower() not in existing_vendors:
                    results.append(
                        {
                            "vendor_name": vname,
                            "vendor_id": match.get("vendor_id"),
                            "times_seen": 0,
                            "last_price": None,
                            "last_qty": None,
                            "last_seen": None,
                            "win_rate": None,
                            "avg_response_hours": None,
                            "is_affinity": True,
                            "affinity_confidence": match.get("confidence", 0),
                            "affinity_reasoning": match.get("reasoning", ""),
                        }
                    )
        except Exception:
            logger.warning(f"Vendor affinity lookup failed for {norm_mpn}", exc_info=True)

    ctx = _base_ctx(request, user, "vendors")
    ctx.update(
        {
            "mpn": mpn.strip().upper() if mpn.strip() else None,
            "results": results,
            "hx_target": hx_target,
            "push_url_base": push_url_base,
        }
    )
    return template_response("htmx/partials/vendors/find_by_part.html", ctx)


@router.get("/v2/partials/vendors/{vendor_id}", response_class=HTMLResponse)
async def vendor_detail_partial(
    request: Request,
    vendor_id: int,
    mpn: str = "",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return vendor detail as HTML partial with safety data and tabs."""
    vendor = get_vendor_card_or_404(db, vendor_id)

    contacts = (
        db.query(VendorContact)
        .filter(VendorContact.vendor_card_id == vendor_id)
        .order_by(VendorContact.interaction_count.desc().nullslast())
        .limit(20)
        .all()
    )

    sightings_query = db.query(Sighting).filter(Sighting.vendor_name_normalized == vendor.normalized_name)
    if mpn.strip():
        from app.utils.normalization import normalize_mpn

        norm = normalize_mpn(mpn)
        if norm:
            sightings_query = sightings_query.filter(Sighting.normalized_mpn == norm)

    recent_sightings = sightings_query.order_by(Sighting.created_at.desc().nullslast()).limit(10).all()

    # Load safety data from most recent SourcingLead
    safety_band = None
    safety_summary = None
    safety_flags = None
    lead = (
        db.query(SourcingLead)
        .filter(SourcingLead.vendor_name_normalized == vendor.normalized_name)
        .order_by(SourcingLead.created_at.desc())
        .first()
    )
    if lead:
        safety_band = lead.vendor_safety_band
        safety_summary = lead.vendor_safety_summary
        safety_flags = lead.vendor_safety_flags

    now_utc = datetime.now(timezone.utc)
    vendor_cadence = _cadence_state(None, vendor.last_outbound_at, now_utc)
    vendor_nbt = _next_best_touch(None, vendor.last_outbound_at, now_utc)

    ctx = _base_ctx(request, user, "vendors")
    ctx.update(
        {
            "vendor": vendor,
            "contacts": contacts,
            "recent_sightings": recent_sightings,
            "safety_band": safety_band,
            "safety_summary": safety_summary,
            "safety_flags": safety_flags,
            "safety_score": None,
            "safety_available": False,
            "mpn_filter": mpn.strip().upper() if mpn.strip() else None,
            "cadence_state": vendor_cadence,
            "next_best_touch": vendor_nbt,
            "now_utc": now_utc,
        }
    )
    return template_response("htmx/partials/vendors/detail.html", ctx)


@router.get("/v2/partials/vendors/{vendor_id}/tab/{tab}", response_class=HTMLResponse)
async def vendor_tab(
    request: Request,
    vendor_id: int,
    tab: str,
    mpn: str = "",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return a specific tab partial for vendor detail."""
    vendor = get_vendor_card_or_404(db, vendor_id)

    valid_tabs = {"overview", "contacts", "find_contacts", "emails", "analytics", "offers", "reviews"}
    if tab not in valid_tabs:
        raise HTTPException(404, f"Unknown tab: {tab}")

    ctx = _base_ctx(request, user, "vendors")
    ctx["vendor"] = vendor

    if tab == "overview":
        sightings_query = db.query(Sighting).filter(Sighting.vendor_name_normalized == vendor.normalized_name)
        if mpn.strip():
            from app.utils.normalization import normalize_mpn

            norm = normalize_mpn(mpn)
            if norm:
                sightings_query = sightings_query.filter(Sighting.normalized_mpn == norm)

        recent_sightings = sightings_query.order_by(Sighting.created_at.desc().nullslast()).limit(10).all()
        # Safety data
        safety_band = None
        safety_summary = None
        safety_flags = None
        safety_score = None
        safety_available = False
        lead = (
            db.query(SourcingLead)
            .filter(SourcingLead.vendor_name_normalized == vendor.normalized_name)
            .order_by(SourcingLead.created_at.desc())
            .first()
        )
        if lead:
            safety_band = lead.vendor_safety_band
            safety_summary = lead.vendor_safety_summary
            safety_flags = lead.vendor_safety_flags
            safety_score = lead.vendor_safety_score
            safety_available = True
        contacts = (
            db.query(VendorContact)
            .filter(VendorContact.vendor_card_id == vendor_id)
            .order_by(VendorContact.interaction_count.desc().nullslast())
            .limit(20)
            .all()
        )
        ctx.update(
            {
                "recent_sightings": recent_sightings,
                "contacts": contacts,
                "safety_band": safety_band,
                "safety_summary": safety_summary,
                "safety_flags": safety_flags,
                "safety_score": safety_score,
                "safety_available": safety_available,
                "mpn_filter": mpn.strip().upper() if mpn.strip() else None,
            }
        )
        # Re-use the inline overview from the detail template
        # by rendering just the overview portion
        return template_response("htmx/partials/vendors/overview_tab.html", ctx)

    elif tab == "contacts":
        contacts = (
            db.query(VendorContact)
            .filter(VendorContact.vendor_card_id == vendor_id)
            .order_by(VendorContact.interaction_count.desc().nullslast())
            .limit(50)
            .all()
        )
        ctx["contacts"] = contacts
        ctx["vendor"] = vendor
        return template_response("htmx/partials/vendors/tabs/contacts.html", ctx)

    elif tab == "find_contacts":
        prospects = (
            db.query(ProspectContact)
            .filter(ProspectContact.vendor_card_id == vendor_id)
            .order_by(ProspectContact.created_at.desc())
            .limit(50)
            .all()
        )
        ctx["prospects"] = prospects
        return template_response("htmx/partials/vendors/find_contacts_tab.html", ctx)

    elif tab == "emails":
        from ..models.offers import Contact as RfqContact
        from ..models.offers import VendorResponse

        norm = (vendor.normalized_name or "").lower().strip()
        contacts = (
            (
                db.query(RfqContact)
                .filter(RfqContact.vendor_name_normalized == norm)
                .order_by(RfqContact.created_at.desc())
                .limit(100)
                .all()
            )
            if norm
            else []
        )
        responses = (
            (
                db.query(VendorResponse)
                .filter(sqlfunc.lower(VendorResponse.vendor_name) == norm)
                .order_by(VendorResponse.received_at.desc().nullslast())
                .limit(100)
                .all()
            )
            if norm
            else []
        )
        ctx = _base_ctx(request, user, "vendors")
        ctx.update({"vendor": vendor, "contacts": contacts, "responses": responses})
        return template_response("htmx/partials/vendors/emails_tab.html", ctx)

    elif tab == "analytics":
        html = f"""<div class="space-y-6">
          <div class="grid grid-cols-2 md:grid-cols-3 gap-4">
            <div class="bg-white rounded-lg border border-gray-200 p-4 text-center">
              <p class="text-2xl font-bold text-brand-500">{"{:.0f}%".format((vendor.overall_win_rate or 0) * 100)}</p>
              <p class="text-xs text-gray-500 mt-1">Win Rate</p>
            </div>
            <div class="bg-white rounded-lg border border-gray-200 p-4 text-center">
              <p class="text-2xl font-bold text-brand-500">{"{:.0f}%".format((vendor.response_rate or 0) * 100)}</p>
              <p class="text-xs text-gray-500 mt-1">Response Rate</p>
            </div>
            <div class="bg-white rounded-lg border border-gray-200 p-4 text-center">
              <p class="text-2xl font-bold text-brand-500">{"{:.0f}".format(vendor.vendor_score or 0)}</p>
              <p class="text-xs text-gray-500 mt-1">Vendor Score</p>
            </div>
            <div class="bg-white rounded-lg border border-gray-200 p-4 text-center">
              <p class="text-2xl font-bold text-gray-900">{vendor.sighting_count or 0}</p>
              <p class="text-xs text-gray-500 mt-1">Sightings</p>
            </div>
            <div class="bg-white rounded-lg border border-gray-200 p-4 text-center">
              <p class="text-2xl font-bold text-gray-900">{"{:.0f}".format(vendor.avg_response_hours or 0)}</p>
              <p class="text-xs text-gray-500 mt-1">Avg Response Hours</p>
            </div>
            <div class="bg-white rounded-lg border border-gray-200 p-4 text-center">
              <p class="text-2xl font-bold text-gray-900">{"{:.0f}".format(vendor.engagement_score or 0)}</p>
              <p class="text-xs text-gray-500 mt-1">Engagement Score</p>
            </div>
          </div>
          <p class="text-sm text-gray-500 text-center">Analytics data builds as you interact with this vendor.</p>
        </div>"""
        return HTMLResponse(html)

    elif tab == "reviews":
        return await vendor_reviews(request=request, vendor_id=vendor_id, user=user, db=db)

    else:  # offers
        offers = (
            db.query(Offer)
            .filter(Offer.vendor_name == vendor.display_name)
            .order_by(Offer.created_at.desc().nullslast())
            .limit(50)
            .all()
        )
        rows = []
        for o in offers:
            price_str = f"${o.unit_price:,.4f}" if o.unit_price else "RFQ"
            date_str = o.created_at.strftime("%b %d, %Y") if o.created_at else _DASH
            qty_str = f"{o.qty_available:,}" if o.qty_available else _DASH
            rows.append(f"""<tr class="hover:bg-brand-50">
              <td class="px-4 py-2 text-sm font-mono text-gray-900">{o.mpn or _DASH}</td>
              <td class="px-4 py-2 text-sm text-gray-500 text-right">{qty_str}</td>
              <td class="px-4 py-2 text-sm text-right">{price_str}</td>
              <td class="px-4 py-2 text-sm text-gray-500">{o.lead_time or _DASH}</td>
              <td class="px-4 py-2 text-sm text-gray-500">{date_str}</td>
            </tr>""")
        if rows:
            html = f"""<div class="overflow-x-auto">
              <table class="min-w-full divide-y divide-gray-200">
                <thead class="bg-gray-50">
                  <tr>
                    <th class="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">MPN</th>
                    <th class="px-4 py-2 text-right text-xs font-medium text-gray-500 uppercase">Qty</th>
                    <th class="px-4 py-2 text-right text-xs font-medium text-gray-500 uppercase">Price</th>
                    <th class="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Lead Time</th>
                    <th class="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Date</th>
                  </tr>
                </thead>
                <tbody class="divide-y divide-gray-200">{"".join(rows)}</tbody>
              </table>
            </div>"""
        else:
            html = '<div class="p-8 text-center"><p class="text-sm text-gray-500">No offers from this vendor yet.</p></div>'
        return HTMLResponse(html)


# ── Sprint 3: Vendor CRUD + Contact Management ────────────────────────


@router.get("/v2/partials/vendors/{vendor_id}/edit-form", response_class=HTMLResponse)
async def vendor_edit_form(
    request: Request,
    vendor_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return inline edit form for vendor header fields."""
    vendor = get_vendor_card_or_404(db, vendor_id)
    return template_response(
        "htmx/partials/vendors/edit_vendor_form.html",
        {"request": request, "vendor": vendor},
    )


@router.post("/v2/partials/vendors/{vendor_id}/edit", response_class=HTMLResponse)
async def edit_vendor(
    request: Request,
    vendor_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Save vendor edits and return refreshed vendor detail."""
    vendor = get_vendor_card_or_404(db, vendor_id)

    form = await request.form()
    display_name = form.get("display_name", "").strip()
    if display_name:
        vendor.display_name = display_name
        from ..vendor_utils import normalize_vendor_name

        vendor.normalized_name = normalize_vendor_name(display_name)

    website = form.get("website", "").strip()
    vendor.website = website or vendor.website

    emails_raw = form.get("emails", "").strip()
    if emails_raw:
        vendor.emails = [e.strip() for e in emails_raw.split(",") if e.strip()]

    phones_raw = form.get("phones", "").strip()
    if phones_raw:
        vendor.phones = [p.strip() for p in phones_raw.split(",") if p.strip()]

    vendor.updated_at = datetime.now(timezone.utc)
    db.commit()
    logger.info("Vendor {} edited by {}", vendor_id, user.email)

    return await vendor_detail_partial(request=request, vendor_id=vendor_id, user=user, db=db)


@router.post("/v2/partials/vendors/{vendor_id}/toggle-blacklist", response_class=HTMLResponse)
async def toggle_vendor_blacklist(
    request: Request,
    vendor_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Toggle blacklist status and return refreshed vendor detail."""
    vendor = get_vendor_card_or_404(db, vendor_id)

    vendor.is_blacklisted = not vendor.is_blacklisted
    vendor.updated_at = datetime.now(timezone.utc)
    db.commit()
    status = "blacklisted" if vendor.is_blacklisted else "un-blacklisted"
    logger.info("Vendor {} {} by {}", vendor_id, status, user.email)

    return await vendor_detail_partial(request=request, vendor_id=vendor_id, user=user, db=db)


@router.get("/v2/partials/vendors/{vendor_id}/contacts/{contact_id}/timeline", response_class=HTMLResponse)
async def contact_timeline(
    request: Request,
    vendor_id: int,
    contact_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return activity timeline for a vendor contact."""
    from ..models.intelligence import ActivityLog

    contact = (
        db.query(VendorContact)
        .filter(VendorContact.id == contact_id, VendorContact.vendor_card_id == vendor_id)
        .first()
    )
    if not contact:
        raise HTTPException(404, "Contact not found")

    activities = (
        (
            db.query(ActivityLog)
            .filter(ActivityLog.contact_email == contact.email)
            .order_by(ActivityLog.created_at.desc())
            .limit(20)
            .all()
        )
        if contact.email
        else []
    )

    return template_response(
        "htmx/partials/vendors/contact_timeline.html",
        {"request": request, "contact": contact, "activities": activities, "vendor_id": vendor_id},
    )


@router.get("/v2/partials/vendors/{vendor_id}/contact-nudges", response_class=HTMLResponse)
async def vendor_contact_nudges(
    request: Request,
    vendor_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return nudge suggestions for dormant vendor contacts."""
    vendor = get_vendor_card_or_404(db, vendor_id)

    contacts = db.query(VendorContact).filter(VendorContact.vendor_card_id == vendor_id).all()
    # Contacts with no interaction in 30+ days are nudge candidates
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    nudges = []
    for c in contacts:
        if not c.last_interaction_at:
            nudges.append(c)
        else:
            # Handle both tz-aware and tz-naive datetimes
            last = c.last_interaction_at
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            if last < cutoff:
                nudges.append(c)
    return template_response(
        "htmx/partials/vendors/contact_nudges.html",
        {"request": request, "nudges": nudges, "vendor": vendor},
    )


@router.get("/v2/partials/vendors/{vendor_id}/reviews", response_class=HTMLResponse)
async def vendor_reviews(
    request: Request,
    vendor_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return reviews section for a vendor."""
    from ..models import VendorReview

    vendor = get_vendor_card_or_404(db, vendor_id)

    reviews = (
        db.query(VendorReview)
        .filter(VendorReview.vendor_card_id == vendor_id)
        .options(joinedload(VendorReview.user))
        .order_by(VendorReview.created_at.desc())
        .limit(20)
        .all()
    )
    return template_response(
        "htmx/partials/vendors/reviews.html",
        {"request": request, "reviews": reviews, "vendor": vendor, "user": user},
    )


@router.post("/v2/partials/vendors/{vendor_id}/reviews", response_class=HTMLResponse)
async def add_vendor_review(
    request: Request,
    vendor_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Add a review to a vendor and return refreshed reviews."""
    from ..models import VendorReview

    get_vendor_card_or_404(db, vendor_id)  # validates existence

    form = await request.form()
    try:
        rating = int(form.get("rating", "3"))
    except (ValueError, TypeError):
        rating = 3
    comment = form.get("comment", "").strip()

    review = VendorReview(
        vendor_card_id=vendor_id,
        user_id=user.id,
        rating=max(1, min(5, rating)),
        comment=comment or None,
    )
    db.add(review)
    db.commit()
    logger.info("Review added for vendor {} by {} (rating={})", vendor_id, user.email, rating)

    return await vendor_reviews(request=request, vendor_id=vendor_id, user=user, db=db)


@router.delete("/v2/partials/vendors/{vendor_id}/reviews/{review_id}", response_class=HTMLResponse)
async def delete_vendor_review(
    request: Request,
    vendor_id: int,
    review_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Delete a vendor review (own reviews only) and return refreshed reviews."""
    from ..models import VendorReview

    review = (
        db.query(VendorReview).filter(VendorReview.id == review_id, VendorReview.vendor_card_id == vendor_id).first()
    )
    if not review:
        raise HTTPException(404, "Review not found")
    if review.user_id != user.id:
        raise HTTPException(403, "Can only delete your own reviews")

    db.delete(review)
    db.commit()

    return await vendor_reviews(request=request, vendor_id=vendor_id, user=user, db=db)


# ── AI Contact Finder actions (Phase 3A) ───────────────────────────────


@router.post("/v2/partials/vendors/{vendor_id}/ai/find-contacts", response_class=HTMLResponse)
async def vendor_find_contacts(
    request: Request,
    vendor_id: int,
    title_keywords: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Trigger AI web search for contacts at this vendor, return HTML results."""
    vendor = get_vendor_card_or_404(db, vendor_id)

    from ..config import settings as app_settings

    # Check AI feature gate
    ai_flag = app_settings.ai_features_enabled
    if ai_flag == "off":
        return HTMLResponse(
            '<div class="p-4 text-center text-sm text-amber-600 bg-amber-50 rounded-lg border border-amber-200">'
            "AI features are currently disabled. Contact your admin to enable them.</div>"
        )

    ctx = _base_ctx(request, user, "vendors")
    ctx["vendor"] = vendor

    try:
        from app.services.ai_service import enrich_contacts_websearch

        keywords = title_keywords.strip() if title_keywords else None
        web_results = await enrich_contacts_websearch(vendor.display_name, vendor.domain, keywords, limit=10)

        # Dedup and save as ProspectContact records
        seen: set[str] = set()
        new_count = 0
        for c in web_results:
            email = (c.get("email") or "").lower()
            key = email if email else c.get("full_name", "").lower()
            if key and key in seen:
                continue
            seen.add(key)

            pc = ProspectContact(
                vendor_card_id=vendor_id,
                full_name=c["full_name"],
                title=c.get("title"),
                email=c.get("email"),
                email_status=c.get("email_status"),
                phone=c.get("phone"),
                linkedin_url=c.get("linkedin_url"),
                source=c.get("source", "web_search"),
                confidence=c.get("confidence", "low"),
            )
            db.add(pc)
            new_count += 1

        db.commit()
    except Exception as exc:
        logger.error(f"AI contact finder error for vendor {vendor_id}: {exc}")
        return HTMLResponse(
            '<div class="p-4 text-center text-sm text-rose-600 bg-rose-50 rounded-lg border border-rose-200">'
            f"AI search failed: {exc}</div>"
        )

    # Reload all prospects for this vendor
    prospects = (
        db.query(ProspectContact)
        .filter(ProspectContact.vendor_card_id == vendor_id)
        .order_by(ProspectContact.created_at.desc())
        .limit(50)
        .all()
    )
    ctx["prospects"] = prospects
    ctx["search_count"] = new_count
    return template_response("htmx/partials/vendors/find_contacts_results.html", ctx)


@router.post(
    "/v2/partials/vendors/{vendor_id}/ai/prospect/{prospect_id}/save",
    response_class=HTMLResponse,
)
async def vendor_prospect_save(
    request: Request,
    vendor_id: int,
    prospect_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Mark a prospect contact as saved."""
    pc = db.query(ProspectContact).filter(ProspectContact.id == prospect_id).first()
    if not pc:
        raise HTTPException(404, "Prospect contact not found")

    pc.is_saved = True
    pc.saved_by_id = user.id
    db.commit()

    vendor = db.query(VendorCard).filter(VendorCard.id == vendor_id).first()
    ctx = _base_ctx(request, user, "vendors")
    ctx["vendor"] = vendor
    ctx["p"] = pc
    return template_response("htmx/partials/vendors/prospect_card.html", ctx)


@router.post(
    "/v2/partials/vendors/{vendor_id}/ai/prospect/{prospect_id}/promote",
    response_class=HTMLResponse,
)
async def vendor_prospect_promote(
    request: Request,
    vendor_id: int,
    prospect_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Promote a prospect contact to a VendorContact."""
    pc = db.query(ProspectContact).filter(ProspectContact.id == prospect_id).first()
    if not pc:
        raise HTTPException(404, "Prospect contact not found")

    # Dedup: check if email already exists on this vendor
    existing = None
    if pc.email:
        existing = db.query(VendorContact).filter_by(vendor_card_id=vendor_id, email=pc.email).first()

    if existing:
        if pc.full_name and not existing.full_name:
            existing.full_name = pc.full_name
        if pc.title and not existing.title:
            existing.title = pc.title
        if pc.phone and not existing.phone:
            existing.phone = pc.phone
        if pc.linkedin_url and not existing.linkedin_url:
            existing.linkedin_url = pc.linkedin_url
        vc = existing
    else:
        vc = VendorContact(
            vendor_card_id=vendor_id,
            full_name=pc.full_name,
            title=pc.title,
            email=pc.email,
            phone=pc.phone,
            linkedin_url=pc.linkedin_url,
            source="prospect_promote",
        )
        db.add(vc)
        db.flush()

    pc.promoted_to_type = "vendor_contact"
    pc.promoted_to_id = vc.id
    pc.is_saved = True
    pc.saved_by_id = user.id
    db.commit()

    vendor = db.query(VendorCard).filter(VendorCard.id == vendor_id).first()
    ctx = _base_ctx(request, user, "vendors")
    ctx["vendor"] = vendor
    ctx["p"] = pc
    return template_response("htmx/partials/vendors/prospect_card.html", ctx)


@router.delete(
    "/v2/partials/vendors/{vendor_id}/ai/prospect/{prospect_id}",
    response_class=HTMLResponse,
)
async def vendor_prospect_delete(
    request: Request,
    vendor_id: int,
    prospect_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Delete a prospect contact."""
    pc = db.query(ProspectContact).filter(ProspectContact.id == prospect_id).first()
    if not pc:
        raise HTTPException(404, "Prospect contact not found")
    db.delete(pc)
    db.commit()
    # Return empty string to remove the card from DOM
    return HTMLResponse("")


# ── Company/Customer partials ──────────────────────────────────────────


# Redirect old /v2/companies URLs to /v2/customers
@router.get("/v2/companies", response_class=HTMLResponse)
@router.get("/v2/companies/{path:path}", response_class=HTMLResponse)
async def companies_redirect(request: Request, path: str = ""):
    """Redirect old /v2/companies URLs to /v2/customers."""
    from fastapi.responses import RedirectResponse

    new_url = f"/v2/customers/{path}" if path else "/v2/customers"
    if request.url.query:
        new_url += f"?{request.url.query}"
    return RedirectResponse(url=new_url, status_code=301)


# Redirect old /v2/partials/companies URLs to /v2/partials/customers
@router.get("/v2/partials/companies", response_class=HTMLResponse)
@router.get("/v2/partials/companies/{path:path}", response_class=HTMLResponse)
async def partials_companies_redirect(request: Request, path: str = ""):
    """Redirect old /v2/partials/companies URLs to /v2/partials/customers."""
    from fastapi.responses import RedirectResponse

    new_url = f"/v2/partials/customers/{path}" if path else "/v2/partials/customers"
    if request.url.query:
        new_url += f"?{request.url.query}"
    return RedirectResponse(url=new_url, status_code=301)


# CDM staleness rules + account-list query building live in the service layer
# (app/services/crm_service.py). _cdm_list_ctx and _company_contact_rows are
# plain service imports used by the CDM routes below. _staleness_tier is
# additionally re-exported under its historical name because tests
# (tests/test_htmx_views_nightly28.py) import it from this module — the F401
# keeps ruff from stripping that alias.
from ..services.crm_service import cadence_state as _cadence_state  # noqa: E402
from ..services.crm_service import cdm_list_ctx as _cdm_list_ctx  # noqa: E402
from ..services.crm_service import company_commercial_stats as _company_commercial_stats  # noqa: E402
from ..services.crm_service import company_contact_rows as _company_contact_rows  # noqa: E402
from ..services.crm_service import next_best_touch as _next_best_touch  # noqa: E402
from ..services.crm_service import order_by_clock as _order_by_clock  # noqa: E402
from ..services.crm_service import staleness_tier as _staleness_tier  # noqa: E402, F401
from ..services.tagging import (  # noqa: E402
    assign_segment_tag as _assign_segment_tag,
)
from ..services.tagging import (
    get_or_create_segment_tag as _get_or_create_segment_tag,
)
from ..services.tagging import (
    list_all_segment_tags as _list_all_segment_tags,
)
from ..services.tagging import (
    list_company_segment_tags as _list_company_segment_tags,
)
from ..services.tagging import (
    unassign_segment_tag as _unassign_segment_tag,
)

_ALLOWED_HX_TARGETS = {"#main-content", "#crm-tab-content"}
_ALLOWED_PUSH_URL_BASES = {"/v2/vendors", "/v2/customers", "/v2/crm"}


def _sanitize_hx_params(hx_target: str, push_url_base: str, default_push: str) -> tuple[str, str]:
    """Validate hx_target and push_url_base against allowlists."""
    if hx_target not in _ALLOWED_HX_TARGETS:
        hx_target = "#main-content"
    if push_url_base not in _ALLOWED_PUSH_URL_BASES:
        push_url_base = default_push
    return hx_target, push_url_base


@router.get("/v2/partials/customers", response_class=HTMLResponse)
async def companies_list_partial(
    request: Request,
    search: str = "",
    staleness: str = "",
    account_type: str = "",
    my_only: bool = False,
    sort: str = "oldest",
    segment: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return the CDM account workspace (split-panel list + detail) as HTML partial."""
    ctx = _base_ctx(request, user, "customers")
    ctx.update(
        _cdm_list_ctx(
            db,
            user,
            search=search,
            staleness=staleness,
            account_type=account_type,
            my_only=my_only,
            sort=sort,
            segment=segment,
            limit=limit,
            offset=offset,
            include_overdue=True,
        )
    )
    return template_response("htmx/partials/customers/list.html", ctx)


@router.get("/v2/partials/customers/account-list", response_class=HTMLResponse)
async def companies_account_list_partial(
    request: Request,
    search: str = "",
    staleness: str = "",
    account_type: str = "",
    my_only: bool = False,
    sort: str = "oldest",
    segment: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return only the CDM left-panel account list (filter/sort/pagination refresh).

    The overdue chip lives in the filter bar (not re-rendered here), so this route skips
    the overdue COUNT query.
    """
    ctx = {"request": request, "user": user}
    ctx.update(
        _cdm_list_ctx(
            db,
            user,
            search=search,
            staleness=staleness,
            account_type=account_type,
            my_only=my_only,
            sort=sort,
            segment=segment,
            limit=limit,
            offset=offset,
        )
    )
    return template_response("htmx/partials/customers/_account_list.html", ctx)


# ── Sprint 4: Company CRUD (static routes — must precede {company_id}) ──


@router.get("/v2/partials/customers/create-form", response_class=HTMLResponse)
async def company_create_form(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return create company form."""
    users = (
        db.query(User).filter(User.role.in_((UserRole.BUYER, UserRole.TRADER, UserRole.MANAGER, UserRole.ADMIN))).all()
    )
    return template_response(
        "htmx/partials/customers/create_form.html",
        {"request": request, "users": users},
    )


@router.post("/v2/partials/customers/create", response_class=HTMLResponse)
async def create_company(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Create a new company and redirect to its detail page."""
    form = await request.form()
    name = form.get("name", "").strip()
    if not name:
        raise HTTPException(400, "Company name is required")

    # Duplicate check
    existing = db.query(Company).filter(sqlfunc.lower(Company.name) == name.lower()).first()
    if existing:
        raise HTTPException(409, f"Company '{existing.name}' already exists (ID {existing.id})")

    company = Company(
        name=name,
        website=form.get("website", "").strip() or None,
        industry=form.get("industry", "").strip() or None,
        notes=form.get("notes", "").strip() or None,
        is_active=True,
    )
    owner_id = form.get("owner_id", "")
    if owner_id and owner_id.isdigit():
        company.account_owner_id = int(owner_id)
    db.add(company)
    db.flush()

    # Auto-create default site
    default_site = CustomerSite(
        company_id=company.id,
        site_name="HQ",
        site_type="headquarters",
        is_active=True,
    )
    db.add(default_site)
    db.commit()
    logger.info("Company {} created by {}", company.id, user.email)

    return await company_detail_partial(request=request, company_id=company.id, user=user, db=db)


@router.get("/v2/partials/customers/typeahead", response_class=HTMLResponse)
async def company_typeahead(
    request: Request,
    q: str = "",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return company typeahead results as HTML options."""
    if not q.strip() or len(q.strip()) < 2:
        return HTMLResponse("")

    sb = SearchBuilder(q.strip())
    companies = (
        db.query(Company)
        .filter(Company.is_active.is_(True), sb.ilike_filter(Company.name))
        .order_by(Company.name)
        .limit(10)
        .all()
    )
    rows = [f'<option value="{c.id}">{c.name}</option>' for c in companies]
    return HTMLResponse("\n".join(rows))


@router.get("/v2/partials/customers/check-duplicate", response_class=HTMLResponse)
async def check_company_duplicate(
    request: Request,
    name: str = "",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Check for duplicate company name, return warning HTML if found."""
    if not name.strip():
        return HTMLResponse("")

    existing = (
        db.query(Company)
        .filter(
            Company.is_active.is_(True),
            sqlfunc.lower(Company.name) == name.strip().lower(),
        )
        .first()
    )
    if existing:
        return HTMLResponse(
            f'<p class="text-sm text-amber-600">A company named "{existing.name}" already exists (ID {existing.id}).</p>'
        )
    return HTMLResponse("")


def build_account_timeline(contacts, quotes, activities, *, req_map):
    """Merge RFQ contacts, quotes, and activity logs into a single sorted list.

    Each event dict has the shape:
        {ts, kind, channel, direction, title, detail, is_meaningful,
         quality_score, quality_classification, raw}

    ``kind`` is one of "rfq" | "quote" | "activity".
    Events are sorted descending by ``ts`` (newest first).
    ``raw`` carries the original ORM object for template use.

    Called by: company_tab (activity branch) in htmx_views.py.
    """
    from datetime import datetime, timezone

    _epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    events: list[dict] = []

    for c in contacts or []:
        ts = c.created_at or _epoch
        events.append(
            {
                "ts": ts,
                "kind": "rfq",
                "channel": "email",
                "direction": "outbound",
                "title": c.vendor_name or "Unknown Vendor",
                "detail": c.subject or "",
                "is_meaningful": True,
                "quality_score": None,
                "quality_classification": None,
                "raw": c,
                "req": req_map.get(c.requisition_id) if req_map and c.requisition_id else None,
            }
        )

    for q in quotes or []:
        ts = q.created_at or _epoch
        # Use won_revenue for won quotes; fall back to subtotal then total_cost
        if q.status == "won" and q.won_revenue:
            display_value = q.won_revenue
        else:
            display_value = q.subtotal or q.total_cost
        events.append(
            {
                "ts": ts,
                "kind": "quote",
                "channel": "internal",
                "direction": None,
                "title": q.quote_number or "Quote",
                "detail": "${:,.2f}".format(float(display_value)) if display_value else "",
                "is_meaningful": True,
                "quality_score": None,
                "quality_classification": None,
                "raw": q,
                "display_value": display_value,
            }
        )

    for a in activities or []:
        ts = a.occurred_at or a.created_at or _epoch
        events.append(
            {
                "ts": ts,
                "kind": "activity",
                "channel": a.channel,
                "direction": a.direction,
                "title": (a.activity_type or "").replace("_", " ").title(),
                "detail": a.summary or a.notes or a.subject or "",
                "is_meaningful": a.is_meaningful,
                "quality_score": a.quality_score,
                "quality_classification": a.quality_classification,
                "raw": a,
            }
        )

    events.sort(key=lambda e: e["ts"], reverse=True)
    return events


def _company_quotes_query(db: Session, company):
    """Quotes belonging to an account: union of quotes linked via the
    company's customer sites OR via the company's requisitions (the latter
    catches quotes whose customer_site_id is NULL). Returns a Query, or None
    when the account can own no quotes (no sites and no requisitions).
    Called by: company_detail_partial (count), company_tab (quotes + activity).
    """
    site_ids = [s.id for s in db.query(CustomerSite.id).filter(CustomerSite.company_id == company.id).all()]
    req_ids = [
        r.id
        for r in db.query(Requisition.id)
        .filter(
            or_(
                Requisition.company_id == company.id,
                sqlfunc.lower(sqlfunc.trim(Requisition.customer_name)) == company.name.lower().strip(),
            )
        )
        .all()
    ]
    conds = []
    if site_ids:
        conds.append(Quote.customer_site_id.in_(site_ids))
    if req_ids:
        conds.append(Quote.requisition_id.in_(req_ids))
    if not conds:
        return None
    return db.query(Quote).filter(or_(*conds)).options(joinedload(Quote.requisition))


def _company_buy_plans_query(db: Session, company):
    """Buy plans belonging to an account: all buy-plans whose requisition links
    to the company (via company_id FK or customer_name match). Returns a Query,
    or None when the account has no requisitions.
    Called by: company_detail_partial (count), company_tab (buy_plans).
    """
    req_ids = [
        r.id
        for r in db.query(Requisition.id)
        .filter(
            or_(
                Requisition.company_id == company.id,
                sqlfunc.lower(sqlfunc.trim(Requisition.customer_name)) == company.name.lower().strip(),
            )
        )
        .all()
    ]
    if not req_ids:
        return None
    return (
        db.query(BuyPlan)
        .options(joinedload(BuyPlan.lines), joinedload(BuyPlan.requisition))
        .filter(BuyPlan.requisition_id.in_(req_ids))
    )


@router.get("/v2/partials/customers/{company_id}/segment-tags", response_class=HTMLResponse)
async def company_segment_tags_partial(
    request: Request,
    company_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return the segment-tag chips + editor partial for a company."""
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(404, "Company not found")
    tags = _list_company_segment_tags(company_id=company_id, db=db)
    all_segment_tags = _list_all_segment_tags(db=db)
    return template_response(
        "htmx/partials/customers/_segment_tags.html",
        {
            "request": request,
            "company": company,
            "segment_tags": tags,
            "all_segment_tags": all_segment_tags,
        },
    )


@router.post("/v2/partials/customers/{company_id}/segment-tags", response_class=HTMLResponse)
async def company_assign_segment_tag(
    request: Request,
    company_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Assign a segment tag to a company.

    Accepts tag_id= (existing) or tag_name= (creates new).
    """
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(404, "Company not found")

    form = await request.form()
    tag_id_raw = form.get("tag_id", "").strip()
    tag_name_raw = form.get("tag_name", "").strip()

    if tag_name_raw:
        tag = _get_or_create_segment_tag(tag_name_raw, db)
    elif tag_id_raw:
        try:
            tag_id = int(tag_id_raw)
        except ValueError:
            raise HTTPException(400, "tag_id must be an integer")
        from ..models.tags import Tag as _Tag

        tag = db.query(_Tag).filter_by(id=tag_id).first()
        if not tag:
            raise HTTPException(404, "Tag not found")
    else:
        raise HTTPException(400, "Provide tag_id or tag_name")

    _assign_segment_tag(company_id=company_id, tag_id=tag.id, db=db)
    db.commit()

    tags = _list_company_segment_tags(company_id=company_id, db=db)
    all_segment_tags = _list_all_segment_tags(db=db)
    return template_response(
        "htmx/partials/customers/_segment_tags.html",
        {
            "request": request,
            "company": company,
            "segment_tags": tags,
            "all_segment_tags": all_segment_tags,
        },
    )


@router.delete("/v2/partials/customers/{company_id}/segment-tags/{tag_id}", response_class=HTMLResponse)
async def company_unassign_segment_tag(
    request: Request,
    company_id: int,
    tag_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Remove a segment tag from a company."""
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(404, "Company not found")

    _unassign_segment_tag(company_id=company_id, tag_id=tag_id, db=db)
    db.commit()

    tags = _list_company_segment_tags(company_id=company_id, db=db)
    all_segment_tags = _list_all_segment_tags(db=db)
    return template_response(
        "htmx/partials/customers/_segment_tags.html",
        {
            "request": request,
            "company": company,
            "segment_tags": tags,
            "all_segment_tags": all_segment_tags,
        },
    )


_VALID_TIERS = frozenset({"key", "core", "standard", "prospect"})

# Canonical buying-role taxonomy (P2b).  Legacy values (buyer/technical/
# decision_maker/operations) remain valid in the DB but can only be cleared
# via the "— clear —" option; they are not in this set.
CANONICAL_ROLES = ("specifier", "buyer_po", "ap_payer", "logistics", "exec", "other")
_VALID_ROLES = frozenset(CANONICAL_ROLES)


@router.post("/v2/partials/customers/{company_id}/tier", response_class=HTMLResponse)
async def set_company_tier(
    request: Request,
    company_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Set Company.tier; re-renders the cadence hero with updated badge + NBT.

    Accepts tier= from the inline select.  Blank value clears the tier (NULL → behaves
    as 'standard').  Invalid value → 400.
    """
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(404, "Company not found")

    form = await request.form()
    tier_raw = (form.get("tier") or "").strip()

    if tier_raw and tier_raw not in _VALID_TIERS:
        raise HTTPException(400, f"Invalid tier '{tier_raw}'. Valid: {sorted(_VALID_TIERS)}")

    company.tier = tier_raw or None
    db.commit()
    db.refresh(company)

    _cadence = _cadence_state(company.tier, company.last_outbound_at)
    _nbt = _next_best_touch(company.tier, company.last_outbound_at)
    logger.info("Company {} tier set to {} by {}", company_id, company.tier, user.email)
    return template_response(
        "htmx/partials/customers/_cadence_hero.html",
        {
            "request": request,
            "company": company,
            "cadence_state": _cadence,
            "next_best_touch": _nbt,
        },
    )


@router.post(
    "/v2/partials/customers/{company_id}/contacts/{contact_id}/role",
    response_class=HTMLResponse,
)
async def set_contact_role(
    request: Request,
    company_id: int,
    contact_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Set SiteContact.contact_role; re-renders the role chip editor.

    Accepts contact_role= from the inline select.  Blank value clears the role (NULL).
    Invalid value → 400 (legacy values that pre-exist in the DB are not accepted via
    this endpoint; rep must choose a canonical role).
    """
    contact = (
        db.query(SiteContact)
        .join(CustomerSite)
        .filter(SiteContact.id == contact_id, CustomerSite.company_id == company_id)
        .first()
    )
    if not contact:
        raise HTTPException(404, "Contact not found")

    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(404, "Company not found")

    form = await request.form()
    role_raw = (form.get("contact_role") or "").strip()

    if role_raw and role_raw not in _VALID_ROLES:
        raise HTTPException(400, f"Invalid contact_role '{role_raw}'. Valid: {sorted(_VALID_ROLES)}")

    contact.contact_role = role_raw or None
    db.commit()
    db.refresh(contact)

    logger.info(
        "Contact {} role set to {} by {} (company {})",
        contact_id,
        contact.contact_role,
        user.email,
        company_id,
    )
    return template_response(
        "htmx/partials/customers/_role_chip_editor.html",
        {
            "request": request,
            "company": company,
            "contact": contact,
        },
    )


@router.post(
    "/v2/partials/customers/{company_id}/contacts/{contact_id}/do-not-contact",
    response_class=HTMLResponse,
)
async def set_contact_dnc(
    request: Request,
    company_id: int,
    contact_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Set or clear SiteContact.do_not_contact; re-renders the DNC toggle partial.

    Accepts do_not_contact= from the inline form.  Non-empty value → True. Empty string
    → False (clear the flag).
    """
    contact = (
        db.query(SiteContact)
        .join(CustomerSite)
        .filter(SiteContact.id == contact_id, CustomerSite.company_id == company_id)
        .first()
    )
    if not contact:
        raise HTTPException(404, "Contact not found")

    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(404, "Company not found")

    form = await request.form()
    dnc_raw = (form.get("do_not_contact") or "").strip()

    contact.do_not_contact = bool(dnc_raw)
    db.commit()
    db.refresh(contact)

    logger.info(
        "Contact {} do_not_contact set to {} by {} (company {})",
        contact_id,
        contact.do_not_contact,
        user.email,
        company_id,
    )
    return template_response(
        "htmx/partials/customers/_dnc_toggle.html",
        {
            "request": request,
            "company": company,
            "contact": contact,
        },
    )


@router.get("/v2/partials/customers/{company_id}", response_class=HTMLResponse)
async def company_detail_partial(
    request: Request,
    company_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return company detail as HTML partial with tabs."""
    company = (
        db.query(Company)
        .options(joinedload(Company.account_owner), joinedload(Company.sites))
        .filter(Company.id == company_id)
        .first()
    )
    if not company:
        raise HTTPException(404, "Company not found")

    sites = [s for s in (company.sites or []) if s.is_active]

    # Count open requisitions — use company_id FK if available, fall back to name match
    from sqlalchemy import or_

    open_req_count = (
        db.query(sqlfunc.count(Requisition.id))
        .filter(
            or_(
                Requisition.company_id == company.id,
                sqlfunc.lower(sqlfunc.trim(Requisition.customer_name)) == company.name.lower().strip(),
            ),
            Requisition.status.in_(
                [
                    RequisitionStatus.ACTIVE,
                    RequisitionStatus.SOURCING,
                    RequisitionStatus.DRAFT,
                ]
            ),
        )
        .scalar()
        or 0
    )

    _cq = _company_quotes_query(db, company)
    quote_count = _cq.count() if _cq is not None else 0

    _bpq = _company_buy_plans_query(db, company)
    buy_plan_count = _bpq.count() if _bpq is not None else 0

    # Cadence card + commercial strip context
    from datetime import timezone as _tz

    _stats = _company_commercial_stats(db, [company.id]).get(company.id, {})
    _cadence = _cadence_state(company.tier, company.last_outbound_at)
    _nbt = _next_best_touch(company.tier, company.last_outbound_at)
    contact_rows = _company_contact_rows(db, company_id, sites=sites)
    segment_tags = _list_company_segment_tags(company_id=company_id, db=db)
    all_segment_tags = _list_all_segment_tags(db=db)

    ctx = _base_ctx(request, user, "customers")
    ctx.update(
        {
            "company": company,
            "sites": sites,
            "open_req_count": open_req_count,
            "quote_count": quote_count,
            "buy_plan_count": buy_plan_count,
            # Pass the active-only sites list — contacts on deactivated sites must
            # not be shown (clicking them would log outreach against, and bump,
            # a deactivated entity).
            "contact_rows": contact_rows,
            "user": user,
            # Cadence card
            "cadence_state": _cadence,
            "next_best_touch": _nbt,
            "contact_count": len(contact_rows),
            "site_count": len(sites),
            # Commercial strip
            "win_rate": _stats.get("win_rate"),
            "revenue_90d": _stats.get("revenue_90d", 0.0),
            "last_req_date": _stats.get("last_req_date"),
            # Clock day calculations
            "now_utc": datetime.now(_tz.utc),
            # Segment tags
            "segment_tags": segment_tags,
            "all_segment_tags": all_segment_tags,
        }
    )
    return template_response("htmx/partials/customers/detail.html", ctx)


@router.get("/v2/partials/customers/{company_id}/tab/{tab}", response_class=HTMLResponse)
async def company_tab(
    request: Request,
    company_id: int,
    tab: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return a specific tab partial for company detail."""
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(404, "Company not found")

    valid_tabs = {"sites", "contacts", "requisitions", "activity", "quotes", "buy_plans"}
    if tab not in valid_tabs:
        raise HTTPException(404, f"Unknown tab: {tab}")

    if tab == "sites":
        from sqlalchemy.orm import joinedload

        sites = (
            db.query(CustomerSite)
            .options(joinedload(CustomerSite.owner))
            .filter(CustomerSite.company_id == company_id, CustomerSite.is_active.is_(True))
            .all()
        )
        users = db.query(User).order_by(User.name).all()
        ctx = _base_ctx(request, user, "customers")
        ctx["company"] = company
        ctx["sites"] = sites
        ctx["users"] = users
        return template_response("htmx/partials/customers/tabs/sites_tab.html", ctx)

    elif tab == "contacts":
        from datetime import timezone as _tz

        ctx = _base_ctx(request, user, "customers")
        ctx.update(
            {
                "company": company,
                "contact_rows": _company_contact_rows(db, company_id),
                "now_utc": datetime.now(_tz.utc),
            }
        )
        return template_response("htmx/partials/customers/tabs/contacts_tab.html", ctx)

    elif tab == "requisitions":
        from sqlalchemy import or_

        reqs = (
            db.query(Requisition)
            .filter(
                or_(
                    Requisition.company_id == company.id,
                    sqlfunc.lower(sqlfunc.trim(Requisition.customer_name)) == company.name.lower().strip(),
                )
            )
            .order_by(Requisition.created_at.desc().nullslast())
            .limit(50)
            .all()
        )
        rows = []
        for r in reqs:
            date_str = r.created_at.strftime("%b %d, %Y") if r.created_at else "\u2014"
            rows.append(f"""<tr class="hover:bg-brand-50 cursor-pointer"
                hx-get="/v2/partials/requisitions/{r.id}"
                hx-target="#main-content"
                hx-push-url="/v2/requisitions/{r.id}">
              <td class="px-4 py-2 text-sm font-medium text-brand-500">{r.name}</td>
              <td class="px-4 py-2 text-sm text-gray-500">{r.status or _DASH}</td>
              <td class="px-4 py-2 text-sm text-gray-500">{date_str}</td>
            </tr>""")
        if rows:
            html = f"""<div class="overflow-x-auto">
              <table class="min-w-full divide-y divide-gray-200">
                <thead class="bg-gray-50">
                  <tr>
                    <th class="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Name</th>
                    <th class="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Status</th>
                    <th class="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Created</th>
                  </tr>
                </thead>
                <tbody class="divide-y divide-gray-200">{"".join(rows)}</tbody>
              </table>
            </div>"""
        else:
            html = '<div class="p-8 text-center"><p class="text-sm text-gray-500">No requisitions for this company.</p></div>'
        return HTMLResponse(html)

    elif tab == "quotes":
        cq = _company_quotes_query(db, company)
        quotes = cq.order_by(Quote.created_at.desc().nullslast()).all() if cq is not None else []
        ctx = _base_ctx(request, user, "customers")
        ctx.update({"company": company, "quotes": quotes})
        return template_response("htmx/partials/customers/tabs/quotes_tab.html", ctx)

    elif tab == "buy_plans":
        bpq = _company_buy_plans_query(db, company)
        buy_plans = bpq.order_by(BuyPlan.created_at.desc().nullslast()).all() if bpq is not None else []
        ctx = _base_ctx(request, user, "customers")
        ctx.update({"company": company, "buy_plans": buy_plans})
        return template_response("htmx/partials/customers/tabs/buy_plans_tab.html", ctx)

    else:  # activity
        from sqlalchemy import or_ as or_clause

        from ..models.intelligence import ActivityLog
        from ..models.offers import Contact as RfqContact

        # Find all requisition IDs linked to this company (via FK or name match)
        req_ids = [
            r.id
            for r in db.query(Requisition.id)
            .filter(
                or_clause(
                    Requisition.company_id == company.id,
                    sqlfunc.lower(sqlfunc.trim(Requisition.customer_name)) == company.name.lower().strip(),
                )
            )
            .all()
        ]

        # RFQ contacts across company's requisitions
        contacts = []
        if req_ids:
            contacts = (
                db.query(RfqContact)
                .filter(RfqContact.requisition_id.in_(req_ids))
                .order_by(RfqContact.created_at.desc())
                .limit(30)
                .all()
            )
        # Build req_map for backlinks
        req_map = {}
        if contacts:
            linked_req_ids = {c.requisition_id for c in contacts}
            for r in db.query(Requisition).filter(Requisition.id.in_(linked_req_ids)).all():
                req_map[r.id] = r

        # Quotes for this account — union of site-linked and requisition-linked
        # (matches the Quotes tab; site link alone misses NULL-site quotes).
        cq = _company_quotes_query(db, company)
        quotes = cq.order_by(Quote.created_at.desc().nullslast()).limit(20).all() if cq is not None else []

        # Direct activity logs on this company + its requisitions
        activity_filters = [ActivityLog.company_id == company.id]
        if req_ids:
            activity_filters.append(ActivityLog.requisition_id.in_(req_ids))
        activities = (
            db.query(ActivityLog)
            .filter(or_clause(*activity_filters))
            .order_by(ActivityLog.created_at.desc())
            .limit(30)
            .all()
        )

        # Merge all three sources into a single chronological timeline
        timeline = build_account_timeline(contacts, quotes, activities, req_map=req_map)

        # Compute truncation flag: True if ANY source hit its limit
        # (RFQ .limit(30), quotes .limit(20), activities .limit(30))
        timeline_truncated = len(contacts) >= 30 or len(quotes) >= 20 or len(activities) >= 30

        # Load email intelligence for email activities (retained for potential future use)
        email_external_ids = [a.external_id for a in activities if a.external_id and a.channel == "email"]
        email_intel_map: dict = {}
        if email_external_ids:
            from ..models.email_intelligence import EmailIntelligence

            ei_records = db.query(EmailIntelligence).filter(EmailIntelligence.message_id.in_(email_external_ids)).all()
            email_intel_map = {ei.message_id: ei for ei in ei_records}

        ctx = _base_ctx(request, user, "customers")
        ctx.update(
            {
                "company": company,
                "timeline": timeline,
                "timeline_truncated": timeline_truncated,
                # Keep raw lists available for summary counts
                "contacts": contacts,
                "quotes": quotes,
                "activities": activities,
                "req_map": req_map,
                "email_intel_map": email_intel_map,
            }
        )
        return template_response("htmx/partials/customers/tabs/activity_tab.html", ctx)


# ── Sites & Site Contacts CRUD (Phase 4) ───────────────────────────────


@router.post("/v2/partials/customers/{company_id}/sites", response_class=HTMLResponse)
async def create_site(
    request: Request,
    company_id: int,
    site_name: str = Form(""),
    site_type: str = Form(""),
    city: str = Form(""),
    country: str = Form(""),
    owner_id: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Create a new site for a company, return the site card partial."""
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(404, "Company not found")

    if not site_name.strip():
        return HTMLResponse('<div class="p-2 text-xs text-rose-600">Site name is required.</div>')

    # Enforce one-owner-per-site rule: each user can only own one site
    try:
        parsed_owner_id = int(owner_id) if owner_id else None
    except (ValueError, TypeError):
        parsed_owner_id = None
    if parsed_owner_id:
        existing = (
            db.query(CustomerSite)
            .filter(
                CustomerSite.owner_id == parsed_owner_id,
            )
            .first()
        )
        if existing:
            owner_user = db.get(User, parsed_owner_id)
            owner_name = owner_user.display_name if owner_user else f"User #{parsed_owner_id}"
            return HTMLResponse(
                f'<div class="p-2 text-xs text-rose-600">{owner_name} already owns site "{existing.site_name}". Each user can only own one site.</div>'
            )

    site = CustomerSite(
        company_id=company_id,
        site_name=site_name.strip(),
        site_type=site_type or None,
        city=city or None,
        country=country or None,
        owner_id=parsed_owner_id,
        is_active=True,
    )
    db.add(site)
    db.commit()
    db.refresh(site)

    # Eager load owner for template
    if site.owner_id:
        _ = site.owner

    ctx = _base_ctx(request, user, "customers")
    ctx["company"] = company
    ctx["s"] = site
    return template_response("htmx/partials/customers/tabs/site_card.html", ctx)


@router.delete("/v2/partials/customers/{company_id}/sites/{site_id}", response_class=HTMLResponse)
async def delete_site(
    request: Request,
    company_id: int,
    site_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Soft-delete a site (set is_active=False)."""
    site = db.query(CustomerSite).filter(CustomerSite.id == site_id, CustomerSite.company_id == company_id).first()
    if not site:
        raise HTTPException(404, "Site not found")
    site.is_active = False
    db.commit()
    return HTMLResponse("")


@router.get(
    "/v2/partials/customers/{company_id}/sites/{site_id}/contacts",
    response_class=HTMLResponse,
)
async def site_contacts_list(
    request: Request,
    company_id: int,
    site_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Load contacts for a specific site."""
    site = db.query(CustomerSite).filter(CustomerSite.id == site_id, CustomerSite.company_id == company_id).first()
    if not site:
        raise HTTPException(404, "Site not found")

    contacts = (
        db.query(SiteContact)
        .filter(SiteContact.customer_site_id == site_id, SiteContact.is_active.is_(True))
        .order_by(SiteContact.is_primary.desc(), SiteContact.full_name)
        .all()
    )
    company = db.query(Company).filter(Company.id == company_id).first()
    ctx = _base_ctx(request, user, "customers")
    ctx["site"] = site
    ctx["contacts"] = contacts
    ctx["company"] = company
    return template_response("htmx/partials/customers/tabs/site_contacts.html", ctx)


@router.post(
    "/v2/partials/customers/{company_id}/sites/{site_id}/contacts",
    response_class=HTMLResponse,
)
async def create_site_contact(
    request: Request,
    company_id: int,
    site_id: int,
    full_name: str = Form(""),
    email: str = Form(""),
    title: str = Form(""),
    phone: str = Form(""),
    wechat_id: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Create a site contact and return refreshed contacts list."""
    site = db.query(CustomerSite).filter(CustomerSite.id == site_id, CustomerSite.company_id == company_id).first()
    if not site:
        raise HTTPException(404, "Site not found")

    if not full_name.strip():
        return HTMLResponse('<div class="p-2 text-xs text-rose-600">Name is required.</div>')

    # SiteContact.wechat_id is String(100) — reject over-length input here (the
    # in-memory SQLite test engine ignores VARCHAR lengths, but Postgres 500s).
    if len(wechat_id.strip()) > 100:
        return HTMLResponse('<div class="p-2 text-xs text-rose-600">WeChat ID must be 100 characters or fewer.</div>')

    # Dedup by email
    if email:
        from sqlalchemy import func

        existing = (
            db.query(SiteContact)
            .filter(
                SiteContact.customer_site_id == site_id,
                func.lower(SiteContact.email) == email.strip().lower(),
            )
            .first()
        )
        if existing:
            # Already exists — just return the list
            pass
        else:
            contact = SiteContact(
                customer_site_id=site_id,
                full_name=full_name.strip(),
                email=email.strip() or None,
                title=title.strip() or None,
                phone=phone.strip() or None,
                wechat_id=wechat_id.strip() or None,
            )
            db.add(contact)
            db.commit()
    else:
        contact = SiteContact(
            customer_site_id=site_id,
            full_name=full_name.strip(),
            title=title.strip() or None,
            phone=phone.strip() or None,
            wechat_id=wechat_id.strip() or None,
        )
        db.add(contact)
        db.commit()

    # Return refreshed contacts list
    contacts = (
        db.query(SiteContact)
        .filter(SiteContact.customer_site_id == site_id, SiteContact.is_active.is_(True))
        .order_by(SiteContact.is_primary.desc(), SiteContact.full_name)
        .all()
    )
    company = db.query(Company).filter(Company.id == company_id).first()
    ctx = _base_ctx(request, user, "customers")
    ctx["site"] = site
    ctx["contacts"] = contacts
    ctx["company"] = company
    return template_response("htmx/partials/customers/tabs/site_contacts.html", ctx)


@router.delete(
    "/v2/partials/customers/{company_id}/sites/{site_id}/contacts/{contact_id}",
    response_class=HTMLResponse,
)
async def delete_site_contact(
    request: Request,
    company_id: int,
    site_id: int,
    contact_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Delete a site contact."""
    contact = (
        db.query(SiteContact).filter(SiteContact.id == contact_id, SiteContact.customer_site_id == site_id).first()
    )
    if not contact:
        raise HTTPException(404, "Contact not found")
    db.delete(contact)
    db.commit()
    return HTMLResponse("")


@router.post(
    "/v2/partials/customers/{company_id}/sites/{site_id}/contacts/{contact_id}/primary",
    response_class=HTMLResponse,
)
async def set_primary_contact(
    request: Request,
    company_id: int,
    site_id: int,
    contact_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Set a contact as primary for the site (unsets others)."""
    # Validate the site belongs to the company BEFORE mutating — a mismatched
    # URL must not flip the primary flag and then 500 on render.
    site = db.query(CustomerSite).filter(CustomerSite.id == site_id, CustomerSite.company_id == company_id).first()
    if not site:
        raise HTTPException(404, "Site not found")
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(404, "Company not found")

    contact = (
        db.query(SiteContact).filter(SiteContact.id == contact_id, SiteContact.customer_site_id == site_id).first()
    )
    if not contact:
        raise HTTPException(404, "Contact not found")

    # Unset all other primary contacts on this site
    db.query(SiteContact).filter(
        SiteContact.customer_site_id == site_id,
        SiteContact.is_primary.is_(True),
        SiteContact.id != contact_id,
    ).update({"is_primary": False})
    contact.is_primary = True
    db.commit()

    # Return refreshed contacts list
    contacts = (
        db.query(SiteContact)
        .filter(SiteContact.customer_site_id == site_id, SiteContact.is_active.is_(True))
        .order_by(SiteContact.is_primary.desc(), SiteContact.full_name)
        .all()
    )
    ctx = _base_ctx(request, user, "customers")
    ctx["site"] = site
    ctx["contacts"] = contacts
    ctx["company"] = company
    return template_response("htmx/partials/customers/tabs/site_contacts.html", ctx)


# ── Sprint 4: Company CRUD (parameterized routes) ──────────────────────


@router.get("/v2/partials/customers/{company_id}/edit-form", response_class=HTMLResponse)
async def company_edit_form(
    request: Request,
    company_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return inline edit form for company fields."""
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(404, "Company not found")
    users = (
        db.query(User).filter(User.role.in_((UserRole.BUYER, UserRole.TRADER, UserRole.MANAGER, UserRole.ADMIN))).all()
    )
    return template_response(
        "htmx/partials/customers/edit_form.html",
        {"request": request, "company": company, "users": users},
    )


@router.post("/v2/partials/customers/{company_id}/edit", response_class=HTMLResponse)
async def edit_company(
    request: Request,
    company_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Save company edits and return refreshed detail."""
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(404, "Company not found")

    form = await request.form()
    name = form.get("name", "").strip()
    if name:
        company.name = name
    website = form.get("website", "").strip()
    if website:
        company.website = website
    industry = form.get("industry", "").strip()
    company.industry = industry or company.industry
    notes = form.get("notes", "").strip()
    company.notes = notes or company.notes

    owner_id = form.get("owner_id", "")
    if owner_id and owner_id.isdigit():
        company.account_owner_id = int(owner_id)

    company.updated_at = datetime.now(timezone.utc)
    db.commit()
    logger.info("Company {} edited by {}", company_id, user.email)

    return await company_detail_partial(request=request, company_id=company_id, user=user, db=db)


# ── Merge Duplicate ─────────────────────────────────────────────────────────


@router.get("/v2/partials/customers/{company_id}/merge-preview", response_class=HTMLResponse)
async def company_merge_preview(
    request: Request,
    company_id: int,
    remove_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return a preview of what will happen when remove_id is merged into company_id.

    Shows counts: sites, contacts, activities that will be reassigned; confirms the
    loser will be deleted. Used by the merge-confirm modal before the user commits.
    """
    from ..models.intelligence import ActivityLog as _AL

    keep = db.query(Company).filter(Company.id == company_id).first()
    if not keep:
        raise HTTPException(404, "Company not found")

    remove = db.query(Company).filter(Company.id == remove_id).first()
    if not remove:
        raise HTTPException(400, "Duplicate company not found")

    if keep.id == remove.id:
        raise HTTPException(400, "Cannot merge a company with itself")

    # Count what will move
    site_count = db.query(sqlfunc.count(CustomerSite.id)).filter(CustomerSite.company_id == remove.id).scalar() or 0
    contact_count = (
        db.query(sqlfunc.count(SiteContact.id))
        .join(CustomerSite, SiteContact.customer_site_id == CustomerSite.id)
        .filter(CustomerSite.company_id == remove.id)
        .scalar()
        or 0
    )
    activity_count = db.query(sqlfunc.count(_AL.id)).filter(_AL.company_id == remove.id).scalar() or 0
    req_count = db.query(sqlfunc.count(Requisition.id)).filter(Requisition.company_id == remove.id).scalar() or 0

    ctx = {
        "request": request,
        "keep": keep,
        "remove": remove,
        "site_count": site_count,
        "contact_count": contact_count,
        "activity_count": activity_count,
        "req_count": req_count,
    }
    return template_response("htmx/partials/customers/_merge_preview.html", ctx)


@router.post("/v2/partials/customers/{company_id}/merge", response_class=HTMLResponse)
async def company_merge(
    request: Request,
    company_id: int,
    remove_id: int = Form(...),
    confirmed: str = Form(default=""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Merge remove_id into company_id (the keeper).

    Requires confirmed="true" to prevent accidental submissions. Calls the
    canonical merge_companies() engine — no FK logic lives here.
    POST is mandatory: this is a destructive, irreversible operation.
    """
    from ..services.company_merge_service import merge_companies as _merge

    if confirmed.lower() != "true":
        raise HTTPException(400, "Merge requires explicit confirmation (confirmed=true)")

    keep = db.query(Company).filter(Company.id == company_id).first()
    if not keep:
        raise HTTPException(404, "Company not found")

    if remove_id == company_id:
        raise HTTPException(400, "Cannot merge a company with itself")

    remove = db.query(Company).filter(Company.id == remove_id).first()
    if not remove:
        raise HTTPException(400, "Duplicate company not found")

    try:
        result = _merge(company_id, remove_id, db)
        db.commit()
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    logger.info(
        "Manual company merge: kept {} ({}), removed {} by {}",
        company_id,
        keep.name,
        remove_id,
        user.email,
    )

    # Redirect browser to keeper's detail page via HTMX redirect header
    safe_name = html_mod.escape(keep.name or "")
    response = HTMLResponse(
        f'<p class="text-sm text-emerald-600 py-2">Merged into <strong>{safe_name}</strong>. '
        f"{int(result.get('sites_moved', 0))} site(s) and {int(result.get('reassigned', 0))} record(s) reassigned.</p>",
        status_code=200,
    )
    response.headers["HX-Redirect"] = f"/v2/partials/customers/{company_id}"
    return response


@router.get("/v2/partials/customers/{company_id}/merge-form", response_class=HTMLResponse)
async def company_merge_form(
    request: Request,
    company_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return the merge-duplicate modal form for a company.

    Renders a search input to find the duplicate and a submit button that triggers the
    merge-preview step.
    """
    keep = db.query(Company).filter(Company.id == company_id).first()
    if not keep:
        raise HTTPException(404, "Company not found")

    ctx = {"request": request, "keep": keep}
    return template_response("htmx/partials/customers/_merge_form.html", ctx)


@router.get("/v2/partials/customers/{company_id}/sites/{site_id}/edit-form", response_class=HTMLResponse)
async def site_edit_form(
    request: Request,
    company_id: int,
    site_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return modal edit form for a customer site."""
    site = db.query(CustomerSite).filter(CustomerSite.id == site_id, CustomerSite.company_id == company_id).first()
    if not site:
        raise HTTPException(404, "Site not found")
    users = db.query(User).order_by(User.name).all()
    return template_response(
        "htmx/partials/customers/tabs/site_edit_modal.html",
        {"request": request, "site": site, "company_id": company_id, "users": users},
    )


@router.post("/v2/partials/customers/{company_id}/sites/{site_id}/edit", response_class=HTMLResponse)
async def edit_site(
    request: Request,
    company_id: int,
    site_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Update site fields and return refreshed sites tab."""
    site = db.query(CustomerSite).filter(CustomerSite.id == site_id, CustomerSite.company_id == company_id).first()
    if not site:
        raise HTTPException(404, "Site not found")

    form = await request.form()
    site_name = form.get("site_name", "").strip()
    if not site_name:
        raise HTTPException(400, "site_name is required")
    site.site_name = site_name
    site.address_line1 = form.get("address_line1", "").strip() or site.address_line1
    site.address_line2 = form.get("address_line2", "").strip() or site.address_line2
    site.city = form.get("city", "").strip() or site.city
    site.state = form.get("state", "").strip() or site.state
    site.zip = form.get("zip", "").strip() or site.zip
    site.country = form.get("country", "").strip() or site.country
    site.site_type = form.get("site_type", "").strip() or site.site_type
    site.payment_terms = form.get("payment_terms", "").strip() or site.payment_terms
    site.shipping_terms = form.get("shipping_terms", "").strip() or site.shipping_terms
    notes_val = form.get("notes", "").strip()
    if notes_val:
        site.notes = notes_val
    owner_id = form.get("owner_id", "")
    if owner_id and str(owner_id).isdigit():
        site.owner_id = int(owner_id)
    site.updated_at = datetime.now(timezone.utc)
    db.commit()
    logger.info("Site {} edited by {}", site_id, user.email)

    return await company_tab(request=request, company_id=company_id, tab="sites", user=user, db=db)


@router.get(
    "/v2/partials/customers/{company_id}/sites/{site_id}/contacts/{contact_id}/edit-form",
    response_class=HTMLResponse,
)
async def contact_edit_form(
    request: Request,
    company_id: int,
    site_id: int,
    contact_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return modal edit form for a site contact."""
    contact = (
        db.query(SiteContact).filter(SiteContact.id == contact_id, SiteContact.customer_site_id == site_id).first()
    )
    if not contact:
        raise HTTPException(404, "Contact not found")
    # Verify site belongs to company
    site = db.query(CustomerSite).filter(CustomerSite.id == site_id, CustomerSite.company_id == company_id).first()
    if not site:
        raise HTTPException(404, "Site not found")
    return template_response(
        "htmx/partials/customers/tabs/contact_edit_modal.html",
        {"request": request, "contact": contact, "site": site, "company_id": company_id},
    )


@router.post(
    "/v2/partials/customers/{company_id}/sites/{site_id}/contacts/{contact_id}/edit",
    response_class=HTMLResponse,
)
async def edit_site_contact(
    request: Request,
    company_id: int,
    site_id: int,
    contact_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Update editable contact fields (never contact_role) and return refreshed contacts
    panel."""
    contact = (
        db.query(SiteContact).filter(SiteContact.id == contact_id, SiteContact.customer_site_id == site_id).first()
    )
    if not contact:
        raise HTTPException(404, "Contact not found")
    site = db.query(CustomerSite).filter(CustomerSite.id == site_id, CustomerSite.company_id == company_id).first()
    if not site:
        raise HTTPException(404, "Site not found")

    form = await request.form()
    full_name = form.get("full_name", "").strip()
    if not full_name:
        raise HTTPException(400, "full_name is required")

    email_val = form.get("email", "").strip()
    if email_val and "@" not in email_val:
        raise HTTPException(400, "Invalid email address")

    contact.full_name = full_name
    contact.title = form.get("title", "").strip() or contact.title
    contact.email = email_val or contact.email
    phone_val = form.get("phone", "").strip()
    if phone_val:
        contact.phone = phone_val
    wechat_val = form.get("wechat_id", "").strip()
    if wechat_val:
        contact.wechat_id = wechat_val
    notes_val = form.get("notes", "").strip()
    if notes_val:
        contact.notes = notes_val
    # contact_role is intentionally NOT updated here — owned by the P2b role setter
    contact.updated_at = datetime.now(timezone.utc)
    db.commit()
    logger.info("Contact {} edited by {}", contact_id, user.email)

    contacts = (
        db.query(SiteContact)
        .filter(SiteContact.customer_site_id == site_id, SiteContact.is_active.is_(True))
        .order_by(SiteContact.is_primary.desc(), SiteContact.full_name)
        .all()
    )
    company = db.query(Company).filter(Company.id == company_id).first()
    ctx = _base_ctx(request, user, "customers")
    ctx["site"] = site
    ctx["contacts"] = contacts
    ctx["company"] = company
    return template_response("htmx/partials/customers/tabs/site_contacts.html", ctx)


@router.post(
    "/v2/partials/customers/{company_id}/sites/{site_id}/contacts/{contact_id}/notes",
    response_class=HTMLResponse,
)
async def add_site_contact_note(
    request: Request,
    company_id: int,
    site_id: int,
    contact_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Add a note to a site contact and return updated notes list."""
    from ..models.intelligence import ActivityLog

    contact = (
        db.query(SiteContact).filter(SiteContact.id == contact_id, SiteContact.customer_site_id == site_id).first()
    )
    if not contact:
        raise HTTPException(404, "Contact not found")

    form = await request.form()
    notes_text = form.get("notes", "").strip()
    if not notes_text:
        raise HTTPException(400, "Notes cannot be empty")

    log = ActivityLog(
        user_id=user.id,
        activity_type=ActivityType.CONTACT_NOTE,
        channel=Channel.MANUAL,
        contact_name=contact.full_name or "",
        contact_email=contact.email or "",
        notes=notes_text,
    )
    db.add(log)
    db.commit()
    logger.info("Note added for site contact {} by {}", contact_id, user.email)

    # Return refreshed notes
    return await get_site_contact_notes(
        request=request,
        company_id=company_id,
        site_id=site_id,
        contact_id=contact_id,
        user=user,
        db=db,
    )


@router.get(
    "/v2/partials/customers/{company_id}/sites/{site_id}/contacts/{contact_id}/notes",
    response_class=HTMLResponse,
)
async def get_site_contact_notes(
    request: Request,
    company_id: int,
    site_id: int,
    contact_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return notes timeline for a site contact."""
    from ..models.intelligence import ActivityLog

    contact = (
        db.query(SiteContact).filter(SiteContact.id == contact_id, SiteContact.customer_site_id == site_id).first()
    )
    if not contact:
        raise HTTPException(404, "Contact not found")

    notes = (
        (
            db.query(ActivityLog)
            .filter(
                ActivityLog.activity_type == "contact_note",
                ActivityLog.contact_email == contact.email,
            )
            .order_by(ActivityLog.created_at.desc())
            .limit(20)
            .all()
        )
        if contact.email
        else []
    )

    return template_response(
        "htmx/partials/customers/contact_notes.html",
        {"request": request, "contact": contact, "notes": notes, "company_id": company_id, "site_id": site_id},
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
    quote = db.query(Quote).options(joinedload(Quote.quote_lines)).filter(Quote.id == quote_id).first()
    if not quote:
        raise HTTPException(404, "Quote not found")

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
    quote = db.query(Quote).filter(Quote.id == quote_id).first()
    if not quote:
        raise HTTPException(404, "Quote not found")
    if quote.status != "draft":
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
    quote = db.query(Quote).filter(Quote.id == quote_id).first()
    if not quote:
        raise HTTPException(404, "Quote not found")
    if quote.status not in ("sent", "won", "lost"):
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
    quote = db.query(Quote).filter(Quote.id == quote_id).first()
    if not quote:
        raise HTTPException(404, "Quote not found")

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
    log = log_call_activity(
        user_id=user.id,
        direction="outbound",
        phone=vendor_phone,
        duration_seconds=None,
        external_id=None,
        contact_name=vendor_name,
        db=db,
        requisition_id=req_id,
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

    stale = (
        db.query(RfqContact)
        .filter(
            RfqContact.contact_type == "email",
            RfqContact.status.in_(["sent", "opened"]),
            RfqContact.created_at < threshold,
        )
        .limit(50)
        .all()
    )

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
    count = (
        db.query(sqlfunc.count(RfqContact.id))
        .filter(
            RfqContact.contact_type == "email",
            RfqContact.status.in_(["sent", "opened"]),
            RfqContact.created_at < threshold,
        )
        .scalar()
        or 0
    )
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
                    RequisitionStatus.ACTIVE,
                    RequisitionStatus.SOURCING,
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


@router.get("/v2/partials/buy-plans", response_class=HTMLResponse)
async def buy_plans_list_partial(
    request: Request,
    q: str = "",
    status: str = "",
    mine: bool = False,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return buy plans list as HTML partial."""
    query = db.query(BuyPlan).options(
        joinedload(BuyPlan.quote),
        joinedload(BuyPlan.requisition),
        joinedload(BuyPlan.submitted_by),
        joinedload(BuyPlan.approved_by),
        joinedload(BuyPlan.lines),
    )

    if status:
        query = query.filter(BuyPlan.status == status)
    if mine:
        query = query.filter(BuyPlan.submitted_by_id == user.id)
    if q.strip():
        sb = SearchBuilder(q.strip())
        query = query.filter(sb.ilike_filter(BuyPlan.sales_order_number, BuyPlan.customer_po_number))

    # Sales users only see their own
    if user.role == UserRole.SALES:
        query = query.filter(BuyPlan.submitted_by_id == user.id)

    plans = query.order_by(BuyPlan.created_at.desc()).limit(200).all()

    # Build lightweight list items
    buy_plans = []
    for p in plans:
        customer_name = None
        if p.quote and p.quote.customer_site:
            site = p.quote.customer_site
            co = site.company if hasattr(site, "company") else None
            customer_name = co.name if co else getattr(site, "site_name", None)

        buy_plans.append(
            {
                "id": p.id,
                "quote_id": p.quote_id,
                "quote_number": p.quote.quote_number if p.quote else None,
                "customer_name": customer_name,
                "sales_order_number": p.sales_order_number,
                "status": p.status,
                "so_status": p.so_status,
                "total_cost": float(p.total_cost) if p.total_cost else 0,
                "total_margin_pct": float(p.total_margin_pct) if p.total_margin_pct else 0,
                "line_count": len(p.lines) if p.lines else 0,
                "submitted_by_name": p.submitted_by.name if p.submitted_by else None,
                "auto_approved": p.auto_approved or False,
                "is_stock_sale": p.is_stock_sale or False,
                "created_at": str(p.created_at) if p.created_at else None,
            }
        )

    ctx = _base_ctx(request, user, "buy-plans")
    ctx.update(
        {
            "buy_plans": buy_plans,
            "q": q,
            "status": status,
            "mine": mine,
            "total": len(buy_plans),
        }
    )
    return template_response("htmx/partials/buy_plans/list.html", ctx)


@router.get("/v2/partials/buy-plans/{plan_id}", response_class=HTMLResponse)
async def buy_plan_detail_partial(
    request: Request,
    plan_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return buy plan detail as HTML partial."""
    bp = (
        db.query(BuyPlan)
        .options(
            joinedload(BuyPlan.lines).joinedload(BuyPlanLine.offer),
            joinedload(BuyPlan.lines).joinedload(BuyPlanLine.requirement),
            joinedload(BuyPlan.lines).joinedload(BuyPlanLine.buyer),
            joinedload(BuyPlan.quote),
            joinedload(BuyPlan.requisition),
            joinedload(BuyPlan.submitted_by),
            joinedload(BuyPlan.approved_by),
        )
        .filter(BuyPlan.id == plan_id)
        .first()
    )
    if not bp:
        raise HTTPException(404, "Buy plan not found")

    ctx = _base_ctx(request, user, "buy-plans")
    ctx.update(
        {
            "bp": bp,
            "lines": bp.lines or [],
            "is_ops_member": _is_ops_member(user, db),
            "user": user,
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
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Manager approves or rejects a pending buy plan — returns refreshed detail."""
    from ..services.buyplan_notifications import (
        notify_approved,
        notify_rejected,
        run_notify_bg,
    )
    from ..services.buyplan_workflow import approve_buy_plan

    form = await request.form()
    action = form.get("action", "approve")

    if user.role not in (UserRole.MANAGER, UserRole.ADMIN):
        raise HTTPException(403, "Manager or admin role required")

    try:
        plan = approve_buy_plan(plan_id, action, user, db, notes=form.get("notes"))
        db.commit()
        if action == "approve":
            await run_notify_bg(notify_approved, plan.id)
        else:
            await run_notify_bg(notify_rejected, plan.id)
    except (ValueError, PermissionError) as e:
        raise HTTPException(400, str(e))

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

    return await buy_plan_detail_partial(request, plan_id, user, db)


@router.post("/v2/partials/buy-plans/{plan_id}/lines/{line_id}/confirm-po", response_class=HTMLResponse)
async def buy_plan_confirm_po_partial(
    request: Request,
    plan_id: int,
    line_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Buyer confirms PO — returns refreshed detail."""
    from datetime import datetime

    from ..services.buyplan_notifications import notify_po_confirmed, run_notify_bg
    from ..services.buyplan_workflow import confirm_po

    form = await request.form()
    po_number = form.get("po_number", "").strip()
    ship_date_str = form.get("estimated_ship_date", "")

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
    from ..services.buyplan_notifications import notify_completed, run_notify_bg
    from ..services.buyplan_workflow import check_completion, verify_po

    form = await request.form()
    action = form.get("action", "approve")

    try:
        verify_po(plan_id, line_id, action, user, db, rejection_note=form.get("rejection_note"))
        db.commit()
        updated = check_completion(plan_id, db)
        if updated and updated.status == BuyPlanStatus.COMPLETED:
            db.commit()
            await run_notify_bg(notify_completed, plan_id)
    except (ValueError, PermissionError) as e:
        raise HTTPException(400, str(e))

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

    if not db.get(BuyPlan, plan_id):
        raise HTTPException(404, "Buy plan not found")

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


@router.post("/v2/partials/buy-plans/{plan_id}/reset", response_class=HTMLResponse)
async def buy_plan_reset_partial(
    request: Request,
    plan_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Reset halted/cancelled plan to draft — returns refreshed detail."""
    from ..services.buyplan_workflow import reset_buy_plan_to_draft

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
    return template_response("htmx/base_page.html", ctx)


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
    return template_response("htmx/base_page.html", ctx)


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
    from ..services.sourcing_leads import update_lead_status

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
    from ..services.sourcing_leads import append_lead_feedback

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
    return template_response("htmx/base_page.html", ctx)


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
    user: User = Depends(require_user),
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
    quote = (
        db.query(Quote)
        .options(
            joinedload(Quote.customer_site).joinedload(CustomerSite.company),
            joinedload(Quote.requisition),
            joinedload(Quote.created_by),
        )
        .filter(Quote.id == quote_id)
        .first()
    )
    if not quote:
        raise HTTPException(404, "Quote not found")
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
    quote = db.get(Quote, quote_id)
    if not quote:
        raise HTTPException(404, "Quote not found")
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
    quote = db.get(Quote, quote_id)
    if not quote:
        raise HTTPException(404, "Quote not found")
    offer = db.get(Offer, offer_id)
    if not offer:
        raise HTTPException(404, "Offer not found")
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
    """Mark quote as sent — returns refreshed detail partial."""

    quote = db.get(Quote, quote_id)
    if not quote:
        raise HTTPException(404, "Quote not found")
    require_valid_transition("quote", quote.status, QuoteStatus.SENT)
    quote.status = QuoteStatus.SENT
    quote.sent_at = datetime.now(timezone.utc)
    db.commit()
    logger.info("Quote {} marked as sent by {}", quote.quote_number, user.email)
    return await quote_detail_partial(request, quote_id, user, db)


@router.post("/v2/partials/quotes/{quote_id}/result", response_class=HTMLResponse)
async def quote_result_htmx(
    request: Request,
    quote_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Mark quote result (won/lost) — returns refreshed detail partial."""

    quote = db.get(Quote, quote_id)
    if not quote:
        raise HTTPException(404, "Quote not found")
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
    quote = db.get(Quote, quote_id)
    if not quote:
        raise HTTPException(404, "Quote not found")
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

    quote = db.query(Quote).filter(Quote.id == quote_id, Quote.requisition_id == req_id).first()
    if not quote:
        raise HTTPException(404, "Quote not found")
    if quote.status != "draft":
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

    quote = db.query(Quote).filter(Quote.id == quote_id).first()
    if not quote:
        raise HTTPException(404, "Quote not found")
    if quote.status != "won":
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
    suggested = db.query(ProspectAccount).filter(ProspectAccount.status == ProspectAccountStatus.SUGGESTED).all()
    claimed = (
        db.query(sqlfunc.count(ProspectAccount.id))
        .filter(ProspectAccount.status == ProspectAccountStatus.CLAIMED)
        .scalar()
        or 0
    )
    return {
        "total": len(suggested),
        "buyer_ready": sum(1 for p in suggested if build_priority_snapshot(p)["is_buyer_ready"]),
        "call_now": sum(1 for p in suggested if (p.readiness_score or 0) >= 70),
        "claimed": claimed,
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
    sort: str = "buyer_ready_desc",
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return the prospecting card grid as an HTML partial.

    Sorts: buyer_ready_desc (default) ranks by the composite buyer-ready score from
    build_priority_snapshot (the single source of truth for "buyer ready"); fit_desc
    and recent_desc sort in SQL. Dismissed prospects are hidden unless filtered for.
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

    if sort == "buyer_ready_desc":
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


# ── Settings partials ────────────────────────────────────────────────


@router.get("/v2/partials/settings", response_class=HTMLResponse)
async def settings_partial(
    request: Request,
    tab: str = "sources",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Settings page — renders index with active tab."""
    ctx = _base_ctx(request, user, "settings")
    ctx["active_tab"] = tab
    ctx["is_admin"] = user.role == UserRole.ADMIN
    return template_response("htmx/partials/settings/index.html", ctx)


@router.get("/v2/partials/settings/sources", response_class=HTMLResponse)
async def settings_sources_tab(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Sources tab content."""
    sources = db.query(ApiSource).order_by(ApiSource.display_name).all()
    ctx = _base_ctx(request, user, "settings")
    ctx["sources"] = sources
    return template_response("htmx/partials/settings/sources.html", ctx)


@router.get("/v2/partials/settings/system", response_class=HTMLResponse)
async def settings_system_tab(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """System config tab — admin only."""
    if user.role != UserRole.ADMIN:
        raise HTTPException(403, "Admin only")
    from ..services.admin_service import get_all_config

    config = get_all_config(db)
    ctx = _base_ctx(request, user, "settings")
    ctx["config"] = config
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
    ctx["inbox_status"] = get_inbox_sync_status(user)
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
    ctx["inbox_status"] = get_inbox_sync_status(user)
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

    vendor_dupes = []
    company_dupes = []
    try:
        from ..vendor_utils import find_vendor_dedup_candidates

        vendor_dupes = find_vendor_dedup_candidates(db, threshold=85, limit=30)
    except Exception as e:
        logger.warning(f"Vendor dedup scan failed: {e}")
    try:
        from ..company_utils import find_company_dedup_candidates

        company_dupes = find_company_dedup_candidates(db, threshold=85, limit=30)
    except Exception as e:
        logger.warning(f"Company dedup scan failed: {e}")

    ctx = _base_ctx(request, user, "settings")
    ctx["vendor_dupes"] = vendor_dupes
    ctx["company_dupes"] = company_dupes
    return template_response("htmx/partials/settings/data_ops.html", ctx)


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
        return HTMLResponse(
            f'<p class="text-sm text-emerald-600 py-2">Merged into {html_mod.escape(str(result.get("kept_name", "vendor")))}. '
            f"{result.get('reassigned', 0)} records reassigned.</p>"
        )
    except ValueError as e:
        return HTMLResponse(f'<p class="text-sm text-rose-600 py-2">Error: {html_mod.escape(str(e))}</p>')


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
        return HTMLResponse(
            f'<p class="text-sm text-emerald-600 py-2">Merged into {html_mod.escape(str(result.get("kept_name", "company")))}.</p>'
        )
    except (ValueError, Exception) as e:
        return HTMLResponse(f'<p class="text-sm text-rose-600 py-2">Error: {html_mod.escape(str(e))}</p>')


# ── Proactive Part Match ─────────────────────────────────────────────


@router.get("/v2/partials/proactive", response_class=HTMLResponse)
async def proactive_list_partial(
    request: Request,
    tab: str = "matches",
    user: User = Depends(require_user),
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


@router.post("/v2/partials/admin/import/vendors", response_class=HTMLResponse)
async def import_vendors_csv(
    request: Request,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Import vendors from CSV upload."""
    form = await request.form()
    file = form.get("file")
    if not file:
        raise HTTPException(400, "CSV file is required")

    import csv
    import io

    content = await file.read()
    reader = csv.DictReader(io.StringIO(content.decode("utf-8")))
    count = 0
    for row in reader:
        name = row.get("name", "").strip()
        if not name:
            continue
        from ..vendor_utils import normalize_vendor_name

        norm = normalize_vendor_name(name)
        existing = db.query(VendorCard).filter(VendorCard.normalized_name == norm).first()
        if not existing:
            vc = VendorCard(
                display_name=name,
                normalized_name=norm,
                emails=[row.get("email", "")] if row.get("email") else [],
                phones=[row.get("phone", "")] if row.get("phone") else [],
                website=row.get("website", ""),
                sighting_count=0,
            )
            db.add(vc)
            count += 1
    db.commit()
    logger.info("Vendor CSV import: {} new vendors by {}", count, user.email)

    return HTMLResponse(
        f'<div class="bg-emerald-50 border border-emerald-200 rounded p-3 text-sm text-emerald-700">'
        f"Imported {count} new vendors from CSV.</div>"
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
    from sqlalchemy import case

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
                    RequisitionStatus.ACTIVE,
                    RequisitionStatus.SOURCING,
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

    ctx = _base_ctx(request, user, "requisitions")
    ctx.update(
        {
            "requirements": requirements,
            "offer_stats": offer_stats,
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

    ctx = _base_ctx(request, user, "requisitions")
    ctx.update({"requirement": req, "offers": offers})
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

    part.sourcing_status = SourcingStatus.OPEN
    db.commit()
    logger.info("Part {} unarchived by {}", requirement_id, user.email)

    return await parts_list_partial(request=request, user=user, db=db)


@router.patch("/v2/partials/requisitions/{req_id}/archive", response_class=HTMLResponse)
async def archive_requisition(
    req_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Archive a whole requisition and cascade to all its requirements."""
    requisition = db.get(Requisition, req_id)
    if not requisition:
        raise HTTPException(404, "Requisition not found")

    prior_status = requisition.status
    requisition.status = RequisitionStatus.ARCHIVED
    for child in requisition.requirements:
        child.sourcing_status = SourcingStatus.ARCHIVED
    if prior_status != RequisitionStatus.ARCHIVED:
        _log_activity(
            db,
            activity_type=ActivityType.REQ_ARCHIVED,
            requisition_id=requisition.id,
            user_id=user.id,
            description="Requisition archived",
        )
    db.commit()
    logger.info("Requisition {} ({} parts) archived by {}", req_id, len(requisition.requirements), user.email)

    return await parts_list_partial(request=request, user=user, db=db)


@router.patch("/v2/partials/requisitions/{req_id}/unarchive", response_class=HTMLResponse)
async def unarchive_requisition(
    req_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Unarchive a requisition and restore all its requirements to open."""
    requisition = db.get(Requisition, req_id)
    if not requisition:
        raise HTTPException(404, "Requisition not found")

    prior_status = requisition.status
    requisition.status = RequisitionStatus.ACTIVE
    for child in requisition.requirements:
        if child.sourcing_status == SourcingStatus.ARCHIVED:
            child.sourcing_status = SourcingStatus.OPEN
    if prior_status != RequisitionStatus.ACTIVE:
        _log_activity(
            db,
            activity_type=ActivityType.REQ_UNARCHIVED,
            requisition_id=requisition.id,
            user_id=user.id,
            description="Requisition unarchived",
        )
    db.commit()
    logger.info("Requisition {} unarchived by {}", req_id, user.email)

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

    # Bulk-update parts in a single query instead of N+1
    if requirement_ids:
        db.query(Requirement).filter(
            Requirement.id.in_(requirement_ids),
        ).update({"sourcing_status": SourcingStatus.ARCHIVED}, synchronize_session="fetch")

    # Archive requisitions and cascade to their children
    if requisition_ids:
        reqs = db.query(Requisition).filter(Requisition.id.in_(requisition_ids)).all()
        for requisition in reqs:
            requisition.status = RequisitionStatus.ARCHIVED
            _log_activity(
                db,
                activity_type=ActivityType.REQ_ARCHIVED,
                requisition_id=requisition.id,
                user_id=user.id,
                description="Requisition archived",
            )
        # Cascade: archive all children of these requisitions
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

    # Bulk-update parts in a single query instead of N+1
    if requirement_ids:
        db.query(Requirement).filter(
            Requirement.id.in_(requirement_ids),
            Requirement.sourcing_status == SourcingStatus.ARCHIVED,
        ).update({"sourcing_status": SourcingStatus.OPEN}, synchronize_session="fetch")

    # Unarchive requisitions and cascade to their children
    if requisition_ids:
        reqs = db.query(Requisition).filter(Requisition.id.in_(requisition_ids)).all()
        for requisition in reqs:
            requisition.status = RequisitionStatus.ACTIVE
        # Cascade: restore archived children of these requisitions
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
async def trouble_tickets_workspace(request: Request, user: User = Depends(require_user)):
    """Trouble Tickets workspace — loaded into #main-content."""
    return template_response(
        "htmx/partials/tickets/workspace.html",
        {**_base_ctx(request, user, "tickets")},
    )


@router.get("/v2/partials/trouble-tickets/list", response_class=HTMLResponse)
async def trouble_tickets_list(
    request: Request,
    status: str = "",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Trouble Tickets list partial — grouped by root cause, filterable by status."""
    from app.models.root_cause_group import RootCauseGroup
    from app.models.trouble_ticket import TroubleTicket

    q = (
        db.query(TroubleTicket)
        .options(joinedload(TroubleTicket.root_cause_group), joinedload(TroubleTicket.submitter))
        .filter(TroubleTicket.source == TicketSource.REPORT_BUTTON)
    )
    if status:
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

    return template_response(
        "htmx/partials/tickets/list.html",
        {
            **_base_ctx(request, user, "tickets"),
            "total": total,
            "groups": groups,
            "grouped": grouped,
            "ungrouped": ungrouped,
            "current_status": status,
        },
    )


@router.get("/v2/partials/trouble-tickets/{ticket_id}", response_class=HTMLResponse)
async def trouble_ticket_detail(
    request: Request,
    ticket_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Trouble Ticket detail partial — swapped into #main-content."""
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
