"""routers/htmx/companies/core/lists.py — customers list partials, bulk actions, CSV
import, archived list (W4.8 split of core.py).

The legacy /v2/companies → /v2/customers redirects, the CDM account workspace
(split-panel list) + left-panel refresh, account bulk actions, company CSV import
preview/confirm, and the archived (DNC) companies list. Every route here is a
STATIC path (no single-segment ``{company_id}``) — this module registers FIRST in
core/__init__ so ``POST /v2/partials/customers/bulk/{action}`` precedes
.lifecycle's ``/{company_id}/deactivate`` and ``/{company_id}/send-to-prospecting``
(both live bulk action names — order is behavior).

Called by: app.routers.htmx.companies.core (package __init__ re-export, route
    registration)
Depends on: app.services.company_import_service, app.services.crm_service,
    .._registries, ..saved_views, ..._shared, .common
"""

import json
from datetime import UTC, datetime

from fastapi import Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from loguru import logger
from sqlalchemy.orm import Session

from .....database import get_db
from .....dependencies import is_manager_or_admin, require_user
from .....dependencies import manageable_company_ids as _manageable_company_ids
from .....models import Company, User
from .....services.company_import_service import confirm_company_import, parse_csv_rows, preview_company_import
from .....services.crm_service import cdm_list_ctx
from .....template_env import template_response
from ..._shared import _base_ctx
from .._registries import BULK_MAX_IDS as _BULK_MAX_IDS
from ..saved_views import _saved_views_ctx
from .common import router

# ── Company/Customer partials ──────────────────────────────────────────


# Redirect old /v2/companies URLs to /v2/customers
@router.get("/v2/companies", response_class=HTMLResponse)
@router.get("/v2/companies/{path:path}", response_class=HTMLResponse)
async def companies_redirect(request: Request, path: str = ""):
    """Redirect old /v2/companies URLs to /v2/customers."""
    new_url = f"/v2/customers/{path}" if path else "/v2/customers"
    if request.url.query:
        new_url += f"?{request.url.query}"
    return RedirectResponse(url=new_url, status_code=301)


# Redirect old /v2/partials/companies URLs to /v2/partials/customers
@router.get("/v2/partials/companies", response_class=HTMLResponse)
@router.get("/v2/partials/companies/{path:path}", response_class=HTMLResponse)
async def partials_companies_redirect(request: Request, path: str = ""):
    """Redirect old /v2/partials/companies URLs to /v2/partials/customers."""
    new_url = f"/v2/partials/customers/{path}" if path else "/v2/partials/customers"
    if request.url.query:
        new_url += f"?{request.url.query}"
    return RedirectResponse(url=new_url, status_code=301)


@router.get("/v2/partials/customers", response_class=HTMLResponse)
async def companies_list_partial(
    request: Request,
    search: str = "",
    staleness: str = "",
    account_type: str = "",
    my_only: bool = False,
    sort: str = "oldest",
    segment: int = Query(0, ge=0),
    disposition: str = "",
    has_open_reqs: bool = False,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return the CDM account workspace (split-panel list + detail) as HTML partial."""
    ctx = _base_ctx(request, user, "customers")
    ctx.update(
        cdm_list_ctx(
            db,
            user,
            search=search,
            staleness=staleness,
            account_type=account_type,
            my_only=my_only,
            sort=sort,
            segment=segment,
            disposition=disposition or None,
            has_open_reqs=has_open_reqs,
            limit=limit,
            offset=offset,
            include_overdue=True,
            include_users=is_manager_or_admin(user),
        )
    )
    ctx.update(_saved_views_ctx(request, user, db, "customers"))
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
    disposition: str = "",
    has_open_reqs: bool = False,
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
        cdm_list_ctx(
            db,
            user,
            search=search,
            staleness=staleness,
            account_type=account_type,
            my_only=my_only,
            sort=sort,
            segment=segment,
            disposition=disposition or None,
            has_open_reqs=has_open_reqs,
            limit=limit,
            offset=offset,
            include_users=is_manager_or_admin(user),
        )
    )
    return template_response("htmx/partials/customers/_account_list.html", ctx)


# ── Bulk actions (static — must precede /{company_id}) ────────────────────
# Every bulk action FILTERS selected ids to only those the caller can act on.
# Sales/trader reps may only act on companies where can_manage_account() is True.
# Manager/admin can act on all. "assign-owner" is MANAGER/ADMIN ONLY.
# Non-manageable companies are silently skipped; the summary reports both counts.

_VALID_BULK_COMPANY_ACTIONS = frozenset({"deactivate", "send-to-prospecting", "assign-owner"})


@router.post("/v2/partials/customers/bulk/{action}", response_class=HTMLResponse)
async def customers_bulk_action(
    request: Request,
    action: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Apply a bulk action to selected companies.

    Auth: deactivate + send-to-prospecting gate per-company via can_manage_account;
    assign-owner is MANAGER/ADMIN ONLY (403 for reps).
    Selected ids that the caller cannot act on are silently skipped; the returned
    partial includes a summary of applied vs skipped counts.

    Actions:
    - deactivate: set is_active=False
    - send-to-prospecting: clear ownership (ownership_cleared_at + account_owner_id=NULL)
    - assign-owner: set account_owner_id to owner_id form param (MANAGER/ADMIN only)
    """
    if action not in _VALID_BULK_COMPANY_ACTIONS:
        raise HTTPException(400, f"Invalid action '{action}'. Allowed: {sorted(_VALID_BULK_COMPANY_ACTIONS)}")

    # assign-owner is manager/admin only — 403 before reading IDs to avoid timing oracle
    if action == "assign-owner" and not is_manager_or_admin(user):
        raise HTTPException(403, "assign-owner requires MANAGER or ADMIN role")

    form = await request.form()
    ids_str = form.get("ids", "") or ""
    try:
        ids = [int(x.strip()) for x in ids_str.split(",") if x.strip().isdigit()]
    except ValueError as e:
        raise HTTPException(400, "Invalid ID list") from e

    if len(ids) > _BULK_MAX_IDS:
        raise HTTPException(400, f"Maximum {_BULK_MAX_IDS} companies per bulk action")

    if not ids:
        # No-op: return refreshed list
        ctx = {"request": request, "user": user}
        ctx.update(
            cdm_list_ctx(
                db,
                user,
                search="",
                staleness="",
                account_type="",
                my_only=False,
                sort="oldest",
                segment=0,
                disposition=None,
                has_open_reqs=False,
                limit=50,
                offset=0,
                include_users=is_manager_or_admin(user),
            )
        )
        return template_response("htmx/partials/customers/_account_list.html", ctx)

    companies = db.query(Company).filter(Company.id.in_(ids)).all()

    # Filter to only companies this user can act on
    if is_manager_or_admin(user):
        authorised = companies
        skipped = 0
    else:
        manageable_ids = _manageable_company_ids(user, companies, db)
        authorised = [c for c in companies if c.id in manageable_ids]
        skipped = len(companies) - len(authorised)

    applied = 0
    if action == "deactivate":
        for co in authorised:
            co.is_active = False
            applied += 1
    elif action == "send-to-prospecting":
        for co in authorised:
            co.account_owner_id = None
            co.ownership_cleared_at = datetime.now(UTC)
            applied += 1
    elif action == "assign-owner":
        owner_id_raw = form.get("owner_id")
        if not owner_id_raw:
            raise HTTPException(400, "owner_id is required for assign-owner")
        try:
            new_owner_id = int(owner_id_raw)
        except (TypeError, ValueError) as e:
            raise HTTPException(400, "owner_id must be an integer") from e
        new_owner = db.get(User, new_owner_id)
        if not new_owner or not new_owner.is_active:
            raise HTTPException(400, "owner_id does not correspond to an active user")
        for co in authorised:
            co.account_owner_id = new_owner_id
            applied += 1

    if applied:
        db.commit()
        logger.info(
            "Bulk {} applied to {} companies ({} skipped) by {}",
            action,
            applied,
            skipped,
            user.email,
        )

    action_label = {
        "deactivate": "Deactivated",
        "send-to-prospecting": "Sent to prospecting",
        "assign-owner": "Reassigned",
    }.get(action, action.title())

    if skipped:
        msg = f"{action_label} {applied} of {applied + skipped} ({skipped} skipped — not yours)"
    else:
        msg = f"{action_label} {applied} account{'s' if applied != 1 else ''}"

    ctx = {"request": request, "user": user}
    ctx.update(
        cdm_list_ctx(
            db,
            user,
            search="",
            staleness="",
            account_type="",
            my_only=False,
            sort="oldest",
            segment=0,
            disposition=None,
            has_open_reqs=False,
            limit=50,
            offset=0,
            include_users=is_manager_or_admin(user),
        )
    )
    resp = template_response("htmx/partials/customers/_account_list.html", ctx)
    resp.headers["HX-Trigger"] = json.dumps(
        {
            "showToast": {"message": msg},
            "clearSelection": True,
        }
    )
    return resp


# ── Company CSV import: preview + confirm ──────────────────────────────────
# Business logic (CSV parse, dedup queries, row creation) lives in
# app.services.company_import_service — these routes stay HTTP-only (P4.2).


@router.post("/v2/partials/customers/import/preview", response_class=HTMLResponse)
async def import_companies_preview(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Parse company CSV and return a preview table (no writes).

    Expected columns: name (required), website, account_type.
    Flags: duplicate (normalized_name collision), invalid (missing name).
    """
    form = await request.form()
    file = form.get("file")
    if not file:
        raise HTTPException(400, "A CSV file is required")

    try:
        content_bytes = await file.read() if hasattr(file, "read") else file.file.read()
        raw_rows = parse_csv_rows(content_bytes)
    except AttributeError:
        # "file" submitted as a plain form field (e.g. a bare string) rather than an
        # actual upload — no .read()/.file to pull bytes from. Same graceful partial
        # as a malformed CSV, not a 500.
        raw_rows = None
    if raw_rows is None:
        return HTMLResponse(
            '<div class="text-rose-700 text-sm p-3 bg-rose-50 rounded border border-rose-200">'
            "Could not parse CSV — please check the file format.</div>"
        )

    try:
        preview = preview_company_import(db, raw_rows)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    return template_response(
        "htmx/partials/customers/_import_preview.html",
        {
            "request": request,
            "rows": preview["rows"],
            "valid_count": preview["valid_count"],
            "dup_count": preview["dup_count"],
            "invalid_count": preview["invalid_count"],
            "rows_json": json.dumps(preview["valid_rows"]),
        },
    )


@router.post("/v2/partials/customers/import/confirm", response_class=HTMLResponse)
async def import_companies_confirm(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Create Company rows from a confirmed import (validated rows_json payload).

    Each row: {name, website?, account_type?}. Deduplicates by normalized_name.
    Sets account_owner_id to the importing user.
    """
    form = await request.form()
    rows_json_str = form.get("rows_json", "")
    if not rows_json_str:
        raise HTTPException(400, "rows_json is required")

    try:
        rows = json.loads(rows_json_str)
        if not isinstance(rows, list):
            raise ValueError("Expected a list")
    except (ValueError, TypeError) as e:
        raise HTTPException(400, "Invalid rows_json — must be a JSON array") from e

    try:
        result = confirm_company_import(db, rows, user)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    resp = template_response(
        "htmx/partials/customers/_import_confirm_summary.html",
        {"request": request, "summary": result["summary"]},
    )
    resp.headers["HX-Trigger"] = json.dumps({"showToast": {"message": result["summary"]}})
    return resp


# Registered here (a static GET path grouped with the other list views) rather than in
# its original slot between reactivate and send-to-prospecting — safe because no
# single-segment GET /v2/partials/customers/{param} route exists inside this
# subpackage, so GET registration order is not load-bearing (unlike the POST side).
@router.get("/v2/partials/customers/archived", response_class=HTMLResponse)
async def archived_companies_list(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return the list of archived (DNC) companies.

    Gate: require_user — any logged-in user may VIEW the archived list.
    Reactivate button is only rendered for manager/admin.

    Called by: a future "Archived" tab or "View Archived" link in the CDM workspace.
    """
    companies = db.query(Company).filter(Company.is_active.is_(False)).order_by(Company.name).all()
    ctx = {
        "request": request,
        "user": user,
        "companies": companies,
        "can_reactivate": is_manager_or_admin(user),
    }
    return template_response("htmx/partials/customers/archived_list.html", ctx)
