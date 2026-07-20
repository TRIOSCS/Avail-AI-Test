"""routers/htmx/companies/contacts.py — Contacts-tab CRUD, bulk actions, suggested-
contacts discovery, contact merge/move support, and contact notes/history/files (P4.3
split).

The cross-company customer-contacts list (``/v2/partials/contacts``), the Contacts-tab
add/create/suggested-discovery/move loops, per-contact toggles (role/DNC/priority/
archive), inline field editing, and the notes/history/files modals. Also owns
``_render_contacts_list`` — the shared grouped-list re-render used by this module AND
by ``.sites`` / ``.merge`` after their own contact-affecting mutations.

Called by: app.routers.htmx.companies (package __init__ re-export, route registration),
    .sites, .merge (``_render_contacts_list``); tests monkeypatch
    ``app.routers.htmx.companies._run_contact_discovery`` (via the package attribute —
    resolved dynamically through ``_pkg`` at call time so the patch takes effect).
Depends on: app.services.crm_service, app.services.activity_service,
    app.services.crm_field_history, app.services.contact_discovery_runs,
    app.enrichment_service, app.services.company_import_service, ._registries,
    .saved_views
"""

import html as html_mod
import json
from datetime import UTC, datetime

import httpx
from fastapi import BackgroundTasks, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from loguru import logger
from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session

import app.routers.htmx.companies as _pkg
from app.services.contact_discovery_runs import ContactDiscoveryOutcome, contact_discovery_runs

from ....database import get_db
from ....dependencies import can_manage_account, is_manager_or_admin, require_user
from ....dependencies import manageable_company_ids as _manageable_company_ids
from ....models import AccountCollaborator, Company, CustomerSite, SiteContact, User
from ....services.activity_service import get_site_contact_notes, log_site_contact_note
from ....services.company_import_service import confirm_contact_import, parse_csv_rows, preview_contact_import
from ....services.crm_field_history import ENTITY_CONTACT, field_history_for, record_field_change
from ....services.crm_service import company_contact_rows, customer_contacts_list_ctx
from ....template_env import template_response
from .._shared import _base_ctx
from . import router
from ._registries import BULK_MAX_IDS as _BULK_MAX_IDS
from ._registries import (
    CANONICAL_ROLES,
    EDITABLE_CONTACT_FIELDS,
    FIELD_LABELS,
    _recompose_full_name,
    _validate_role,
    apply_contact_field,
)
from .saved_views import _saved_views_ctx

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


def _form_int(form, key: str, default: int = 0) -> int:
    raw = (form.get(key) or "").strip()
    return int(raw) if raw.isdigit() else default


def _contacts_list_response(request: Request, user: User, db: Session, form, prefix: str = "") -> HTMLResponse:
    """Re-render the global contacts list from the hx-included filter fields.

    prefix: read filter values from prefixed form keys (e.g. "filter_search") — used by
    the origin=contacts edit-modal save, whose form carries its own contact_role field
    and so namespaces the list filters to avoid the name collision.
    """
    ctx = _base_ctx(request, user, "crm")
    ctx.update(
        customer_contacts_list_ctx(
            db,
            user,
            search=(form.get(f"{prefix}search") or "").strip(),
            company_id=_form_int(form, f"{prefix}company_id"),
            contact_role=(form.get(f"{prefix}contact_role") or "").strip(),
            cadence_state=(form.get(f"{prefix}cadence_state") or "").strip(),
            limit=_form_int(form, f"{prefix}limit", 50),
            offset=_form_int(form, f"{prefix}offset", 0),
        )
    )
    ctx["contact_roles"] = CANONICAL_ROLES
    ctx.update(_saved_views_ctx(request, user, db, "contacts"))
    return template_response("htmx/partials/customers/contacts_list.html", ctx)


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


def _render_contacts_list(request: Request, user: User, company: Company, db: Session) -> HTMLResponse:
    """Build and return the contacts grouped-list partial for the Contacts tab.

    Shared by create, add-suggested, delete, set-primary, and edit endpoints (in this
    module AND in .sites / .merge) so every swap path stays in sync with one another.
    """
    ctx = _base_ctx(request, user, "customers")
    ctx.update(
        {
            "company": company,
            "contact_rows": company_contact_rows(db, company.id, viewer=user),
            "now_utc": datetime.now(UTC),
            "roles": CANONICAL_ROLES,
        }
    )
    return template_response("htmx/partials/customers/tabs/_contacts_grouped_list.html", ctx)


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

    if not can_manage_account(user, company, db):
        raise HTTPException(403, "Only the owner or an admin can edit this contact")

    form = await request.form()
    contact.contact_role = _validate_role(form.get("contact_role") or "")
    db.commit()
    db.refresh(contact)

    logger.info(
        "Contact {} role set to {} by {} (company {})",
        contact_id,
        contact.contact_role,
        user.email,
        company_id,
    )
    return _render_contacts_list(request, user, company, db)


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

    if not can_manage_account(user, company, db):
        raise HTTPException(403, "Only the owner or an admin can edit this contact")

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
    return _render_contacts_list(request, user, company, db)


@router.post(
    "/v2/partials/customers/{company_id}/contacts/{contact_id}/priority",
    response_class=HTMLResponse,
)
async def set_contact_priority(
    request: Request,
    company_id: int,
    contact_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Set or clear SiteContact.is_priority; re-renders the priority toggle partial.

    IDOR-safe: the contact must belong to a site under this company. Non-empty
    is_priority= → True; empty → False (clear).
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

    if not can_manage_account(user, company, db):
        raise HTTPException(403, "Only the owner or an admin can edit this contact")

    form = await request.form()
    contact.is_priority = bool((form.get("is_priority") or "").strip())
    db.commit()
    db.refresh(contact)

    logger.info(
        "Contact {} is_priority set to {} by {} (company {})",
        contact_id,
        contact.is_priority,
        user.email,
        company_id,
    )
    return _render_contacts_list(request, user, company, db)


@router.post(
    "/v2/partials/customers/{company_id}/contacts/{contact_id}/archive",
    response_class=HTMLResponse,
)
async def set_contact_archive(
    request: Request,
    company_id: int,
    contact_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Set or clear SiteContact.is_archived; re-renders the archive toggle partial.

    IDOR-safe: the contact must belong to a site under this company. Non-empty
    is_archived= → True; empty → False (restore). Archived contacts stay visible
    (sorted to the bottom) — is_active is never touched here.
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

    if not can_manage_account(user, company, db):
        raise HTTPException(403, "Only the owner or an admin can edit this contact")

    form = await request.form()
    contact.is_archived = bool((form.get("is_archived") or "").strip())
    db.commit()
    db.refresh(contact)

    logger.info(
        "Contact {} is_archived set to {} by {} (company {})",
        contact_id,
        contact.is_archived,
        user.email,
        company_id,
    )
    return _render_contacts_list(request, user, company, db)


# ── Contacts-tab management (C2) ───────────────────────────────────────


@router.get(
    "/v2/partials/customers/{company_id}/contacts/add-form",
    response_class=HTMLResponse,
)
async def contacts_tab_add_form(
    request: Request,
    company_id: int,
    site_id: int | None = None,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return the shared _contact_form.html in add mode for the Contacts tab modal.

    Optional site_id pre-selects that site in the form's Site dropdown — set by the "+
    add here" affordance on a per-site section header (Contacts surface).
    """
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(404, "Company not found")

    active_sites = (
        db.query(CustomerSite)
        .filter(CustomerSite.company_id == company_id, CustomerSite.is_active.is_(True))
        .order_by(CustomerSite.site_name)
        .all()
    )
    # Only honor site_id when it belongs to THIS company's active sites (IDOR-safe).
    preselect_site_id = site_id if any(s.id == site_id for s in active_sites) else None
    users = db.query(User).order_by(User.name).all()
    return template_response(
        "htmx/partials/customers/tabs/_contact_form.html",
        {
            "request": request,
            "mode": "add",
            "company": company,
            "contact": None,
            "site": None,
            "sites": active_sites,
            "preselect_site_id": preselect_site_id,
            "roles": CANONICAL_ROLES,
            "users": users,
        },
    )


@router.post(
    "/v2/partials/customers/{company_id}/contacts",
    response_class=HTMLResponse,
)
async def contacts_tab_create(
    request: Request,
    company_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Create a contact from the Contacts tab modal and return the grouped list.

    Site resolution order:
      1. site_id == '__new__' + new_site_name → create site first using a single
         SQLAlchemy transaction — db.flush() sends the site INSERT to PG but does
         not commit; if db.commit() fails the entire transaction (site + contact)
         rolls back atomically.
      2. site_id valid int → use that site
      3. site_id blank/missing → auto-create an 'HQ' site for zero-site companies;
         for companies with sites default to the first active HQ-typed site.

    After resolving the site, creates SiteContact with email dedup per-site.
    Duplicate email on the same site returns HTTP 409 with a user-visible error.
    Returns the grouped list HTML for swap into #contacts-tab-list.
    """
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(404, "Company not found")
    if not can_manage_account(user, company, db):
        raise HTTPException(403, "Not authorized")

    form = await request.form()
    # Step 4: accept first_name + last_name (new form) or full_name (legacy fallback).
    first_name_val = (form.get("first_name") or "").strip() or None
    last_name_val = (form.get("last_name") or "").strip() or None
    full_name_legacy = (form.get("full_name") or "").strip()

    if first_name_val or last_name_val:
        # New form: compose full_name from parts
        if not first_name_val and not last_name_val:
            raise HTTPException(400, "At least one of first_name or last_name is required")
        full_name = f"{first_name_val or ''} {last_name_val or ''}".strip()
    elif full_name_legacy:
        # Legacy: full_name submitted directly; split into parts
        full_name = full_name_legacy
        parts = full_name.split(" ", 1)
        first_name_val = parts[0] or None
        last_name_val = parts[1].strip() if len(parts) > 1 else None
    else:
        raise HTTPException(400, "full_name is required")

    email_val = (form.get("email") or "").strip().lower() or None
    if email_val and "@" not in email_val:
        raise HTTPException(400, "Invalid email address")

    site_id_raw = (form.get("site_id") or "").strip()
    new_site_name = (form.get("new_site_name") or "").strip()

    # ── Resolve or pre-validate the site (no writes yet) ───────────────
    # For existing sites, resolve + validate before any writes so dedup
    # can return cleanly without needing a rollback.
    existing_site: CustomerSite | None = None  # already-persisted site

    if site_id_raw == "__new__":
        if not new_site_name:
            raise HTTPException(400, "new_site_name is required when site_id=__new__")
        # Site will be created in the commit below
    elif site_id_raw:
        try:
            sid = int(site_id_raw)
        except ValueError as e:
            raise HTTPException(400, "Invalid site_id") from e
        existing_site = (
            db.query(CustomerSite).filter(CustomerSite.id == sid, CustomerSite.company_id == company_id).first()
        )
        if not existing_site:
            raise HTTPException(404, "Site not found")
    else:
        # No site_id provided — resolve default or mark for auto-create
        current_sites = (
            db.query(CustomerSite)
            .filter(CustomerSite.company_id == company_id, CustomerSite.is_active.is_(True))
            .order_by(CustomerSite.site_name)
            .all()
        )
        if current_sites:
            hq_site = next((s for s in current_sites if (s.site_type or "") == "hq"), None)
            existing_site = hq_site or current_sites[0]
        # else: zero-site → will auto-create below

    # ── Per-site email dedup (only possible for existing sites) ─────────
    # For __new__ + zero-site cases the target site has no ID yet, so no dedup needed.
    if email_val and existing_site:
        dup = (
            db.query(SiteContact)
            .filter(
                SiteContact.customer_site_id == existing_site.id,
                sqlfunc.lower(SiteContact.email) == email_val,
            )
            .first()
        )
        if dup:
            # Dedup: 409 so the user knows the contact was not created
            raise HTTPException(409, f"A contact with email {email_val} already exists at this site")

    # ── Create site if needed (inside one transaction with the contact) ──
    if existing_site:
        site = existing_site
    elif site_id_raw == "__new__":
        site = CustomerSite(company_id=company_id, site_name=new_site_name, is_active=True)
        db.add(site)
        db.flush()  # get site.id before creating contact
    else:
        # Zero-site auto-create HQ
        site = CustomerSite(company_id=company_id, site_name="HQ", site_type="hq", is_active=True)
        db.add(site)
        db.flush()

    # ── Validate role ───────────────────────────────────────────────────
    role = _validate_role(form.get("contact_role") or "")
    is_priority = bool((form.get("is_priority") or "").strip())

    # SiteContact.wechat_id is String(100); SQLite (tests) ignores VARCHAR lengths
    # but Postgres 500s on over-length. Reject here, matching the legacy
    # create_site_contact guard so the canonical add path is consistent.
    wechat_id_val = (form.get("wechat_id") or "").strip()
    if len(wechat_id_val) > 100:
        raise HTTPException(400, "WeChat ID must be 100 characters or fewer.")

    # ── reports_to_id (self-FK — not in EDITABLE_CONTACT_FIELDS) ────────────
    reports_to_id_raw = (form.get("reports_to_id") or "").strip()
    reports_to_id = int(reports_to_id_raw) if reports_to_id_raw.isdigit() else None
    if reports_to_id is not None:
        mgr = (
            db.query(SiteContact)
            .join(CustomerSite, SiteContact.customer_site_id == CustomerSite.id)
            .filter(SiteContact.id == reports_to_id, CustomerSite.company_id == company_id)
            .first()
        )
        if not mgr:
            raise HTTPException(400, "reports_to must be a contact in the same company")

    # ── Create contact ──────────────────────────────────────────────────
    # contact_owner_id is intentionally NOT read from the form — ownership
    # flows via site → account owner (per-contact picker removed in Phase 1).
    contact = SiteContact(
        customer_site_id=site.id,
        full_name=full_name,
        first_name=first_name_val,
        last_name=last_name_val,
        email=email_val,
        title=(form.get("title") or "").strip() or None,
        phone=(form.get("phone") or "").strip() or None,
        secondary_email=(form.get("secondary_email") or "").strip() or None,
        secondary_phone=(form.get("secondary_phone") or "").strip() or None,
        wechat_id=wechat_id_val or None,
        notes=(form.get("notes") or "").strip() or None,
        linkedin_url=(form.get("linkedin_url") or "").strip() or None,
        contact_role=role,
        is_priority=is_priority,
        reports_to_id=reports_to_id,
    )
    db.add(contact)
    db.commit()
    logger.info(
        "Contact created for company {} site {} by {}",
        company_id,
        site.id,
        user.email,
    )
    return _render_contacts_list(request, user, company, db)


# ── Suggested-contacts UI (account-building loop) ──────────────────────


async def _run_contact_discovery(company_id: int, domain: str, name: str) -> None:
    """Background worker: run the contact-discovery waterfall for one company.

    Scheduled by ``contacts_tab_suggested`` (the "Find contacts" button) so the click never
    blocks on the ~10-40s of external-provider calls (``find_suggested_contacts_with_errors``:
    Hunter/Clay/Lusha/Explorium). The transient result (discovered contacts + which providers
    errored) is recorded in ``contact_discovery_runs`` so the status poller can render the same
    ``_suggested_contacts.html`` panel the old synchronous path produced.

    This is a pure external-API call — it never touches the DB — so, unlike the account-enrich
    runner, it opens no session. It degrades gracefully: any failure is folded into
    ``errored_providers`` (the amber "couldn't reach" banner), mirroring the old inline
    behavior. Must NEVER raise: it is a fire-and-forget task.
    """
    # Import kept function-local (not hoisted) — tests monkeypatch
    # app.enrichment_service.find_suggested_contacts_with_errors via that exact
    # module-attribute path, which only takes effect on a fresh lookup per call.
    from app.enrichment_service import find_suggested_contacts_with_errors

    suggested: list[dict] = []
    errored: list[str] = []
    try:
        suggested, errored = await find_suggested_contacts_with_errors(domain, name)
    except Exception as exc:
        if isinstance(exc, (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError)):
            logger.warning("Contact discovery connectivity error for company {}: {}", company_id, exc)
        else:
            logger.error("Contact discovery unexpected error for company {}: {}", company_id, exc, exc_info=True)
        suggested = []
        errored = ["all"]
    finally:
        contact_discovery_runs.finish(
            company_id,
            ContactDiscoveryOutcome(suggested=suggested, errored_providers=errored),
        )


@router.get(
    "/v2/partials/customers/{company_id}/suggested-contacts",
    response_class=HTMLResponse,
)
async def contacts_tab_suggested(
    request: Request,
    company_id: int,
    background_tasks: BackgroundTasks,
    domain: str = "",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """The "Find contacts" button — **enqueue** the discovery waterfall, return a
    poller.

    The multi-provider suggested-contacts waterfall (Hunter/Clay/Lusha/Explorium, ~10-40s)
    used to run INLINE here, so the click felt hung. It now schedules that work as a FastAPI
    background task and returns the "Finding contacts…" poller immediately; the status route
    (:contacts_tab_suggested_status) swaps in the ``_suggested_contacts.html`` result panel
    once the run lands (or the amber "couldn't reach" banner if providers degraded).
    """
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(404, "Company not found")

    if not domain:
        domain = company.domain or company.website or ""
    if not domain:
        raise HTTPException(400, "No domain available for this company")

    # Normalize (strip scheme/www/path)
    domain = domain.replace("https://", "").replace("http://", "").replace("www.", "").split("/")[0]

    # Double-enqueue guard: a run already in flight must not stack a second waterfall.
    # The scheduled callable is resolved off the PACKAGE attribute (not this module's own
    # global) so a test that monkeypatches app.routers.htmx.companies._run_contact_discovery
    # replaces what actually runs.
    if contact_discovery_runs.begin(company_id):
        background_tasks.add_task(_pkg._run_contact_discovery, company_id, domain, company.name or "")

    return template_response(
        "htmx/partials/customers/tabs/_suggested_contacts_finding.html",
        {"request": request, "company": company},
    )


@router.get(
    "/v2/partials/customers/{company_id}/suggested-contacts/status",
    response_class=HTMLResponse,
)
async def contacts_tab_suggested_status(
    request: Request,
    company_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Poll target for the "Finding contacts…" panel — reflects the background run's
    state.

    While the run is in flight, re-renders the poller (keep polling). When it lands, returns
    the ``_suggested_contacts.html`` result panel (discovered contacts / neutral empty state /
    amber "couldn't reach" banner) and answers HTTP 286 to STOP polling. A deleted company or
    an already-consumed outcome stops polling with an empty body.
    """
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        # Polling sub-resource: htmx neither swaps nor cancels an `every 2s` poll on a 4xx,
        # so a 404 would leave the panel hammering this route. 286 stops it; empty clears it.
        return HTMLResponse("", status_code=286)

    if contact_discovery_runs.is_running(company_id):
        return template_response(
            "htmx/partials/customers/tabs/_suggested_contacts_finding.html",
            {"request": request, "company": company},
        )

    outcome = contact_discovery_runs.consume_outcome(company_id)
    if outcome is None:
        # No run in flight and no pending outcome (already consumed, or lost on restart) —
        # stop polling and clear the panel.
        return HTMLResponse("", status_code=286)

    active_sites = (
        db.query(CustomerSite)
        .filter(CustomerSite.company_id == company_id, CustomerSite.is_active.is_(True))
        .order_by(CustomerSite.site_name)
        .all()
    )
    ctx = _base_ctx(request, user, "customers")
    ctx.update(
        {
            "company": company,
            "suggested": outcome.suggested,
            "errored_providers": outcome.errored_providers,
            "active_sites": active_sites,
        }
    )
    response = template_response("htmx/partials/customers/tabs/_suggested_contacts.html", ctx)
    response.status_code = 286  # htmx's stop-polling status — the result panel still swaps in.
    return response


@router.post(
    "/v2/partials/customers/{company_id}/suggested-contacts/add",
    response_class=HTMLResponse,
)
async def contacts_tab_add_suggested(
    request: Request,
    company_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Add a single suggested contact to a site and return the grouped list with toast.

    Form fields: site_id (int), full_name, email, title, phone, linkedin_url,
    source (default "enrichment"), email_verified ("1" / "true" = True),
    from_enrich ("1" when posted from the header Enrich result panel).

    Dedup: if the email already exists on the site, returns a "already on file"
    toast — never a silent no-op or 409 error.
    Returns _contacts_grouped_list.html + HX-Trigger toast for the Contacts tab; when
    from_enrich=1, returns a self-contained "✓ Added" <li> fragment (the enrich panel
    lives outside the Contacts tab, so it self-swaps the clicked row via outerHTML).
    """
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(404, "Company not found")
    if not can_manage_account(user, company, db):
        raise HTTPException(403, "Not authorized")

    form = await request.form()
    full_name = (form.get("full_name") or "").strip()
    if not full_name:
        raise HTTPException(400, "full_name is required")

    site_id_raw = (form.get("site_id") or "").strip()
    email_val = (form.get("email") or "").strip().lower() or None
    title_val = (form.get("title") or "").strip() or None
    phone_val = (form.get("phone") or "").strip() or None
    linkedin_val = (form.get("linkedin_url") or "").strip() or None
    source_val = (form.get("source") or "").strip() or "enrichment"
    email_verified = (form.get("email_verified") or "").strip().lower() in ("1", "true", "yes")

    # Resolve site
    if site_id_raw:
        try:
            sid = int(site_id_raw)
        except ValueError as e:
            raise HTTPException(400, "Invalid site_id") from e
        site = db.query(CustomerSite).filter(CustomerSite.id == sid, CustomerSite.company_id == company_id).first()
        if not site:
            raise HTTPException(404, "Site not found")
    else:
        # Default to HQ site
        sites = (
            db.query(CustomerSite).filter(CustomerSite.company_id == company_id, CustomerSite.is_active.is_(True)).all()
        )
        hq = next((s for s in sites if (s.site_type or "") == "hq"), None)
        site = hq or (sites[0] if sites else None)
        if not site:
            raise HTTPException(400, "No site available — create a site first")

    # Per-site email dedup
    deduped = False
    if email_val:
        existing = (
            db.query(SiteContact)
            .filter(
                SiteContact.customer_site_id == site.id,
                sqlfunc.lower(SiteContact.email) == email_val,
            )
            .first()
        )
        if existing:
            deduped = True
    elif full_name:
        existing_name = (
            db.query(SiteContact)
            .filter(
                SiteContact.customer_site_id == site.id,
                SiteContact.email.is_(None),
                sqlfunc.lower(SiteContact.full_name) == full_name.lower(),
            )
            .first()
        )
        if existing_name:
            deduped = True

    if not deduped:
        sc = SiteContact(
            customer_site_id=site.id,
            full_name=full_name,
            email=email_val,
            title=title_val,
            phone=phone_val,
            linkedin_url=linkedin_val,
            enrichment_source=source_val,
            email_verified=email_verified,
        )
        db.add(sc)
        db.commit()
        logger.info(
            "add_suggested: created SiteContact for company {} site {} by {}",
            company_id,
            site.id,
            user.email,
        )
        toast_msg = f"Added {full_name}"
        toast_kind = "success"
    else:
        toast_msg = f"{full_name} is already on file"
        toast_kind = "info"

    # Enrich-panel Add: the result panel lives outside the Contacts tab (no
    # #contacts-tab-list to re-render), so when the post is flagged from_enrich return a
    # self-contained confirmation row that swaps the clicked <li> in place (hx-swap=outerHTML).
    if (form.get("from_enrich") or "") == "1":
        return HTMLResponse(
            '<li class="px-4 py-3 bg-emerald-50 text-sm text-emerald-700 flex items-center gap-2">'
            '<svg class="h-4 w-4 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" '
            'stroke-width="2.5"><path stroke-linecap="round" stroke-linejoin="round" d="M5 13l4 4L19 7"/></svg>'
            f"{html_mod.escape(toast_msg)}</li>",
            headers={"HX-Trigger": json.dumps({"showToast": {"message": toast_msg, "type": toast_kind}})},
        )

    response = _render_contacts_list(request, user, company, db)
    response.headers["HX-Trigger"] = json.dumps({"showToast": {"message": toast_msg, "type": toast_kind}})
    return response


# ── Inline Field Edit — Contact (WS1) ──────────────────────────────────────


@router.get(
    "/v2/partials/customers/{company_id}/contacts/{contact_id}/field/edit/{field}",
    response_class=HTMLResponse,
)
async def contact_field_edit_form(
    request: Request,
    company_id: int,
    contact_id: int,
    field: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return the inline edit widget for a single contact field."""
    if field not in EDITABLE_CONTACT_FIELDS:
        raise HTTPException(404, f"Unknown editable contact field: {field!r}")
    contact = (
        db.query(SiteContact)
        .join(CustomerSite)
        .filter(SiteContact.id == contact_id, CustomerSite.company_id == company_id)
        .first()
    )
    if not contact:
        raise HTTPException(404, "Contact not found")
    meta = EDITABLE_CONTACT_FIELDS[field]
    extra: dict = {}
    return template_response(
        "htmx/partials/customers/_field_edit.html",
        {
            **_base_ctx(request, user),
            "obj": contact,
            "field": field,
            "entity": "contact",
            "meta": meta,
            "post_url": f"/v2/partials/customers/{company_id}/contacts/{contact_id}/field",
            "display_url": f"/v2/partials/customers/{company_id}/contacts/{contact_id}/field/display/{field}",
            **extra,
        },
    )


@router.get(
    "/v2/partials/customers/{company_id}/contacts/{contact_id}/field/display/{field}",
    response_class=HTMLResponse,
)
async def contact_field_display(
    request: Request,
    company_id: int,
    contact_id: int,
    field: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return the display span for a single contact field (cancel path)."""
    if field not in EDITABLE_CONTACT_FIELDS:
        raise HTTPException(404, f"Unknown editable contact field: {field!r}")
    contact = (
        db.query(SiteContact)
        .join(CustomerSite)
        .filter(SiteContact.id == contact_id, CustomerSite.company_id == company_id)
        .first()
    )
    if not contact:
        raise HTTPException(404, "Contact not found")
    meta = EDITABLE_CONTACT_FIELDS[field]
    return template_response(
        "htmx/partials/customers/_field_display.html",
        {
            **_base_ctx(request, user),
            "obj": contact,
            "field": field,
            "entity": "contact",
            "meta": meta,
            "edit_url": f"/v2/partials/customers/{company_id}/contacts/{contact_id}/field/edit/{field}",
        },
    )


@router.post(
    "/v2/partials/customers/{company_id}/contacts/{contact_id}/field",
    response_class=HTMLResponse,
)
async def contact_field_post(
    request: Request,
    company_id: int,
    contact_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Save a single inline-edited contact field; return the display span.

    IDOR-safe: the contact must belong to a site under {company_id}. Owner-or-admin only.
    """
    contact = (
        db.query(SiteContact)
        .join(CustomerSite)
        .filter(SiteContact.id == contact_id, CustomerSite.company_id == company_id)
        .first()
    )
    if not contact:
        raise HTTPException(404, "Contact not found")
    company = db.get(Company, company_id)
    if company is None or not can_manage_account(user, company, db):
        raise HTTPException(403, "Only the owner or an admin can edit this contact")
    form = await request.form()
    field = (form.get("field") or "").strip()
    if field not in EDITABLE_CONTACT_FIELDS:
        raise HTTPException(404, f"Unknown editable contact field: {field!r}")
    value = form.get("value") or ""
    old_value = getattr(contact, field, None)
    apply_contact_field(contact, field, value, contact.customer_site_id, db)
    record_field_change(
        db,
        entity_type=ENTITY_CONTACT,
        entity_id=contact.id,
        field_name=field,
        old_value=old_value,
        new_value=getattr(contact, field, None),
        user_id=user.id,
    )
    db.commit()
    logger.info("Contact {} field {} edited inline by {}", contact_id, field, user.email)
    meta = EDITABLE_CONTACT_FIELDS[field]
    return template_response(
        "htmx/partials/customers/_field_display.html",
        {
            **_base_ctx(request, user),
            "obj": contact,
            "field": field,
            "entity": "contact",
            "meta": meta,
            "edit_url": f"/v2/partials/customers/{company_id}/contacts/{contact_id}/field/edit/{field}",
        },
    )


# ── Contact Move ─────────────────────────────────────────────────────────────


@router.get("/v2/partials/customers/{company_id}/sites-options")
async def company_sites_options(
    request: Request,
    company_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return JSON list of active sites for a company for the move-contact site picker.

    Used by Alpine.js in _contact_move_form.html to populate the site select on
    company change. Returns [{"id": N, "name": "..."}].
    """
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        return JSONResponse([])

    if not can_manage_account(user, company, db):
        return JSONResponse([])

    sites = (
        db.query(CustomerSite)
        .filter(CustomerSite.company_id == company_id, CustomerSite.is_active.is_(True))
        .order_by(CustomerSite.site_name)
        .all()
    )
    return JSONResponse([{"id": s.id, "name": s.site_name or f"Site {s.id}"} for s in sites])


@router.get("/v2/partials/customers/{company_id}/contacts/{contact_id}/move-form", response_class=HTMLResponse)
async def contact_move_form(
    request: Request,
    company_id: int,
    contact_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return the move-contact modal form.

    Lists all companies the user can manage so they can pick a target.
    """
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(404, "Company not found")

    contact = (
        db.query(SiteContact)
        .join(CustomerSite)
        .filter(SiteContact.id == contact_id, CustomerSite.company_id == company_id)
        .first()
    )
    if not contact:
        raise HTTPException(404, "Contact not found")

    if not can_manage_account(user, company, db):
        raise HTTPException(403, "Only the owner or an admin can move this contact")

    # Build list of companies the user can manage (for the target picker)
    if is_manager_or_admin(user):
        manageable = db.query(Company).filter(Company.is_active.is_(True)).order_by(Company.name).all()
    else:
        # owned companies + collaborator companies
        owned = db.query(Company).filter(Company.is_active.is_(True), Company.account_owner_id == user.id).all()
        collab_ids = [
            row[0]
            for row in db.query(AccountCollaborator.company_id).filter(AccountCollaborator.user_id == user.id).all()
        ]
        if collab_ids:
            collab_cos = db.query(Company).filter(Company.id.in_(collab_ids)).all()
        else:
            collab_cos = []
        seen = {c.id for c in owned}
        manageable = list(owned)
        for co in collab_cos:
            if co.id not in seen:
                manageable.append(co)
                seen.add(co.id)
        manageable.sort(key=lambda c: c.name or "")

    return template_response(
        "htmx/partials/customers/_contact_move_form.html",
        {
            "request": request,
            "contact": contact,
            "company": company,
            "companies": manageable,
        },
    )


@router.post(
    "/v2/partials/customers/{company_id}/contacts/{contact_id}/move",
    response_class=HTMLResponse,
)
async def contact_move(
    request: Request,
    company_id: int,
    contact_id: int,
    target_site_id: int = Form(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Move contact_id to target_site_id.

    Validates: source company accessible, target site exists + is active,
    target company accessible by the same user. Re-renders contacts-tab-list
    for the SOURCE company (contact is gone from here now).
    """
    # Source authz
    source_company = db.query(Company).filter(Company.id == company_id).first()
    if not source_company:
        raise HTTPException(404, "Company not found")

    if not can_manage_account(user, source_company, db):
        raise HTTPException(403, "Only the owner or an admin can move this contact")

    contact = (
        db.query(SiteContact)
        .join(CustomerSite)
        .filter(SiteContact.id == contact_id, CustomerSite.company_id == company_id)
        .first()
    )
    if not contact:
        raise HTTPException(404, "Contact not found")

    # Target site validation
    target_site = db.query(CustomerSite).filter(CustomerSite.id == target_site_id).first()
    if not target_site:
        raise HTTPException(400, "Target site not found")
    if not target_site.is_active:
        raise HTTPException(400, "Target site is inactive")

    # Target authz
    target_company = db.query(Company).filter(Company.id == target_site.company_id).first()
    if not target_company:
        raise HTTPException(400, "Target company not found")

    if not can_manage_account(user, target_company, db):
        raise HTTPException(403, "You do not have access to the target company")

    # Email collision guard: (customer_site_id, email) unique constraint
    if contact.email:
        collision = (
            db.query(SiteContact)
            .filter(
                SiteContact.customer_site_id == target_site_id,
                SiteContact.email == contact.email,
            )
            .first()
        )
        if collision:
            raise HTTPException(400, "A contact with this email already exists at the target site")

    # Execute move
    old_site_id = contact.customer_site_id
    contact.customer_site_id = target_site_id
    db.commit()

    logger.info(
        "Contact move: contact {} ({}) moved from site {} → site {} by {}",
        contact_id,
        contact.full_name,
        old_site_id,
        target_site_id,
        user.email,
    )

    return _render_contacts_list(request, user, source_company, db)


@router.get(
    "/v2/partials/customers/{company_id}/contacts/{contact_id}/edit-form",
    response_class=HTMLResponse,
)
async def contact_edit_form_company_scoped(
    request: Request,
    company_id: int,
    contact_id: int,
    origin: str = "",
    filter_search: str = "",
    filter_company_id: int = Query(0, ge=0),
    filter_contact_role: str = "",
    filter_cadence_state: str = "",
    filter_limit: int = Query(50, ge=1, le=200),
    filter_offset: int = Query(0, ge=0),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return the shared _contact_form.html in edit mode for the Contacts tab.

    This company-scoped route (no site_id in path) is called from the Contacts-tab kebab
    Edit button. It returns _contact_form.html in 'edit' mode so the form posts to
    /contacts/{contact_id}/edit and targets #contacts-tab-list — the canonical swap
    target for the Contacts tab. The former site-scoped edit-form route and
    contact_edit_modal.html have been retired.

    origin=contacts: called from the global /v2/contacts list's per-row Edit button. The
    form then targets #main-content and carries the filter_* values as hidden inputs so
    the save re-renders the global list with the caller's filters intact.
    """
    # Validate the contact belongs to a site that belongs to this company
    contact = (
        db.query(SiteContact)
        .join(CustomerSite, SiteContact.customer_site_id == CustomerSite.id)
        .filter(SiteContact.id == contact_id, CustomerSite.company_id == company_id)
        .first()
    )
    if not contact:
        raise HTTPException(404, "Contact not found")
    site = db.get(CustomerSite, contact.customer_site_id)
    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")
    # 404 (not 403) to match company_detail_partial: out-of-scope accounts must be
    # indistinguishable from missing ones, and this form leaks full contact PII.
    if not can_manage_account(user, company, db):
        raise HTTPException(404, "Company not found")
    # Same-company contacts for reports_to select, excluding self
    site_contacts_for_select = (
        db.query(SiteContact)
        .join(CustomerSite, SiteContact.customer_site_id == CustomerSite.id)
        .filter(
            CustomerSite.company_id == company_id,
            SiteContact.is_active.is_(True),
            SiteContact.id != contact_id,
        )
        .order_by(SiteContact.full_name)
        .all()
    )
    return template_response(
        "htmx/partials/customers/tabs/_contact_form.html",
        {
            "request": request,
            "mode": "edit",
            "company": company,
            "contact": contact,
            "site": site,
            "sites": [],
            "roles": CANONICAL_ROLES,
            "site_contacts_for_select": site_contacts_for_select,
            "origin": origin if origin == "contacts" else "",
            "list_filters": {
                "search": filter_search,
                "company_id": filter_company_id,
                "contact_role": filter_contact_role,
                "cadence_state": filter_cadence_state,
                "limit": filter_limit,
                "offset": filter_offset,
            },
        },
    )


@router.get("/v2/partials/contacts/{contact_id}/files-modal", response_class=HTMLResponse)
async def contact_files_modal(
    request: Request,
    contact_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return the global-modal body hosting the shared attachments panel for a contact.

    Loaded by the contact-card kebab "Files" action via $dispatch('open-modal').
    Access mirrors the contact attachment endpoints: contact → site → company.
    """
    contact = db.get(SiteContact, contact_id)
    if not contact:
        raise HTTPException(404, "Contact not found")
    site = db.get(CustomerSite, contact.customer_site_id)
    if not site or not db.get(Company, site.company_id):
        raise HTTPException(404, "Contact not found")
    return template_response(
        "htmx/partials/customers/_contact_files_modal.html",
        {"request": request, "contact": contact},
    )


def _contact_under_company(db: Session, company_id: int, contact_id: int) -> SiteContact:
    """Load a SiteContact and verify it belongs to *company_id* (via its site).

    Raises HTTPException(404) if the contact does not exist or is not under that company
    — the contact-notes-modal endpoints share this lookup.
    """
    contact = (
        db.query(SiteContact)
        .join(CustomerSite, SiteContact.customer_site_id == CustomerSite.id)
        .filter(SiteContact.id == contact_id, CustomerSite.company_id == company_id)
        .first()
    )
    if not contact:
        raise HTTPException(404, "Contact not found")
    return contact


def _render_contact_notes_modal(
    request: Request,
    company: Company,
    contact: SiteContact,
    db: Session,
    can_manage: bool,
    error: str | None = None,
) -> HTMLResponse:
    """Render the contact-notes modal body (feed + add form).

    Shared by GET + POST.
    """
    notes = get_site_contact_notes(contact.id, db)
    return template_response(
        "htmx/partials/customers/_contact_notes_modal.html",
        {
            "request": request,
            "company": company,
            "contact": contact,
            "notes": notes,
            "can_manage": can_manage,
            "error": error,
        },
    )


@router.get(
    "/v2/partials/customers/{company_id}/contacts/{contact_id}/notes-modal",
    response_class=HTMLResponse,
)
async def contact_notes_modal(
    request: Request,
    company_id: int,
    contact_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return the global-modal body with a contact's note feed + add form.

    Loaded by the contact-card drawer "See all notes" / "+ Add note" action via
    $dispatch('open-modal'). 404 if the contact is not under this company.
    """
    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")
    contact = _contact_under_company(db, company_id, contact_id)
    return _render_contact_notes_modal(request, company, contact, db, can_manage=can_manage_account(user, company, db))


@router.get(
    "/v2/partials/customers/{company_id}/contacts/{contact_id}/history-modal",
    response_class=HTMLResponse,
)
async def contact_history_modal(
    request: Request,
    company_id: int,
    contact_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return the global-modal body with a contact's field-change history.

    Loaded by the contact-card kebab "History" action via $dispatch('open-modal'). 404
    if the contact is not under this company (IDOR guard via the shared lookup).
    """
    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")
    contact = _contact_under_company(db, company_id, contact_id)
    history = field_history_for(db, ENTITY_CONTACT, contact.id)
    ctx = _base_ctx(request, user)
    ctx.update(
        {
            "company": company,
            "contact": contact,
            "history": history,
            "field_labels": FIELD_LABELS,
            "now_utc": datetime.now(UTC),
        }
    )
    return template_response("htmx/partials/customers/_contact_history_modal.html", ctx)


@router.post(
    "/v2/partials/customers/{company_id}/contacts/{contact_id}/notes",
    response_class=HTMLResponse,
)
async def add_contact_note(
    request: Request,
    company_id: int,
    contact_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Log a manual note against a contact, then re-render the notes-modal body.

    can_manage_account gate (403 otherwise). Blank note → inline error (no write).
    """
    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")
    if not can_manage_account(user, company, db):
        raise HTTPException(403, "Only the owner or an admin can add notes for this contact")
    contact = _contact_under_company(db, company_id, contact_id)

    form = await request.form()
    notes_text = (form.get("notes") or "").strip()
    if not notes_text:
        return _render_contact_notes_modal(
            request, company, contact, db, can_manage=True, error="Note cannot be empty."
        )

    log_site_contact_note(
        user_id=user.id,
        site_contact_id=contact.id,
        customer_site_id=contact.customer_site_id,
        company_id=company_id,
        notes=notes_text,
        db=db,
    )
    db.commit()
    logger.info("Note added to contact {} by {} (company {})", contact_id, user.email, company_id)
    return _render_contact_notes_modal(request, company, contact, db, can_manage=True)


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
    """Update editable contact fields and return refreshed Contacts tab grouped list.

    Writes contact_role (validated via _validate_role; blank→NULL, unknown→400),
    is_priority, and linkedin_url. Renders #contacts-tab-list — the Sites tab no longer
    carries a contact editor. When the form carries origin=contacts (the global
    /v2/contacts list's Edit modal), re-renders the global contacts list instead, scoped
    to the filter_* fields the modal carried through.
    """
    contact = (
        db.query(SiteContact).filter(SiteContact.id == contact_id, SiteContact.customer_site_id == site_id).first()
    )
    if not contact:
        raise HTTPException(404, "Contact not found")
    site = db.query(CustomerSite).filter(CustomerSite.id == site_id, CustomerSite.company_id == company_id).first()
    if not site:
        raise HTTPException(404, "Site not found")
    company = db.get(Company, company_id)
    if company is None or not can_manage_account(user, company, db):
        raise HTTPException(403, "Not authorized to manage this account")

    form = await request.form()

    # Name fields — apply atomically so the "at least one required" check
    # sees both values together rather than one-at-a-time.
    first_name_raw = form.get("first_name")
    last_name_raw = form.get("last_name")
    if first_name_raw is not None or last_name_raw is not None:
        new_first = (first_name_raw or "").strip() or None
        new_last = (last_name_raw or "").strip() or None
        if not new_first and not new_last:
            raise HTTPException(400, "At least one of first_name or last_name is required")
        contact.first_name = new_first
        contact.last_name = new_last
        _recompose_full_name(contact)

    # Remaining registry fields (skip first_name/last_name — handled above)
    for f in EDITABLE_CONTACT_FIELDS:
        if f in ("first_name", "last_name"):
            continue
        raw = form.get(f)
        if raw is not None:  # field was submitted
            apply_contact_field(contact, f, raw, site_id, db)

    # Non-registry fields
    contact.notes = (form.get("notes", "") or "").strip() or None
    contact.is_priority = bool((form.get("is_priority", "") or "").strip())
    # reports_to_id — self-FK, not in EDITABLE_CONTACT_FIELDS
    reports_to_id_raw = form.get("reports_to_id")
    if reports_to_id_raw is not None:
        v = reports_to_id_raw.strip()
        new_reports_to_id = int(v) if v.isdigit() else None
        if new_reports_to_id is not None:
            if new_reports_to_id == contact_id:
                raise HTTPException(400, "reports_to must be a contact in the same company")
            mgr = (
                db.query(SiteContact)
                .join(CustomerSite, SiteContact.customer_site_id == CustomerSite.id)
                .filter(SiteContact.id == new_reports_to_id, CustomerSite.company_id == company_id)
                .first()
            )
            if not mgr:
                raise HTTPException(400, "reports_to must be a contact in the same company")
        contact.reports_to_id = new_reports_to_id
    contact.updated_at = datetime.now(UTC)
    db.commit()
    logger.info("Contact {} edited by {}", contact_id, user.email)

    if (form.get("origin") or "") == "contacts":
        resp = _contacts_list_response(request, user, db, form, prefix="filter_")
        resp.headers["HX-Trigger"] = json.dumps({"showToast": {"message": f"Updated {contact.full_name or 'contact'}"}})
        return resp
    return _render_contacts_list(request, user, company, db)
