"""routers/htmx_views.py — HTMX + Alpine.js MVP frontend views.

Serves server-rendered HTML partials for the HTMX-based frontend.
Full page loads render base.html; HTMX requests get just the partial.
All routes live under /v2 to coexist with the original SPA frontend.

Core module: the full-page shell dispatcher (v2_page), the parts workspace
entry point, and the vendor stock-list upload. The rest of the surface
(My Day, email views, AI insights/knowledge/dashboard, search, requisition
bulk+inline edit) lives in per-domain modules under app/routers/htmx/ and is
aggregated into THIS module's `router` via `include_router()` below, so
app/main.py keeps mounting a single `htmx_views_router` unchanged. Names that
tests patch/import directly are re-exported here too (see bottom imports).

Called by: main.py (router mount)
Depends on: models, dependencies, database, .htmx.my_day, .htmx.email_views,
    .htmx.insights_views, .htmx.search_views, .htmx.requisitions_edit
"""

from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from loguru import logger
from sqlalchemy.orm import Session

from ..constants import AccessKey, UserRole
from ..database import get_db
from ..dependencies import get_user, require_access, require_buyer, user_has_access
from ..models import User
from ..template_env import page_response, template_response, templates  # noqa: F401 — templates re-exported for tests
from .auth import _password_login_enabled
from .htmx._shared import _base_ctx, _safe_int, _vite_assets  # noqa: F401 — _safe_int re-exported for tests
from .htmx.email_views import router as _email_views_router
from .htmx.email_views import send_email_reply  # noqa: F401 — re-exported for tests
from .htmx.insights_views import router as _insights_views_router
from .htmx.my_day import router as _my_day_router
from .htmx.requisitions_edit import router as _requisitions_edit_router
from .htmx.requisitions_edit import update_requirement  # noqa: F401 — re-exported for tests
from .htmx.search_views import (  # noqa: F401 — re-exported for tests
    _get_cached_search_results,
    _get_enabled_sources,
    add_to_requisition,
    requisition_picker,
    search_filter,
    search_run,
)
from .htmx.search_views import router as _search_views_router

# app/main.py mounts only THIS module's `router` (unchanged registration), so the
# split-out per-domain sub-routers (P4.3) are aggregated internally instead of
# main.py importing each one individually.
router = APIRouter(tags=["htmx-views"])
router.include_router(_my_day_router)
router.include_router(_email_views_router)
router.include_router(_insights_views_router)
router.include_router(_search_views_router)
router.include_router(_requisitions_edit_router)

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
    (AccessKey.BUY_PLANS, "/v2/approvals"),
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
@router.get("/v2/offers/review-queue", response_class=HTMLResponse)
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
    # First matching segment wins — order is load-bearing (e.g. /vendor-contacts before
    # /vendors). Anything unmatched defaults to the requisitions view.
    _VIEW_SEGMENTS = (
        "approvals",
        "resell",
        "quotes",
        "prospecting",
        "proactive",
        "settings",
        "materials",
        "follow-ups",
        # "/offers" only appears in the review-queue full page — no collision with other
        # view segments, so its position here is not load-bearing.
        "offers",
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
        # Split-panel workspace is the default Sales Hub; ?view=list serves the flat
        # requisitions list so the List-view toggle's pushed URL
        # (/v2/requisitions?view=list) reloads / bookmarks straight to the list.
        partial_url = (
            "/v2/partials/requisitions"
            if request.query_params.get("view") == "list"
            else "/v2/partials/parts/workspace"
        )
    elif current_view == "trouble-tickets":
        partial_url = "/v2/partials/trouble-tickets/workspace"
    elif current_view == "crm":
        partial_url = "/v2/partials/crm/shell"
    elif current_view == "sightings":
        partial_url = "/v2/partials/sightings/workspace"
    elif current_view == "offers":
        # The offer review queue (medium-confidence AI-parsed offers) — surfaced from the
        # Sightings workspace quick-links. Its own canonical page so a pushed/bookmarked
        # /v2/offers/review-queue reloads straight into the queue instead of 404ing.
        partial_url = "/v2/partials/offers/review-queue"
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
    elif current_view == "approvals":
        # The Approvals module is now the org-wide 3-tab decide console (Buy Plan / PO
        # Approval / Prepayment) at /v2/partials/approvals. Thread ?tab= through (customer
        # deep-link pattern) so a reload/bookmark of a pushed tab URL paints the right tab.
        tab_qs = request.query_params.get("tab", "").strip()
        partial_url = f"/v2/partials/approvals?tab={quote(tab_qs)}" if tab_qs else "/v2/partials/approvals"
    else:
        partial_url = f"/v2/partials/{current_view}"
    # Detail views: a trailing numeric id (/{view}/{id}) overrides the list partial with
    # the detail partial. Each split key equals the current_view, so at most one applies.
    _DETAIL_VIEWS = (
        "requisitions",
        "vendors",
        "customers",
        "resell",
        "quotes",
        "prospecting",
        "trouble-tickets",
        # "materials" — /v2/materials/{id} deep-links (row push-url, F5 reload, the
        # Add-part HX-Redirect) must lazy-load the card detail, not the faceted list.
        "materials",
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


# ── Retired Buy Plans hub (/v2/buy-plans) ─────────────────────────────
# The personal My Queue + Pipeline hub retired into the Approvals Workspace once
# Phase-3 parity landed (spec §11.1; docs/APPROVALS_PARITY_CHECKLIST.md). Old
# bookmarks and pushed URLs 308 onto the workspace's Buy Plans tab. The workspace
# list has no per-plan preselection, so a /v2/buy-plans/{id} deep link lands on the
# tab, not the plan (accepted gap — see the checklist).


@router.get("/v2/buy-plans")
@router.get("/v2/buy-plans/{bp_id:int}")
async def buy_plans_hub_retired_redirect(bp_id: int | None = None) -> RedirectResponse:
    """308 the retired Buy Plans hub (and its detail deep links) to the workspace."""
    return RedirectResponse("/v2/approvals?tab=buy-plans", status_code=308)


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
