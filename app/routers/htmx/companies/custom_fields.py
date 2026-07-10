"""routers/htmx/companies/custom_fields.py — free-form label:value custom fields for
companies + contacts (P4.3 split, WS3).

Add/remove entries in ``Company.custom_fields`` / ``SiteContact.custom_fields``
(JSON columns). Both mutate the dict and ``flag_modified`` so SQLAlchemy detects the
in-place JSON change, then re-render the shared ``_custom_fields.html`` partial.

Called by: app.routers.htmx.companies (package __init__ re-export, route registration)
Depends on: app.models, app.dependencies, sqlalchemy.orm.attributes.flag_modified
"""

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from loguru import logger
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from ....database import get_db
from ....dependencies import can_manage_account, require_user
from ....models import Company, CustomerSite, SiteContact, User
from ....template_env import template_response
from . import router


def _render_custom_fields(request: Request, entity: str, obj, company_id: int):
    """Render the _custom_fields.html partial for a company or contact."""
    return template_response(
        "htmx/partials/customers/_custom_fields.html",
        {"request": request, "entity": entity, "obj": obj, "company_id": company_id},
    )


@router.post("/v2/partials/customers/{company_id}/custom-fields", response_class=HTMLResponse)
async def company_add_custom_field(
    request: Request,
    company_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Add or overwrite a label:value pair in company.custom_fields."""
    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")
    if not can_manage_account(user, company, db):
        raise HTTPException(403, "Only the owner or an admin can edit this account")
    form = await request.form()
    label = (form.get("label") or "").strip()
    value = (form.get("value") or "").strip()
    if not label:
        raise HTTPException(400, "label is required")
    existing = company.custom_fields or {}
    updated = {**existing, label: value}
    try:
        company.custom_fields = updated
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    flag_modified(company, "custom_fields")
    db.commit()
    db.refresh(company)
    logger.info("Company {} custom field '{}' set by {}", company_id, label, user.email)
    return _render_custom_fields(request, "company", company, company_id)


@router.delete(
    "/v2/partials/customers/{company_id}/custom-fields/{label:path}",
    response_class=HTMLResponse,
)
async def company_delete_custom_field(
    request: Request,
    company_id: int,
    label: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Remove a label from company.custom_fields."""
    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")
    if not can_manage_account(user, company, db):
        raise HTTPException(403, "Only the owner or an admin can edit this account")
    existing = dict(company.custom_fields or {})
    existing.pop(label, None)
    company.custom_fields = existing
    flag_modified(company, "custom_fields")
    db.commit()
    db.refresh(company)
    logger.info("Company {} custom field '{}' removed by {}", company_id, label, user.email)
    return _render_custom_fields(request, "company", company, company_id)


@router.post(
    "/v2/partials/customers/{company_id}/contacts/{contact_id}/custom-fields",
    response_class=HTMLResponse,
)
async def contact_add_custom_field(
    request: Request,
    company_id: int,
    contact_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Add or overwrite a label:value pair in contact.custom_fields.

    IDOR-safe: verifies the contact belongs to a site under the path company.
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
    label = (form.get("label") or "").strip()
    value = (form.get("value") or "").strip()
    if not label:
        raise HTTPException(400, "label is required")
    existing = contact.custom_fields or {}
    updated = {**existing, label: value}
    try:
        contact.custom_fields = updated
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    flag_modified(contact, "custom_fields")
    db.commit()
    db.refresh(contact)
    logger.info("Contact {} custom field '{}' set by {}", contact_id, label, user.email)
    return _render_custom_fields(request, "contact", contact, company_id)


@router.delete(
    "/v2/partials/customers/{company_id}/contacts/{contact_id}/custom-fields/{label:path}",
    response_class=HTMLResponse,
)
async def contact_delete_custom_field(
    request: Request,
    company_id: int,
    contact_id: int,
    label: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Remove a label from contact.custom_fields.

    IDOR-safe: verifies the contact belongs to a site under the path company.
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
    existing = dict(contact.custom_fields or {})
    existing.pop(label, None)
    contact.custom_fields = existing
    flag_modified(contact, "custom_fields")
    db.commit()
    db.refresh(contact)
    logger.info("Contact {} custom field '{}' removed by {}", contact_id, label, user.email)
    return _render_custom_fields(request, "contact", contact, company_id)
