"""routers/htmx/companies/tags.py — segment tags for companies + site contacts (P4.3
split).

Assign/unassign segment tags on a company (``_segment_tags.html`` chips) and on a
site contact (``_contact_tags.html`` chips). Tag creation/lookup/assignment logic
lives in ``app.services.tagging`` — these routes are HTTP-only glue.

Called by: app.routers.htmx.companies (package __init__ re-export, route registration)
Depends on: app.services.tagging, app.models.tags, app.dependencies
"""

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ....database import get_db
from ....dependencies import can_manage_account, require_user
from ....models import Company, CustomerSite, SiteContact, User
from ....models.tags import EntityTag, Tag
from ....services.tagging import (
    assign_segment_tag,
    get_or_create_segment_tag,
    list_all_segment_tags,
    list_company_segment_tags,
    unassign_segment_tag,
)
from ....template_env import template_response
from . import router


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
    tags = list_company_segment_tags(company_id=company_id, db=db)
    all_segment_tags = list_all_segment_tags(db=db)
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
    if not can_manage_account(user, company, db):
        raise HTTPException(403, "Not authorized")

    form = await request.form()
    tag_id_raw = form.get("tag_id", "").strip()
    tag_name_raw = form.get("tag_name", "").strip()

    if tag_name_raw:
        tag = get_or_create_segment_tag(tag_name_raw, db)
    elif tag_id_raw:
        try:
            tag_id = int(tag_id_raw)
        except ValueError as e:
            raise HTTPException(400, "tag_id must be an integer") from e
        tag = db.query(Tag).filter_by(id=tag_id).first()
        if not tag:
            raise HTTPException(404, "Tag not found")
    else:
        raise HTTPException(400, "Provide tag_id or tag_name")

    assign_segment_tag(company_id=company_id, tag_id=tag.id, db=db)
    db.commit()

    tags = list_company_segment_tags(company_id=company_id, db=db)
    all_segment_tags = list_all_segment_tags(db=db)
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
    if not can_manage_account(user, company, db):
        raise HTTPException(403, "Not authorized")

    unassign_segment_tag(company_id=company_id, tag_id=tag_id, db=db)
    db.commit()

    tags = list_company_segment_tags(company_id=company_id, db=db)
    all_segment_tags = list_all_segment_tags(db=db)
    return template_response(
        "htmx/partials/customers/_segment_tags.html",
        {
            "request": request,
            "company": company,
            "segment_tags": tags,
            "all_segment_tags": all_segment_tags,
        },
    )


# ── Contact tag routes ─────────────────────────────────────────────────────


@router.post(
    "/v2/partials/customers/{company_id}/contacts/{contact_id}/tags",
    response_class=HTMLResponse,
)
async def contact_assign_tag(
    request: Request,
    company_id: int,
    contact_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Assign a segment tag to a site contact.

    Accepts tag_id= (existing) or tag_name= (creates new tag_type='segment'). Returns
    the contact tags chips partial.
    """
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(404, "Company not found")
    if not can_manage_account(user, company, db):
        raise HTTPException(403, "Not authorized to manage this account")
    contact = (
        db.query(SiteContact)
        .join(CustomerSite, SiteContact.customer_site_id == CustomerSite.id)
        .filter(SiteContact.id == contact_id, CustomerSite.company_id == company_id)
        .first()
    )
    if not contact:
        raise HTTPException(404, "Contact not found")

    form = await request.form()
    tag_id_raw = (form.get("tag_id") or "").strip()
    tag_name_raw = (form.get("tag_name") or "").strip()

    if tag_name_raw:
        tag = get_or_create_segment_tag(tag_name_raw, db)
    elif tag_id_raw:
        try:
            tag_id = int(tag_id_raw)
        except ValueError as e:
            raise HTTPException(400, "tag_id must be an integer") from e
        tag = db.query(Tag).filter_by(id=tag_id).first()
        if not tag:
            raise HTTPException(404, "Tag not found")
    else:
        raise HTTPException(400, "Provide tag_id or tag_name")

    existing = db.query(EntityTag).filter_by(entity_type="site_contact", entity_id=contact_id, tag_id=tag.id).first()
    if existing:
        existing.is_visible = True
    else:
        et = EntityTag(
            entity_type="site_contact",
            entity_id=contact_id,
            tag_id=tag.id,
            is_visible=True,
            interaction_count=0,
            total_entity_interactions=0,
        )
        db.add(et)
    db.commit()

    contact_tags = (
        db.query(Tag)
        .join(EntityTag, EntityTag.tag_id == Tag.id)
        .filter(
            EntityTag.entity_type == "site_contact",
            EntityTag.entity_id == contact_id,
            EntityTag.is_visible.is_(True),
        )
        .order_by(Tag.name)
        .all()
    )
    all_segment_tags = list_all_segment_tags(db=db)
    return template_response(
        "htmx/partials/customers/_contact_tags.html",
        {
            "request": request,
            "company": company,
            "contact": contact,
            "contact_tags": contact_tags,
            "all_segment_tags": all_segment_tags,
        },
    )


@router.delete(
    "/v2/partials/customers/{company_id}/contacts/{contact_id}/tags/{tag_id}",
    response_class=HTMLResponse,
)
async def contact_unassign_tag(
    request: Request,
    company_id: int,
    contact_id: int,
    tag_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Remove a segment tag from a site contact."""
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(404, "Company not found")
    if not can_manage_account(user, company, db):
        raise HTTPException(403, "Not authorized to manage this account")
    contact = (
        db.query(SiteContact)
        .join(CustomerSite, SiteContact.customer_site_id == CustomerSite.id)
        .filter(SiteContact.id == contact_id, CustomerSite.company_id == company_id)
        .first()
    )
    if not contact:
        raise HTTPException(404, "Contact not found")

    et = db.query(EntityTag).filter_by(entity_type="site_contact", entity_id=contact_id, tag_id=tag_id).first()
    if et:
        db.delete(et)
        db.commit()

    contact_tags = (
        db.query(Tag)
        .join(EntityTag, EntityTag.tag_id == Tag.id)
        .filter(
            EntityTag.entity_type == "site_contact",
            EntityTag.entity_id == contact_id,
            EntityTag.is_visible.is_(True),
        )
        .order_by(Tag.name)
        .all()
    )
    all_segment_tags = list_all_segment_tags(db=db)
    return template_response(
        "htmx/partials/customers/_contact_tags.html",
        {
            "request": request,
            "company": company,
            "contact": contact,
            "contact_tags": contact_tags,
            "all_segment_tags": all_segment_tags,
        },
    )
