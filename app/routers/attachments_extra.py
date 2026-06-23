"""attachments_extra.py — Company, Contact, and MaterialCard attachment endpoints +
unified serve route.

Provides:
  GET/POST /api/companies/{company_id}/attachments
  DELETE   /api/company-attachments/{att_id}
  GET/POST /api/contacts/{contact_id}/attachments
  DELETE   /api/contact-attachments/{att_id}
  GET/POST /api/material-cards/{card_id}/attachments
  DELETE   /api/material-card-attachments/{att_id}
  GET      /api/attachments/{kind}/{att_id}/content   (unified serve)

Access model:
  company   — any authenticated user may access any company (mirrors company_detail_partial).
              user_can_access_company() is the single, extracted helper used here and can be
              reused by company_detail_partial if that route ever adds per-user gating.
  contact   — resolved SiteContact → CustomerSite → Company; same company check.
  material  — shared catalog; require_user is sufficient (no per-user ownership on parts).

Called by: app/main.py (router registration)
Depends on: app/services/attachment_service, all attachment models, app/dependencies
"""

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.orm import Session, selectinload

from ..database import get_db
from ..dependencies import get_req_for_user, require_user
from ..models import (
    Company,
    CompanyAttachment,
    CustomerSite,
    MaterialCard,
    MaterialCardAttachment,
    Offer,
    OfferAttachment,
    RequirementAttachment,
    RequisitionAttachment,
    SiteContact,
    SiteContactAttachment,
    User,
)
from ..services import attachment_service

router = APIRouter()

_KIND_MODEL = {
    "requisition": RequisitionAttachment,
    "requirement": RequirementAttachment,
    "offer": OfferAttachment,
    "company": CompanyAttachment,
    "contact": SiteContactAttachment,
    "material": MaterialCardAttachment,
}


# ---------------------------------------------------------------------------
# Company-access helper
# ---------------------------------------------------------------------------


def user_can_access_company(db: Session, user: User, company_id: int) -> Company | None:
    """Return the Company if the user may access it, otherwise None.

    Access model mirrors company_detail_partial: any authenticated user may view any
    existing company (no per-user ownership filtering at the detail level). Returns None
    when the company does not exist (callers should raise 404 to avoid existence leaks).
    """
    return db.get(Company, company_id)


# ---------------------------------------------------------------------------
# Serve-route ownership check (closes Task 4 TODO)
# ---------------------------------------------------------------------------


def _check_serve_access(kind: str, att, user: User, db: Session) -> None:
    """Enforce per-kind ownership before serving an attachment.

    Raises HTTPException(404) on access denial to avoid existence leaks. Matches the
    same gate logic that each kind's list endpoint uses.
    """
    if kind == "requisition":
        if not get_req_for_user(db, user, att.requisition_id):
            raise HTTPException(404, "Attachment not found")
    elif kind == "requirement":
        # att.requirement is a lazy-loaded relationship; resolve to its requisition_id.
        req_id = att.requirement.requisition_id if att.requirement else None
        if req_id is None or not get_req_for_user(db, user, req_id):
            raise HTTPException(404, "Attachment not found")
    elif kind == "offer":
        # Offer endpoints gate only on offer existence — match that level here.
        if not db.get(Offer, att.offer_id):
            raise HTTPException(404, "Attachment not found")
    elif kind == "company":
        if not user_can_access_company(db, user, att.company_id):
            raise HTTPException(404, "Attachment not found")
    elif kind == "contact":
        contact = db.get(SiteContact, att.site_contact_id)
        if not contact:
            raise HTTPException(404, "Attachment not found")
        site = db.get(CustomerSite, contact.customer_site_id)
        if not site or not user_can_access_company(db, user, site.company_id):
            raise HTTPException(404, "Attachment not found")
    elif kind == "material":
        # Material cards are a shared catalog — require_user is sufficient.
        # No per-user ownership; any buyer may serve any material attachment.
        if not db.get(MaterialCard, att.material_card_id):
            raise HTTPException(404, "Attachment not found")
    else:
        raise RuntimeError(f"BUG: no serve access check for kind={kind!r}")


# ---------------------------------------------------------------------------
# Unified serve route
# ---------------------------------------------------------------------------


@router.get("/api/attachments/{kind}/{att_id}/content")
async def serve_attachment(
    kind: str,
    att_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Stream or redirect an attachment by kind and ID."""
    model = _KIND_MODEL.get(kind)
    if model is None:
        raise HTTPException(404, f"Unknown attachment kind: {kind!r}")
    att = db.get(model, att_id)
    if att is None:
        raise HTTPException(404, "Attachment not found")
    _check_serve_access(kind, att, user, db)
    return await attachment_service.open_attachment(att, user)


# ---------------------------------------------------------------------------
# Company attachment endpoints
# ---------------------------------------------------------------------------


@router.get("/api/companies/{company_id}/attachments")
async def list_company_attachments(
    company_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """List attachments on a company, newest first."""
    company = user_can_access_company(db, user, company_id)
    if not company:
        raise HTTPException(404, "Company not found")
    atts = (
        db.query(CompanyAttachment)
        .options(selectinload(CompanyAttachment.uploaded_by))
        .filter(CompanyAttachment.company_id == company_id)
        .order_by(CompanyAttachment.created_at.desc())
        .all()
    )
    return [attachment_service.serialize(a) for a in atts]


@router.post("/api/companies/{company_id}/attachments")
async def upload_company_attachment(
    company_id: int,
    file: UploadFile = File(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Upload a file and attach it to a company."""
    company = user_can_access_company(db, user, company_id)
    if not company:
        raise HTTPException(404, "Company not found")
    att = await attachment_service.store_and_attach(
        db,
        model=CompanyAttachment,
        fk_field="company_id",
        entity_label="Companies",
        entity_id=company_id,
        file=file,
        user=user,
    )
    return attachment_service.serialize(att)


@router.delete("/api/company-attachments/{att_id}")
async def delete_company_attachment(
    att_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Delete a company attachment (best-effort cloud delete + DB row removal)."""
    att = db.get(CompanyAttachment, att_id)
    if not att:
        raise HTTPException(404, "Attachment not found")
    if not user_can_access_company(db, user, att.company_id):
        raise HTTPException(404, "Attachment not found")
    return await attachment_service.remove_attachment(db, att, user)


# ---------------------------------------------------------------------------
# Contact attachment endpoints
# ---------------------------------------------------------------------------


@router.get("/api/contacts/{contact_id}/attachments")
async def list_contact_attachments(
    contact_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """List attachments on a site contact, newest first."""
    contact = db.get(SiteContact, contact_id)
    if not contact:
        raise HTTPException(404, "Contact not found")
    site = db.get(CustomerSite, contact.customer_site_id)
    if not site or not user_can_access_company(db, user, site.company_id):
        raise HTTPException(404, "Contact not found")
    atts = (
        db.query(SiteContactAttachment)
        .options(selectinload(SiteContactAttachment.uploaded_by))
        .filter(SiteContactAttachment.site_contact_id == contact_id)
        .order_by(SiteContactAttachment.created_at.desc())
        .all()
    )
    return [attachment_service.serialize(a) for a in atts]


@router.post("/api/contacts/{contact_id}/attachments")
async def upload_contact_attachment(
    contact_id: int,
    file: UploadFile = File(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Upload a file and attach it to a site contact."""
    contact = db.get(SiteContact, contact_id)
    if not contact:
        raise HTTPException(404, "Contact not found")
    site = db.get(CustomerSite, contact.customer_site_id)
    if not site or not user_can_access_company(db, user, site.company_id):
        raise HTTPException(404, "Contact not found")
    att = await attachment_service.store_and_attach(
        db,
        model=SiteContactAttachment,
        fk_field="site_contact_id",
        entity_label="Contacts",
        entity_id=contact_id,
        file=file,
        user=user,
    )
    return attachment_service.serialize(att)


@router.delete("/api/contact-attachments/{att_id}")
async def delete_contact_attachment(
    att_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Delete a contact attachment (best-effort cloud delete + DB row removal)."""
    att = db.get(SiteContactAttachment, att_id)
    if not att:
        raise HTTPException(404, "Attachment not found")
    contact = db.get(SiteContact, att.site_contact_id)
    if not contact:
        raise HTTPException(404, "Attachment not found")
    site = db.get(CustomerSite, contact.customer_site_id)
    if not site or not user_can_access_company(db, user, site.company_id):
        raise HTTPException(404, "Attachment not found")
    return await attachment_service.remove_attachment(db, att, user)


# ---------------------------------------------------------------------------
# Material-card attachment endpoints
# ---------------------------------------------------------------------------


@router.get("/api/material-cards/{card_id}/attachments")
async def list_material_card_attachments(
    card_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """List attachments on a material card, newest first."""
    # Material cards are a shared catalog — require_user is sufficient.
    card = db.get(MaterialCard, card_id)
    if not card:
        raise HTTPException(404, "Material card not found")
    atts = (
        db.query(MaterialCardAttachment)
        .options(selectinload(MaterialCardAttachment.uploaded_by))
        .filter(MaterialCardAttachment.material_card_id == card_id)
        .order_by(MaterialCardAttachment.created_at.desc())
        .all()
    )
    return [attachment_service.serialize(a) for a in atts]


@router.post("/api/material-cards/{card_id}/attachments")
async def upload_material_card_attachment(
    card_id: int,
    file: UploadFile = File(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Upload a file and attach it to a material card."""
    # Material cards are a shared catalog — require_user is sufficient.
    card = db.get(MaterialCard, card_id)
    if not card:
        raise HTTPException(404, "Material card not found")
    att = await attachment_service.store_and_attach(
        db,
        model=MaterialCardAttachment,
        fk_field="material_card_id",
        entity_label="Materials",
        entity_id=card_id,
        file=file,
        user=user,
    )
    return attachment_service.serialize(att)


@router.delete("/api/material-card-attachments/{att_id}")
async def delete_material_card_attachment(
    att_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Delete a material card attachment (best-effort cloud delete + DB row removal)."""
    att = db.get(MaterialCardAttachment, att_id)
    if not att:
        raise HTTPException(404, "Attachment not found")
    # Material cards are a shared catalog — require_user is sufficient.
    if not db.get(MaterialCard, att.material_card_id):
        raise HTTPException(404, "Attachment not found")
    return await attachment_service.remove_attachment(db, att, user)
