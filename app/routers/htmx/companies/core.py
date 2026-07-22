"""routers/htmx/companies/core.py — company list/create/edit/lifecycle basics (P4.3
split).

The customers (account) list + left-panel refresh, account bulk actions, company CSV
import, create/typeahead/duplicate-check, tier/disposition/parent-company/primary-
contact setters, deactivate/reactivate/archived-list, the "send to prospecting" park
flow, AI dup/name suggestions, account collaborators, and the account
create/edit forms + inline field editing. Everything here that needs the rendered
detail partial goes through ``_pkg._render_company_detail`` / ``_pkg.company_detail_partial``
(the package attribute, NOT a static ``from .detail import ...``) for two reasons: (1)
this module's own static routes (account-list, create-form, typeahead, check-duplicate,
archived) MUST register on the shared router before ``.detail``'s
``/v2/partials/customers/{company_id}`` catch-all — a module-level import of ``.detail``
would trigger its route registration as a side effect of importing THIS module, before
any of this module's own routes are defined; and (2) it preserves the monkeypatch on
``app.routers.htmx.companies.company_detail_partial`` a test relies on.

Called by: app.routers.htmx.companies (package __init__ re-export, route registration)
Depends on: app.services.crm_service, app.services.company_import_service,
    app.services.prospect_reclamation, app.company_utils, app.cache.decorators,
    ._registries, .saved_views, .detail
"""

import html as html_mod
import json
from datetime import UTC, datetime

from fastapi import Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from loguru import logger
from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session

import app.routers.htmx.companies as _pkg

from ....cache.decorators import invalidate_prefix
from ....company_utils import find_company_dup_match, suggest_clean_company_name
from ....constants import UserRole
from ....database import get_db
from ....dependencies import can_manage_account, can_manage_account_team, is_manager_or_admin, require_user
from ....dependencies import manageable_company_ids as _manageable_company_ids
from ....models import AccountCollaborator, Company, CustomerSite, SiteContact, User
from ....services.company_import_service import confirm_company_import, parse_csv_rows, preview_company_import
from ....services.crm_field_history import ENTITY_COMPANY, record_field_change
from ....services.crm_service import cadence_state, cdm_list_ctx, next_best_touch
from ....services.prospect_reclamation import park_company_in_prospecting
from ....template_env import template_response
from ....utils.normalization_helpers import normalize_country, normalize_phone_e164, normalize_us_state
from ....utils.search_builder import SearchBuilder
from .._shared import _base_ctx
from . import router
from ._registries import BULK_MAX_IDS as _BULK_MAX_IDS
from ._registries import EDITABLE_ACCOUNT_FIELDS, apply_company_field
from .saved_views import _saved_views_ctx

# NOTE: _render_company_detail is deliberately NOT imported at module scope here.
# .detail's static routes (account-list, create-form, typeahead, check-duplicate,
# archived) must register on the shared router BEFORE .detail's own
# `/v2/partials/customers/{company_id}` catch-all — a module-level `from .detail
# import ...` would trigger .detail's import (and route registration) as a side
# effect of importing THIS module, before any of this module's own @router...
# decorators run. Every call site below goes through `_pkg._render_company_detail`
# instead (same package-attribute indirection already used for
# `_pkg.company_detail_partial` in send_company_to_prospecting_htmx).

_VALID_TIERS = frozenset({"key", "core", "standard", "prospect"})

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

    raw_phone = form.get("phone", "").strip() or None
    raw_hq_state = form.get("hq_state", "").strip() or None
    raw_hq_country = form.get("hq_country", "").strip() or None
    company = Company(
        name=name,
        website=form.get("website", "").strip() or None,
        industry=form.get("industry", "").strip() or None,
        notes=form.get("notes", "").strip() or None,
        is_active=True,
        legal_name=form.get("legal_name", "").strip() or None,
        employee_size=form.get("employee_size", "").strip() or None,
        revenue_range=form.get("revenue_range", "").strip() or None,
        hq_city=form.get("hq_city", "").strip() or None,
        hq_state=(normalize_us_state(raw_hq_state) or raw_hq_state) if raw_hq_state else None,
        hq_country=(normalize_country(raw_hq_country) or raw_hq_country) if raw_hq_country else None,
        phone=(normalize_phone_e164(raw_phone) or raw_phone) if raw_phone else None,
        credit_terms=form.get("credit_terms", "").strip() or None,
        tax_id=form.get("tax_id", "").strip() or None,
        source=form.get("source", "").strip() or "manual",
    )
    # Assigning a NEW account to someone other than yourself is a manager action, and the
    # target must be a real active user (mirrors the bulk assign-owner path). A plain rep
    # assigning to self / leaving it blank keeps the current behaviour.
    owner_id = form.get("owner_id", "")
    if owner_id and owner_id.isdigit():
        new_owner_id = int(owner_id)
        if new_owner_id != user.id:
            if not is_manager_or_admin(user):
                raise HTTPException(403, "Only a manager can assign an account to another user")
            target = db.get(User, new_owner_id)
            if not target or not target.is_active:
                raise HTTPException(400, "Owner must be an active user")
        company.account_owner_id = new_owner_id
    db.add(company)
    db.flush()

    # Auto-create default site
    default_site = CustomerSite(
        company_id=company.id,
        site_name="HQ",
        site_type="hq",
        is_active=True,
    )
    db.add(default_site)
    db.commit()
    logger.info("Company {} created by {}", company.id, user.email)

    # Load the new account's detail into the CDM right panel (form hx-target=#cdm-detail);
    # the HX-Trigger tells the workspace's hidden listener to refresh the left account list
    # so the freshly created row appears. On deep-link contexts (no listener) it no-ops.
    resp = await _pkg._render_company_detail(request, company.id, user, db)
    resp.headers["HX-Trigger"] = "cdmListRefresh"
    return resp


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
    rows = [f'<option value="{c.id}">{html_mod.escape(c.name or "")}</option>' for c in companies]
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
            f'<p class="text-sm text-amber-600">A company named "{html_mod.escape(existing.name or "")}" already exists (ID {existing.id}).</p>'
        )
    return HTMLResponse("")


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
    if not can_manage_account(user, company, db):
        raise HTTPException(403, "Not authorized")

    form = await request.form()
    tier_raw = (form.get("tier") or "").strip()

    if tier_raw and tier_raw not in _VALID_TIERS:
        raise HTTPException(400, f"Invalid tier '{tier_raw}'. Valid: {sorted(_VALID_TIERS)}")

    company.tier = tier_raw or None
    db.commit()
    db.refresh(company)

    _cadence = cadence_state(company.tier, company.last_outbound_at)
    _nbt = next_best_touch(company.tier, company.last_outbound_at)
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


_VALID_DISPOSITIONS = frozenset({"active", "bucket"})


@router.post("/v2/partials/customers/{company_id}/disposition", response_class=HTMLResponse)
async def set_company_disposition(
    request: Request,
    company_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Set Company.disposition (active|bucket); re-renders the disposition control.

    Owner-or-admin only (mirrors release_prospect). Validates against the allowlist
    (invalid → 400). Writes disposition + optional reason + audit fields
    (set_by/set_at). Reversible — set back to 'active'. Invalidates the cached
    company_list / typeahead so the bucketed account drops out of the call-list.
    """
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(404, "Company not found")

    if not can_manage_account(user, company, db):
        raise HTTPException(403, "Only the owner or an admin can set disposition")

    form = await request.form()
    disp_raw = (form.get("disposition") or "").strip()
    reason_raw = (form.get("disposition_reason") or "").strip()

    if disp_raw not in _VALID_DISPOSITIONS:
        raise HTTPException(400, f"Invalid disposition '{disp_raw}'. Valid: {sorted(_VALID_DISPOSITIONS)}")

    company.disposition = disp_raw
    company.disposition_reason = reason_raw or None
    company.disposition_set_by = user.id
    company.disposition_set_at = datetime.now(UTC)
    db.commit()
    db.refresh(company)

    invalidate_prefix("company_list")

    logger.info("Company {} disposition set to {} by {}", company_id, company.disposition, user.email)
    return template_response(
        "htmx/partials/customers/_disposition_control.html",
        {"request": request, "company": company},
    )


@router.post("/v2/partials/customers/{company_id}/deactivate", response_class=HTMLResponse)
async def deactivate_company(
    request: Request,
    company_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Archive (soft-delete) a company — sets is_active=False, clears ownership, stores
    reason.

    Gate: can_manage_account_team — primary owner or manager/admin only.
    On archive: unassigns account owner (ownership_cleared_at stamped) and stores
    optional disposition_reason from the form.
    Re-renders the full company detail partial so the archived banner appears immediately.

    Called by: kebab menu "Archive (Do Not Call)" button in detail.html.
    """
    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")
    if not can_manage_account_team(user, company):
        raise HTTPException(403, "Not authorized to deactivate accounts")
    form = await request.form()
    disposition_reason = form.get("disposition_reason", "").strip() or None
    company.is_active = False
    company.account_owner_id = None
    company.ownership_cleared_at = datetime.now(UTC)
    company.disposition_reason = disposition_reason
    db.commit()
    db.refresh(company)

    invalidate_prefix("company_list")

    logger.info("Company {} archived (DNC) by {}, reason={!r}", company_id, user.email, disposition_reason)
    return await _pkg._render_company_detail(request, company_id, user, db)


@router.post("/v2/partials/customers/{company_id}/reactivate", response_class=HTMLResponse)
async def reactivate_company(
    request: Request,
    company_id: int,
    from_archived: bool = Query(False, alias="from_archived"),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Restore an archived company by setting is_active=True.

    Gate: is_manager_or_admin only.

    from_archived=true: called from the archived-list view → returns the refreshed
    archived_list partial (so the reactivated row disappears from the list).
    Default (false): called from the company detail banner → returns the detail
    partial (banner disappears after reactivate).
    """
    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")
    if not is_manager_or_admin(user):
        raise HTTPException(403, "Only managers and admins may reactivate archived accounts")
    company.is_active = True
    db.commit()
    db.refresh(company)
    logger.info("Company {} reactivated by {}", company_id, user.email)

    if from_archived:
        # Return refreshed archived list — reactivated company will no longer appear.
        companies = db.query(Company).filter(Company.is_active.is_(False)).order_by(Company.name).all()
        ctx = _base_ctx(request, user, "customers")
        ctx.update(
            {
                "companies": companies,
                "can_reactivate": True,  # gate already passed above
            }
        )
        return template_response("htmx/partials/customers/archived_list.html", ctx)

    return await _pkg._render_company_detail(request, company_id, user, db)


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


@router.post("/v2/partials/customers/{company_id}/send-to-prospecting", response_class=HTMLResponse)
async def send_company_to_prospecting_htmx(
    request: Request,
    company_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Park an owned account into the prospecting pool (SP4 "Park in prospecting").

    Gate: the account owner OR a manager/admin (``can_manage_account_team``) — else 403.
    Clears ownership, surfaces the account as a SUGGESTED prospect (by domain) stamped with
    the SP4 sales-park provenance (``discovery_source="sales_park"``, ``parked_by_id``), and
    returns a toast. A manager/admin — who still oversees the now-unassigned account — gets
    the re-rendered detail partial; the former owner, who just relinquished access, is
    redirected back to the customers list (re-rendering the detail would 404 for them).
    """
    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")
    if not can_manage_account_team(user, company):
        raise HTTPException(403, "Only the account owner or a manager can park this account")

    try:
        result = park_company_in_prospecting(company_id, user.id, db, is_admin=is_manager_or_admin(user))
    except LookupError as e:
        raise HTTPException(404, "Company not found") from e
    except ValueError as e:
        raise HTTPException(403, str(e)) from e

    invalidate_prefix("company_list")

    msg = f"Parked {result['company_name']} in prospecting"
    if not result["pooled"]:
        msg += " (no domain — ownership cleared, not pooled)"
    trigger = json.dumps({"showToast": {"message": msg}})

    if is_manager_or_admin(user):
        # Resolved off the PACKAGE attribute (not a static import of .detail.company_detail_partial)
        # so a test that monkeypatches app.routers.htmx.companies.company_detail_partial
        # replaces what actually runs here — see the module docstring.
        response = await _pkg.company_detail_partial(request, company_id, user=user, db=db)
        response.headers["HX-Trigger"] = trigger
        return response

    # Former owner relinquished access — send them back to the customers list.
    response = HTMLResponse("")
    response.headers["HX-Redirect"] = "/v2/customers"
    response.headers["HX-Trigger"] = trigger
    return response


@router.get("/v2/partials/customers/{company_id}/dup-suggestion", response_class=HTMLResponse)
async def company_dup_suggestion(
    request: Request,
    company_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Lazy per-account duplicate banner — top dedup match for THIS company + a Merge
    button reusing the merge-form/preview/merge flow.

    Renders nothing (empty 200) when there is no near-duplicate.
    """
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(404, "Company not found")
    if not can_manage_account(user, company, db):
        raise HTTPException(404, "Company not found")

    # Anchored lookup for THIS company only (one trgm-index probe on PG, one O(n) fuzzy
    # anchor pass on SQLite) — never the global all-pairs scan, whose top-50 truncation
    # could silently hide a real near-dup. Inactive accounts get no banner (the lookup
    # filters partners to active companies; the anchor must be active too).
    match = None
    try:
        if company.is_active:
            match = find_company_dup_match(db, company.id, company.normalized_name, threshold=85)
    except Exception as e:  # pragma: no cover - defensive, mirrors data-ops scan guard
        logger.warning("dup-suggestion scan failed for company {}: {}", company_id, e)
        return HTMLResponse("")

    if not match:
        return HTMLResponse("")

    ctx = {"request": request, "company": company, "match": match}
    return template_response("htmx/partials/customers/_dup_suggestion.html", ctx)


@router.get("/v2/partials/customers/{company_id}/name-suggestion", response_class=HTMLResponse)
async def company_name_suggestion(
    request: Request,
    company_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Suggest-only name-normalization chip: surface the suffix-stripped form of the
    current name as "Suggested name: X — Apply?".

    Uses the same normalizer the dedup scanner uses, re-cased from the original tokens
    (so we only strip the legal suffix / leading "the", never lowercase the whole name).
    Renders nothing (empty 200) when the current name is already clean.
    """
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(404, "Company not found")
    if not can_manage_account(user, company, db):
        raise HTTPException(404, "Company not found")

    suggested = suggest_clean_company_name(company.name or "")
    if not suggested or suggested == (company.name or "").strip():
        return HTMLResponse("")

    ctx = {"request": request, "company": company, "suggested": suggested}
    return template_response("htmx/partials/customers/_name_suggestion.html", ctx)


@router.post("/v2/partials/customers/{company_id}/apply-name", response_class=HTMLResponse)
async def company_apply_name(
    request: Request,
    company_id: int,
    name: str = Form(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Apply a suggested company name (rep-initiated; the suggest-only counterpart to a
    silent rewrite).

    normalized_name follows automatically via Company._sync_normalized_name
    (@validates). Returns an empty 200 so the chip removes itself (hx-swap outerHTML).
    """
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(404, "Company not found")
    if not can_manage_account(user, company, db):
        raise HTTPException(403, "Not authorized")

    new_name = (name or "").strip()
    if not new_name:
        raise HTTPException(400, "Name is required")

    company.name = new_name  # @validates resyncs normalized_name
    db.commit()

    invalidate_prefix("company_list")
    logger.info("Company {} renamed to '{}' (suggested) by {}", company_id, new_name, user.email)
    return HTMLResponse("")


@router.post(
    "/v2/partials/customers/{company_id}/primary-contact/{contact_id}",
    response_class=HTMLResponse,
)
async def set_account_primary_contact(
    request: Request,
    company_id: int,
    contact_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Set Company.primary_contact_id to contact_id (account-level primary contact).

    IDOR-safe: verifies contact belongs to a site under company_id.
    Owner-or-admin gate. Returns refreshed company detail partial.
    """
    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")

    if not can_manage_account(user, company, db):
        raise HTTPException(403, "Only the owner or an admin can edit this account")

    # IDOR-safe: verify contact belongs to a site under this company.
    contact = (
        db.query(SiteContact)
        .join(CustomerSite)
        .filter(SiteContact.id == contact_id, CustomerSite.company_id == company_id)
        .first()
    )
    if not contact:
        raise HTTPException(404, "Contact not found")

    company.primary_contact_id = contact_id
    company.updated_at = datetime.now(UTC)
    db.commit()
    db.refresh(company)
    logger.info("Company {} primary contact set to {} by {}", company_id, contact_id, user.email)

    return await _pkg._render_company_detail(request, company_id, user, db)


@router.post(
    "/v2/partials/customers/{company_id}/parent",
    response_class=HTMLResponse,
)
async def set_parent_company(
    request: Request,
    company_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Set Company.parent_company_id; validates no cycle.

    Accepts parent_company_id= form field (empty → clear).
    Cycle guard: rejects self-parent and any descendant as parent.
    Owner-or-admin gate.
    """
    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")

    if not (is_manager_or_admin(user) or company.account_owner_id == user.id):
        raise HTTPException(403, "Only the account owner or a manager can change company hierarchy")

    form = await request.form()
    raw = (form.get("parent_company_id") or "").strip()

    _set_parent_company(db, company, raw)
    company.updated_at = datetime.now(UTC)
    db.commit()
    db.refresh(company)
    logger.info("Company {} parent set to {} by {}", company_id, raw or "None", user.email)

    return await _pkg._render_company_detail(request, company_id, user, db)


# ── Shared helper — parent-company validation used by set_parent_company + edit_company ──


def _set_parent_company(db: Session, company: Company, raw_parent_id: str) -> None:
    """Validate and set company.parent_company_id from the raw form string.

    ``raw_parent_id`` is the stripped string value from the submitted form:
      - empty string → clear the parent (set to None)
      - integer string → validate, cycle-check, then set

    Raises HTTPException(400) for bad input or cycle; HTTPException(404) for missing
    parent. Does NOT commit — caller owns the transaction.
    """
    if not raw_parent_id:
        company.parent_company_id = None
        return

    if not raw_parent_id.isdigit():
        raise HTTPException(400, "parent_company_id must be an integer")

    parent_id = int(raw_parent_id)
    if parent_id == company.id:
        raise HTTPException(400, "A company cannot be its own parent (would create a cycle)")

    parent = db.get(Company, parent_id)
    if not parent:
        raise HTTPException(404, "Parent company not found")

    # Cycle guard: walk ancestor chain of proposed parent; reject if we reach company.id.
    visited: set[int] = set()
    cursor = parent
    while cursor.parent_company_id is not None:
        if cursor.parent_company_id in visited:
            break  # existing cycle in DB — stop walking
        visited.add(cursor.id)
        if cursor.parent_company_id == company.id:
            raise HTTPException(400, "Setting this parent would create a cycle in the company hierarchy")
        cursor = db.get(Company, cursor.parent_company_id)
        if cursor is None:
            break

    company.parent_company_id = parent_id


# ── Phase 3: Account Collaborators (add/remove helpers) ──────────────────


@router.post("/v2/partials/customers/{company_id}/collaborators", response_class=HTMLResponse)
async def add_account_collaborator(
    request: Request,
    company_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Add a helper collaborator to this account.

    Gate: can_manage_account_team (primary owner OR manager/admin ONLY).
    Helpers, site-owners, and unrelated reps are denied (403).
    Validates: user_id exists, is not the primary owner, is not already a collaborator.
    Returns the refreshed collaborators partial.
    """
    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")

    if not can_manage_account_team(user, company):
        raise HTTPException(403, "Only the account owner or a manager can manage the team")

    form = await request.form()
    raw_user_id = (form.get("user_id") or "").strip()
    if not raw_user_id or not raw_user_id.isdigit():
        raise HTTPException(400, "user_id is required and must be an integer")

    target_user_id = int(raw_user_id)
    target_user = db.get(User, target_user_id)
    if not target_user:
        raise HTTPException(404, "User not found")

    if target_user_id == company.account_owner_id:
        raise HTTPException(400, "The primary account owner cannot be added as a collaborator")

    existing = db.query(AccountCollaborator).filter_by(company_id=company_id, user_id=target_user_id).first()
    if existing:
        raise HTTPException(409, "This user is already a collaborator on this account")

    collaborator = AccountCollaborator(company_id=company_id, user_id=target_user_id, role="helper")
    db.add(collaborator)
    db.commit()
    logger.info(
        "Collaborator added: company={} user={} by {}",
        company_id,
        target_user_id,
        user.email,
    )

    return await _collaborators_partial(request, company_id=company_id, user=user, db=db, company=company)


@router.delete(
    "/v2/partials/customers/{company_id}/collaborators/{collab_user_id}",
    response_class=HTMLResponse,
)
async def remove_account_collaborator(
    request: Request,
    company_id: int,
    collab_user_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Remove a helper collaborator from this account.

    Gate: can_manage_account_team (primary owner OR manager/admin ONLY).
    Helpers and unrelated reps are denied (403).
    Returns the refreshed collaborators partial.
    """
    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")

    if not can_manage_account_team(user, company):
        raise HTTPException(403, "Only the account owner or a manager can manage the team")

    # Validate the target user exists (prevents state-probing via silent 200 on garbage ids)
    if not db.get(User, collab_user_id):
        raise HTTPException(404, "User not found")

    collaborator = db.query(AccountCollaborator).filter_by(company_id=company_id, user_id=collab_user_id).first()
    if collaborator:
        db.delete(collaborator)
        db.commit()
        logger.info(
            "Collaborator removed: company={} user={} by {}",
            company_id,
            collab_user_id,
            user.email,
        )

    return await _collaborators_partial(request, company_id=company_id, user=user, db=db, company=company)


async def _collaborators_partial(
    request: Request,
    company_id: int,
    user: User,
    db: Session,
    company: "Company | None" = None,
):
    """Render the collaborators partial for a given company.

    *company* may be passed by callers that already hold the loaded object to avoid a
    second DB fetch.  If omitted, it is fetched here.
    """
    if company is None:
        company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")

    collaborators = (
        db.query(AccountCollaborator, User)
        .join(User, AccountCollaborator.user_id == User.id)
        .filter(AccountCollaborator.company_id == company_id)
        .order_by(User.name)
        .all()
    )
    can_manage_team = can_manage_account_team(user, company)
    all_users = db.query(User).filter(User.is_active.is_(True)).order_by(User.name).all() if can_manage_team else []

    ctx = _base_ctx(request, user, "customers")
    ctx.update(
        {
            "company": company,
            "collaborators": collaborators,
            "all_users": all_users,
            "can_manage_team": can_manage_team,
        }
    )
    return template_response("htmx/partials/customers/_collaborators.html", ctx)


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
    if not can_manage_account(user, company, db):
        raise HTTPException(404, "Company not found")
    users = (
        db.query(User).filter(User.role.in_((UserRole.BUYER, UserRole.TRADER, UserRole.MANAGER, UserRole.ADMIN))).all()
    )
    all_companies = (
        db.query(Company.id, Company.name)
        .filter(Company.id != company_id, Company.is_active.is_(True))
        .order_by(Company.name)
        .all()
    )
    return template_response(
        "htmx/partials/customers/edit_form.html",
        {"request": request, "company": company, "users": users, "all_companies": all_companies},
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
    if not can_manage_account(user, company, db):
        raise HTTPException(403, "Not authorized to manage this account")

    form = await request.form()
    name = form.get("name", "").strip()
    if name:
        # Duplicate-name guard — mirror create_company. Company.name is nullable=False
        # and NOT unique, so nothing else stops a rename colliding with another account.
        # Exclude self (Company.id != company_id) so a no-op or case-only save on the
        # same row doesn't false-positive.
        existing = (
            db.query(Company).filter(sqlfunc.lower(Company.name) == name.lower(), Company.id != company_id).first()
        )
        if existing:
            raise HTTPException(409, f"Company '{existing.name}' already exists (ID {existing.id})")
        company.name = name
    notes = form.get("notes", "").strip()
    company.notes = notes or company.notes
    source = form.get("source", "").strip()
    company.source = source or company.source
    tax_id = form.get("tax_id", "").strip()
    company.tax_id = tax_id or None
    # Owner reassignment is a TEAM action — only the primary owner / a manager may seize
    # primary ownership (can_manage_account admits collaborators + site-owners, who must
    # NOT be able to lock out the real owner). Gate only when the value actually changes.
    owner_id = form.get("owner_id", "")
    if owner_id and owner_id.isdigit():
        new_owner_id = int(owner_id)
        if new_owner_id != company.account_owner_id:
            if not can_manage_account_team(user, company):
                raise HTTPException(403, "Only the account owner or a manager can change the primary owner")
            # The new owner must be a real active user — mirrors create_company and the
            # bulk assign-owner path, so a deactivated/non-existent id can't silently take
            # ownership (or raise an unhandled FK IntegrityError on commit).
            target = db.get(User, new_owner_id)
            if not target or not target.is_active:
                raise HTTPException(400, "Owner must be an active user")
            company.account_owner_id = new_owner_id

    parent_company_id_raw = form.get("parent_company_id", "").strip()
    # Parent-company (hierarchy) edits are also a team action — match set_parent_company,
    # which gates on owner/manager — so a collaborator can't restructure the hierarchy.
    if parent_company_id_raw != (str(company.parent_company_id or "")):
        if not can_manage_account_team(user, company):
            raise HTTPException(403, "Only the account owner or a manager can change company hierarchy")
    _set_parent_company(db, company, parent_company_id_raw)

    # Registry fields — DRY via apply_company_field.
    # notes/source use blank-sentinel "preserve current value" semantics above; tax_id is
    # explicitly clear-on-blank (a submitted blank sets it to NULL). All three are handled
    # above, so skip them here to keep that behaviour — the registry loop must not re-touch
    # notes/source (its clear-on-blank would wipe a value the user left untouched).
    _form_handled = {"notes", "source", "tax_id"}
    for f in EDITABLE_ACCOUNT_FIELDS:
        if f in _form_handled:
            continue
        raw = form.get(f)
        if raw is not None:  # field was submitted
            apply_company_field(company, f, raw)
    # updated_at set inside apply_company_field; ensure it's set for non-registry writes too
    company.updated_at = datetime.now(UTC)
    db.commit()
    logger.info("Company {} edited by {}", company_id, user.email)

    # Refreshed detail replaces the detail root in place (form hx-target=#company-detail-<id>,
    # outerHTML) so it works in both the workspace and a deep-linked full page. The HX-Trigger
    # refreshes the left account list too (name/owner/type edits change the row) when present.
    resp = await _pkg._render_company_detail(request, company_id, user, db)
    resp.headers["HX-Trigger"] = "cdmListRefresh"
    return resp


# ── Inline Field Edit — Account (WS1) ─────────────────────────────────────


@router.get(
    "/v2/partials/customers/{company_id}/field/edit/{field}",
    response_class=HTMLResponse,
)
async def company_field_edit_form(
    request: Request,
    company_id: int,
    field: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return the inline edit widget for a single account field."""
    if field not in EDITABLE_ACCOUNT_FIELDS:
        raise HTTPException(404, f"Unknown editable field: {field!r}")
    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")
    if not can_manage_account(user, company, db):
        raise HTTPException(404, "Company not found")
    meta = EDITABLE_ACCOUNT_FIELDS[field]
    return template_response(
        "htmx/partials/customers/_field_edit.html",
        {
            **_base_ctx(request, user),
            "obj": company,
            "field": field,
            "entity": "company",
            "meta": meta,
            "post_url": f"/v2/partials/customers/{company_id}/field",
            "display_url": f"/v2/partials/customers/{company_id}/field/display/{field}",
        },
    )


@router.get(
    "/v2/partials/customers/{company_id}/field/display/{field}",
    response_class=HTMLResponse,
)
async def company_field_display(
    request: Request,
    company_id: int,
    field: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return the display span for a single account field (cancel path)."""
    if field not in EDITABLE_ACCOUNT_FIELDS:
        raise HTTPException(404, f"Unknown editable field: {field!r}")
    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")
    if not can_manage_account(user, company, db):
        raise HTTPException(404, "Company not found")
    meta = EDITABLE_ACCOUNT_FIELDS[field]
    return template_response(
        "htmx/partials/customers/_field_display.html",
        {
            **_base_ctx(request, user),
            "obj": company,
            "field": field,
            "entity": "company",
            "meta": meta,
            "edit_url": f"/v2/partials/customers/{company_id}/field/edit/{field}",
        },
    )


@router.post(
    "/v2/partials/customers/{company_id}/field",
    response_class=HTMLResponse,
)
async def company_field_post(
    request: Request,
    company_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Save a single inline-edited account field; return the display span."""
    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")
    if not can_manage_account(user, company, db):
        raise HTTPException(403, "Only the owner or an admin can edit this account")
    form = await request.form()
    field = (form.get("field") or "").strip()
    if field not in EDITABLE_ACCOUNT_FIELDS:
        raise HTTPException(404, f"Unknown editable field: {field!r}")
    value = form.get("value") or ""
    old_value = getattr(company, field, None)
    apply_company_field(company, field, value)
    record_field_change(
        db,
        entity_type=ENTITY_COMPANY,
        entity_id=company.id,
        field_name=field,
        old_value=old_value,
        new_value=getattr(company, field, None),
        user_id=user.id,
    )
    db.commit()
    logger.info("Company {} field {} edited inline by {}", company_id, field, user.email)
    meta = EDITABLE_ACCOUNT_FIELDS[field]
    return template_response(
        "htmx/partials/customers/_field_display.html",
        {
            **_base_ctx(request, user),
            "obj": company,
            "field": field,
            "entity": "company",
            "meta": meta,
            "edit_url": f"/v2/partials/customers/{company_id}/field/edit/{field}",
        },
    )
