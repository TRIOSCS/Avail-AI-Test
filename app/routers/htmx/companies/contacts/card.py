"""routers/htmx/companies/contacts/card.py — per-contact toggles, inline field editing,
and the files/notes/history modals (W4.8 split of contacts.py).

The contact-card write surface: role/DNC/priority/archive toggles, the inline
field edit/display/post trio, the attachments (files) modal, and the contact
notes/history modals + note add. Pure structural move: URLs and behavior
unchanged; every route attaches to the shared router imported from .common
(registration assembled in contacts/__init__).

Called by: app.routers.htmx.companies.contacts (package __init__ re-export,
    route registration)
Depends on: app.services.crm_service, app.services.activity_service,
    app.services.crm_field_history, .._registries, ..._shared, .common
"""

from datetime import UTC, datetime

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from loguru import logger
from sqlalchemy.orm import Session

from .....database import get_db
from .....dependencies import can_manage_account, require_user
from .....models import Company, CustomerSite, SiteContact, User
from .....services.activity_service import get_site_contact_notes, log_site_contact_note
from .....services.crm_field_history import ENTITY_CONTACT, field_history_for, record_field_change
from .....template_env import template_response
from ..._shared import _base_ctx
from .._registries import (
    EDITABLE_CONTACT_FIELDS,
    FIELD_LABELS,
    _validate_role,
    apply_contact_field,
)
from .common import _render_contacts_list, router


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
    company = db.get(Company, company_id)
    # 404 (not 403) to match contact_edit_form_company_scoped: this widget leaks the
    # contact field value, so out-of-scope accounts must be indistinguishable from missing.
    if company is None or not can_manage_account(user, company, db):
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
    company = db.get(Company, company_id)
    # 404 (not 403) to match contact_edit_form_company_scoped: this span leaks the
    # contact field value, so out-of-scope accounts must be indistinguishable from missing.
    if company is None or not can_manage_account(user, company, db):
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
    company = db.get(Company, site.company_id) if site else None
    if not site or company is None:
        raise HTTPException(404, "Contact not found")
    # 404 (not 403) to match contact_edit_form_company_scoped: this modal shell leaks
    # contact PII, so out-of-scope accounts must be indistinguishable from missing.
    if not can_manage_account(user, company, db):
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
    # 404 (not 403) to match contact_edit_form_company_scoped: the notes feed is contact
    # PII, so out-of-scope accounts must be indistinguishable from missing. can_manage_account
    # was previously only a display flag, leaving the feed readable cross-tenant.
    if not can_manage_account(user, company, db):
        raise HTTPException(404, "Company not found")
    contact = _contact_under_company(db, company_id, contact_id)
    return _render_contact_notes_modal(request, company, contact, db, can_manage=True)


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
    # 404 (not 403) to match contact_edit_form_company_scoped: field-change history holds
    # old/new contact PII, so out-of-scope accounts must be indistinguishable from missing.
    if not can_manage_account(user, company, db):
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
