"""routers/htmx/companies/contacts/listing.py — global customer-contacts list, bulk
actions, contact CSV import, for-select options (W4.8 split of contacts.py).

The cross-company customer-contacts list (``/v2/partials/contacts``), contact
bulk archive/DNC, the contact CSV import preview/confirm pair, and the
company-scoped contacts ``for-select`` JSON options. Pure structural move:
URLs and behavior unchanged; every route attaches to the shared router
imported from .common (registration assembled in contacts/__init__).

Called by: app.routers.htmx.companies.contacts (package __init__ re-export,
    route registration)
Depends on: app.services.crm_service, app.services.company_import_service,
    .._registries, ..saved_views, ..._shared, .common
"""

import json

from fastapi import Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from loguru import logger
from sqlalchemy.orm import Session

from .....database import get_db
from .....dependencies import can_manage_account, is_manager_or_admin, require_user
from .....dependencies import manageable_company_ids as _manageable_company_ids
from .....models import Company, CustomerSite, SiteContact, User
from .....services.company_import_service import confirm_contact_import, parse_csv_rows, preview_contact_import
from .....services.crm_service import customer_contacts_list_ctx
from .....template_env import template_response
from ..._shared import _base_ctx
from .._registries import BULK_MAX_IDS as _BULK_MAX_IDS
from .._registries import (
    CANONICAL_ROLES,
)
from ..saved_views import _saved_views_ctx
from .common import _contacts_list_response, router

# ── Global customer-contacts list (cross-company, role-scoped) ─────────────
# /v2/contacts is cross-tenant PII: SALES/TRADER reps see ONLY contacts in
# accounts they can manage (shared company_visibility_predicate); MANAGER/ADMIN
# see all. Scoping lives in crm_service.customer_contacts_query — this route is
# thin HTTP glue.


@router.get("/v2/partials/contacts", response_class=HTMLResponse)
async def customer_contacts_partial(
    request: Request,
    search: str = "",
    company_id: int = Query(0, ge=0),
    contact_role: str = "",
    cadence_state: str = "",
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return the cross-company customer-contacts workspace as an HTML partial."""
    ctx = _base_ctx(request, user, "crm")
    ctx.update(
        customer_contacts_list_ctx(
            db,
            user,
            search=search,
            company_id=company_id,
            contact_role=contact_role,
            cadence_state=cadence_state,
            limit=limit,
            offset=offset,
        )
    )
    ctx["contact_roles"] = CANONICAL_ROLES
    ctx.update(_saved_views_ctx(request, user, db, "contacts"))
    return template_response("htmx/partials/customers/contacts_list.html", ctx)


# ── Contacts bulk actions (static — global cross-company contacts list) ────
# Mirrors the accounts bulk pattern (customers_bulk_action, .core): per-contact auth via
# can_manage_account on the owning company; manager/admin act on all. Selected
# contacts the caller cannot manage are silently skipped (summary reports both).
_VALID_BULK_CONTACT_ACTIONS = frozenset({"archive", "dnc"})


@router.post("/v2/partials/contacts/bulk/{action}", response_class=HTMLResponse)
async def contacts_bulk_action(
    request: Request,
    action: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Apply a bulk action (archive | dnc) to selected contacts.

    Auth: per-contact via can_manage_account on the owning company (manager/admin
    act on all). Non-manageable contacts are silently skipped; the summary reports
    applied vs skipped. Re-renders the contacts list scoped to the included filters.
    """
    if action not in _VALID_BULK_CONTACT_ACTIONS:
        raise HTTPException(400, f"Invalid action '{action}'. Allowed: {sorted(_VALID_BULK_CONTACT_ACTIONS)}")

    form = await request.form()
    ids_str = form.get("ids", "") or ""
    ids = [int(x.strip()) for x in ids_str.split(",") if x.strip().isdigit()]
    if len(ids) > _BULK_MAX_IDS:
        raise HTTPException(400, f"Maximum {_BULK_MAX_IDS} contacts per bulk action")

    applied = 0
    skipped = 0
    if ids:
        rows = (
            db.query(SiteContact, Company)
            .join(CustomerSite, SiteContact.customer_site_id == CustomerSite.id)
            .join(Company, CustomerSite.company_id == Company.id)
            .filter(SiteContact.id.in_(ids))
            .all()
        )
        is_mgr = is_manager_or_admin(user)
        manageable_ids = set() if is_mgr else _manageable_company_ids(user, [company for _, company in rows], db)
        for contact, company in rows:
            if is_mgr or company.id in manageable_ids:
                if action == "archive":
                    contact.is_archived = True
                elif action == "dnc":
                    contact.do_not_contact = True
                applied += 1
            else:
                skipped += 1
        if applied:
            db.commit()
            logger.info(
                "Bulk contact {} applied to {} contacts ({} skipped) by {}",
                action,
                applied,
                skipped,
                user.email,
            )

    label = {"archive": "Archived", "dnc": "Marked Do-Not-Contact"}.get(action, action.title())
    if skipped:
        msg = f"{label} {applied} of {applied + skipped} ({skipped} skipped — not yours)"
    else:
        msg = f"{label} {applied} contact{'s' if applied != 1 else ''}"

    resp = _contacts_list_response(request, user, db, form)
    resp.headers["HX-Trigger"] = json.dumps({"showToast": {"message": msg}, "clearSelection": True})
    return resp


# ── Contact CSV import: preview + confirm ──────────────────────────────────
# Business logic (CSV parse, dedup queries, row creation) lives in
# app.services.company_import_service — these routes stay HTTP-only (P4.2).


@router.post("/v2/partials/customers/import/contacts/preview", response_class=HTMLResponse)
async def import_contacts_preview(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Parse contact CSV and return a preview table (no writes).

    Expected columns: company_name (required), contact_name (required), email, phone, role.
    Flags: duplicate (email collision in site_contacts), invalid (missing required fields).
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
        preview = preview_contact_import(db, raw_rows, user)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    return template_response(
        "htmx/partials/customers/_import_contacts_preview.html",
        {
            "request": request,
            "rows": preview["rows"],
            "valid_count": preview["valid_count"],
            "dup_count": preview["dup_count"],
            "invalid_count": preview["invalid_count"],
            "unauthorized_count": preview["unauthorized_count"],
            "rows_json": json.dumps(preview["valid_rows"]),
        },
    )


@router.post("/v2/partials/customers/import/contacts/confirm", response_class=HTMLResponse)
async def import_contacts_confirm(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Create SiteContact rows from a confirmed contacts import.

    Per row: matches company by normalized_name or domain; attaches contact to
    the company's first ACTIVE site (creates an HQ site if none exists);
    deduplicates by email within the site; skips rows whose company isn't found.
    Reports created, skipped_no_company, and skipped_dup counts.
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
        result = confirm_contact_import(db, rows, user)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    resp = template_response(
        "htmx/partials/customers/_import_confirm_summary.html",
        {"request": request, "summary": result["summary"]},
    )
    resp.headers["HX-Trigger"] = json.dumps({"showToast": {"message": result["summary"]}})
    return resp


@router.get("/v2/partials/customers/{company_id}/contacts/for-select")
async def get_company_contacts_for_select(
    company_id: int,
    exclude_id: int | None = None,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return active site contacts for a company as JSON for the reports_to select.

    Excludes the contact with exclude_id (self-exclusion for reports_to picker).
    """
    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")
    if not can_manage_account(user, company, db):
        raise HTTPException(403, "Not authorized to manage this account")
    q = (
        db.query(SiteContact)
        .join(CustomerSite, SiteContact.customer_site_id == CustomerSite.id)
        .filter(
            CustomerSite.company_id == company_id,
            SiteContact.is_active.is_(True),
        )
    )
    if exclude_id:
        q = q.filter(SiteContact.id != exclude_id)
    contacts = q.order_by(SiteContact.full_name).all()
    return [{"id": c.id, "name": c.full_name or c.first_name or "—"} for c in contacts]
