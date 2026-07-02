"""routers/htmx_views.py — HTMX + Alpine.js MVP frontend views.

Serves server-rendered HTML partials for the HTMX-based frontend.
Full page loads render base.html; HTMX requests get just the partial.
All routes live under /v2 to coexist with the original SPA frontend.

Called by: main.py (router mount)
Depends on: models, dependencies, database, search_service
"""

import html as html_mod
import json
from datetime import datetime, timezone
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from loguru import logger
from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session, joinedload

from ..constants import (
    AccessKey,
    ApiSourceStatus,
    RequisitionStatus,
    SourcingStatus,
    UserRole,
)
from ..database import get_db
from ..dependencies import (
    get_req_for_user,
    get_user,
    is_manager_or_admin,
    require_access,
    require_buyer,
    require_requisition_access,
    require_user,
    user_has_access,
)
from ..models import (
    ApiSource,
    Company,
    Requirement,
    Requisition,
    Sighting,
    SiteContact,
    SourcingLead,
    User,
    VendorCard,
)
from ..scoring import classify_lead, explain_lead, score_unified
from ..services.sighting_ingest import sighting_from_row
from ..services.vendor_unavailability import apply_to_fresh_sightings
from ..template_env import page_response, template_response, templates
from ..utils.search_builder import SearchBuilder
from ..utils.sql_helpers import escape_like
from ._lookup_helpers import get_requisition_or_404
from .auth import _password_login_enabled
from .htmx._shared import _base_ctx, _safe_int, _vite_assets
from .htmx.requisitions import _best_quote_status, requisition_tab, requisitions_list_partial
from .htmx.settings import _run_inbox_scan_now

router = APIRouter(tags=["htmx-views"])

# Nav-id aliases: routes that were demoted into a parent nav item highlight the parent
# instead. The standalone Quotes list redirects to /v2/requisitions and the Reporting
# surface was retired, so neither needs an alias. Quote detail (/v2/quotes/{id}) falls
# through to "quotes", which matches no nav item — correct, since it has no parent tab to
# highlight.
# Current aliases: the global contact lists live under the CRM nav item (twins of
# Customers/Vendors) so they borrow "crm", and the Approvals surface borrows "buy-plans".
_NAV_ID_ALIAS: dict[str, str] = {"contacts": "crm", "vendor-contacts": "crm", "approvals": "buy-plans"}


def _is_htmx(request: Request) -> bool:
    """Check if this is an HTMX partial request (vs full page load)."""
    return request.headers.get("HX-Request") == "true"


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
@router.get("/v2/search/results", response_class=HTMLResponse)
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
        if path.rstrip("/").endswith("/results"):
            # Full-page global-search results (F5/bookmark/share of "View all
            # results"): thread ?q= to the results partial (shell-01). Without this
            # route the pushed /v2/search/results URL 404'd → bare JSON error page.
            q_qs = request.query_params.get("q", "").strip()
            partial_url = f"/v2/partials/search/results?q={quote(q_qs)}"
        else:
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
        # lens URL paint the right lens on first full-page load instead of falling to
        # _default_lens. Lens keys are the two surviving lenses (My Queue + Pipeline). A
        # detail URL (/buy-plans/{id}) is overridden by the _DETAIL_VIEWS block below.
        lens_qs = request.query_params.get("lens", "").strip()
        partial_url = (
            f"/v2/partials/approvals?lens={quote(lens_qs)}"
            if lens_qs in ("my_queue", "pipeline")
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
        if not is_manager_or_admin(user):
            raise HTTPException(403, "Only managers or admins can reassign requisition owners")
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
        if not is_manager_or_admin(user):
            raise HTTPException(403, "Only managers or admins can reassign requisition owners")
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
        # Row context — re-fetch ORM object with relationships. Must attach the SAME
        # computed attrs the list route does (req_count/offer_count/quote_status) or
        # the swapped-in row degrades those cells.
        req = (
            db.query(Requisition)
            .options(
                joinedload(Requisition.creator),
                joinedload(Requisition.requirements),
                joinedload(Requisition.offers),
                joinedload(Requisition.quotes),
            )
            .filter(Requisition.id == req_id)
            .first()
        )
        req.req_count = len(req.requirements) if req.requirements else 0
        req.offer_count = len(req.offers) if req.offers else 0
        req.quote_status = _best_quote_status(req.quotes)
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
            .filter(SourcingLead.vendor_name.ilike(escape_like(vendor_name), escape="\\"))
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
        # Mirror update_requirement: store the canonical key-form normalized_mpn
        # (lowercase, separators stripped) so part-history / material-card joins
        # line up, and resolve the MaterialCard up front.
        from ..search_service import resolve_material_card
        from ..utils.normalization import normalize_mpn_key

        card = resolve_material_card(mpn, db)
        requirement = Requirement(
            requisition_id=requisition_id,
            primary_mpn=mpn,
            normalized_mpn=normalize_mpn_key(mpn),
            material_card_id=card.id if card else None,
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
        f"Added {count} result{'s' if count != 1 else ''} to requisition &ldquo;{html_mod.escape(req.name or '')}&rdquo;"
        f"</div>"
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


# ── Sprint 9: Materials + Activity + Knowledge ────────────────────────


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
