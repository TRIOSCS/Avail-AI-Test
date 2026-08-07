"""routers/htmx/companies/contacts/crud.py — Contacts-tab add/create, edit forms, and
the contact move flow (W4.8 split of contacts.py).

The Contacts-tab add-form/create pair, the company-scoped contact edit form,
``edit_site_contact`` (the site-scoped contact edit write path), the contact
move form/action, and the sites-options JSON helper the move form consumes.
Pure structural move: URLs and behavior unchanged; every route attaches to the
shared router imported from .common (registration assembled in
contacts/__init__).

Called by: app.routers.htmx.companies.contacts (package __init__ re-export,
    route registration)
Depends on: app.services.crm_service, app.services.crm_field_history,
    .._registries, ..._shared, .common
"""

import json
from datetime import UTC, datetime

from fastapi import Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from loguru import logger
from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session

from .....database import get_db
from .....dependencies import can_manage_account, is_manager_or_admin, require_user
from .....models import AccountCollaborator, Company, CustomerSite, SiteContact, User
from .....template_env import template_response
from .....utils.column_limits import ensure_fits_column
from .._registries import (
    CANONICAL_ROLES,
    EDITABLE_CONTACT_FIELDS,
    _backfill_legacy_name_parts,
    _recompose_full_name,
    apply_contact_field,
    resolve_contact_role,
)
from .common import _contacts_list_response, _render_contacts_list, router

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
    # 404 (not 403) to match contact_edit_form_company_scoped: this form leaks the
    # company/site roster and the full user list, so out-of-scope accounts must be
    # indistinguishable from missing ones.
    if not can_manage_account(user, company, db):
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

    # Remaining bounded free-text inputs (parsed here so EVERY length guard runs
    # before any DB write — the '__new__' site branch flushes an INSERT below).
    title_val = (form.get("title") or "").strip() or None
    phone_val = (form.get("phone") or "").strip() or None
    secondary_email_val = (form.get("secondary_email") or "").strip() or None
    secondary_phone_val = (form.get("secondary_phone") or "").strip() or None
    linkedin_url_val = (form.get("linkedin_url") or "").strip() or None

    site_id_raw = (form.get("site_id") or "").strip()
    new_site_name = (form.get("new_site_name") or "").strip()

    # Model-derived length guards (Wave 3 item 6) — 400 instead of a Postgres 500.
    # SQLite (tests) silently accepts over-length values and masks the failure.
    for _field, _value, _label in (
        ("full_name", full_name, "Name"),
        ("first_name", first_name_val, "First Name"),
        ("last_name", last_name_val, "Last Name"),
        ("email", email_val, "Email"),
        ("title", title_val, "Title"),
        ("phone", phone_val, "Phone"),
        ("secondary_email", secondary_email_val, "Secondary Email"),
        ("secondary_phone", secondary_phone_val, "Secondary Phone"),
        ("linkedin_url", linkedin_url_val, "LinkedIn"),
    ):
        ensure_fits_column(SiteContact, _field, _value, _label)
    # The '__new__' branch writes CustomerSite.site_name directly (bypasses create_site).
    ensure_fits_column(CustomerSite, "site_name", new_site_name or None, "Site name")

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

    # ── Resolve role (canonical value, or the OTHER free-text write-in) ──
    role = resolve_contact_role(form.get("contact_role") or "", form.get("contact_role_custom") or "")
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
        title=title_val,
        phone=phone_val,
        secondary_email=secondary_email_val,
        secondary_phone=secondary_phone_val,
        wechat_id=wechat_id_val or None,
        notes=(form.get("notes") or "").strip() or None,
        linkedin_url=linkedin_url_val,
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
        # Legacy full_name-only contact: backfill both parts first (Wave 3 item 2) so a
        # partial submit can't wipe the un-submitted part. A name field ABSENT from the
        # form preserves the (possibly just-backfilled) current part — matching the
        # present-vs-absent rule of the registry loop below; a SUBMITTED blank clears.
        _backfill_legacy_name_parts(contact)
        new_first = ((first_name_raw or "").strip() or None) if first_name_raw is not None else contact.first_name
        new_last = ((last_name_raw or "").strip() or None) if last_name_raw is not None else contact.last_name
        if not new_first and not new_last:
            raise HTTPException(400, "At least one of first_name or last_name is required")
        # Length guards (Wave 3 item 6) — this atomic block assigns directly,
        # bypassing the guarded apply_contact_field loop below. full_name is safe
        # by construction: 120 + space + 120 < 255.
        ensure_fits_column(SiteContact, "first_name", new_first, "First Name")
        ensure_fits_column(SiteContact, "last_name", new_last, "Last Name")
        contact.first_name = new_first
        contact.last_name = new_last
        _recompose_full_name(contact)

    # Remaining registry fields (skip first_name/last_name — handled above;
    # contact_role — handled below with its contact_role_custom write-in companion)
    for f in EDITABLE_CONTACT_FIELDS:
        if f in ("first_name", "last_name", "contact_role"):
            continue
        raw = form.get(f)
        if raw is not None:  # field was submitted
            apply_contact_field(contact, f, raw, site_id, db)

    # contact_role — resolved together with its OTHER free-text write-in companion,
    # so it can't go through the generic single-value apply_contact_field loop above.
    role_raw = form.get("contact_role")
    if role_raw is not None:  # field was submitted
        contact.contact_role = resolve_contact_role(role_raw, form.get("contact_role_custom") or "")

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
