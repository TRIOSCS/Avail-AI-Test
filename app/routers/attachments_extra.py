"""attachments_extra.py — Company, Contact, MaterialCard, and Vendor attachment
endpoints + unified serve route.

Provides:
  GET/POST /api/companies/{company_id}/attachments
  DELETE   /api/company-attachments/{att_id}
  GET/POST /api/contacts/{contact_id}/attachments
  DELETE   /api/contact-attachments/{att_id}
  GET/POST /api/material-cards/{card_id}/attachments
  DELETE   /api/material-card-attachments/{att_id}
  GET/POST /api/vendors/{vendor_id}/attachments
  DELETE   /api/vendor-attachments/{att_id}
  GET/POST /api/vendor-contacts/{contact_id}/attachments
  DELETE   /api/vendor-contact-attachments/{att_id}
  GET      /api/attachments/{kind}/{att_id}/content   (unified serve)

Access model:
  company        — any authenticated user may access any company.
  contact        — resolved SiteContact → CustomerSite → Company; same company check.
  material       — shared catalog; require_user is sufficient.
  vendor_card    — any authenticated user may access any vendor.
  vendor_contact — resolved VendorContact → VendorCard; same vendor check.

Called by: app/main.py (router registration)
Depends on: app/services/attachment_service, all attachment models, app/dependencies
"""

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from sqlalchemy.orm import Session, selectinload

from ..database import get_db
from ..dependencies import can_manage_account, get_req_for_user, require_admin, require_user
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
    VendorCard,
    VendorCardAttachment,
    VendorContact,
    VendorContactAttachment,
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
    "vendor_card": VendorCardAttachment,
    "vendor_contact": VendorContactAttachment,
}


# ---------------------------------------------------------------------------
# Company-access helper
# ---------------------------------------------------------------------------


def user_can_access_company(db: Session, user: User, company_id: int) -> Company | None:
    """Return the Company if the user may manage it, otherwise None.

    Enforces account-ownership via can_manage_account: manager/admin see everything;
    otherwise the user must be the account owner, a site-owner under the company, or a
    collaborator. Returns None when the company does not exist OR the user lacks access —
    callers raise 404 in both cases to avoid existence leaks.

    Contact-attachment callers resolve the owning company through the
    contact → CustomerSite → Company chain and pass that company_id here, so this single
    gate protects company and contact attachments alike.
    """
    company = db.get(Company, company_id)
    if company is None or not can_manage_account(user, company, db):
        return None
    return company


# ---------------------------------------------------------------------------
# Vendor-access helper
# ---------------------------------------------------------------------------


def db_get_vendor_card(db: Session, vendor_id: int) -> VendorCard | None:
    """Return the VendorCard if it exists, otherwise None.

    Any authenticated user may access any vendor (same permissiveness as
    vendor_detail_partial). Named db_get_vendor_card so tests can patch it cleanly.
    """
    return db.get(VendorCard, vendor_id)


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
    elif kind == "vendor_card":
        if not db_get_vendor_card(db, att.vendor_card_id):
            raise HTTPException(404, "Attachment not found")
    elif kind == "vendor_contact":
        contact = db.get(VendorContact, att.vendor_contact_id)
        if not contact:
            raise HTTPException(404, "Attachment not found")
        if not db_get_vendor_card(db, contact.vendor_card_id):
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
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """List attachments on a company, newest first (HTML for HTMX, JSON otherwise)."""
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
    return attachment_service.attachment_list_response(request, kind="company", entity_id=company_id, rows=atts)


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
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """List attachments on a site contact, newest first (HTML for HTMX, JSON
    otherwise)."""
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
    return attachment_service.attachment_list_response(request, kind="contact", entity_id=contact_id, rows=atts)


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
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """List attachments on a material card, newest first (HTML for HTMX, JSON
    otherwise)."""
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
    return attachment_service.attachment_list_response(request, kind="material", entity_id=card_id, rows=atts)


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


# ---------------------------------------------------------------------------
# Vendor card attachment endpoints
# ---------------------------------------------------------------------------


@router.get("/api/vendors/{vendor_id}/attachments")
async def list_vendor_card_attachments(
    vendor_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """List attachments on a vendor card, newest first (HTML for HTMX, JSON
    otherwise)."""
    vendor = db_get_vendor_card(db, vendor_id)
    if not vendor:
        raise HTTPException(404, "Vendor not found")
    atts = (
        db.query(VendorCardAttachment)
        .options(selectinload(VendorCardAttachment.uploaded_by))
        .filter(VendorCardAttachment.vendor_card_id == vendor_id)
        .order_by(VendorCardAttachment.created_at.desc())
        .all()
    )
    return attachment_service.attachment_list_response(request, kind="vendor_card", entity_id=vendor_id, rows=atts)


@router.post("/api/vendors/{vendor_id}/attachments")
async def upload_vendor_card_attachment(
    vendor_id: int,
    file: UploadFile = File(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Upload a file and attach it to a vendor card."""
    vendor = db_get_vendor_card(db, vendor_id)
    if not vendor:
        raise HTTPException(404, "Vendor not found")
    att = await attachment_service.store_and_attach(
        db,
        model=VendorCardAttachment,
        fk_field="vendor_card_id",
        entity_label="Vendors",
        entity_id=vendor_id,
        file=file,
        user=user,
    )
    return attachment_service.serialize(att)


@router.delete("/api/vendor-attachments/{att_id}")
async def delete_vendor_card_attachment(
    att_id: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Delete a vendor card attachment (best-effort cloud delete + DB row removal)."""
    att = db.get(VendorCardAttachment, att_id)
    if not att:
        raise HTTPException(404, "Attachment not found")
    if not db_get_vendor_card(db, att.vendor_card_id):
        raise HTTPException(404, "Attachment not found")
    return await attachment_service.remove_attachment(db, att, user)


# ---------------------------------------------------------------------------
# Vendor contact attachment endpoints
# ---------------------------------------------------------------------------


@router.get("/api/vendor-contacts/{contact_id}/attachments")
async def list_vendor_contact_attachments(
    contact_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """List attachments on a vendor contact, newest first (HTML for HTMX, JSON
    otherwise)."""
    contact = db.get(VendorContact, contact_id)
    if not contact:
        raise HTTPException(404, "Vendor contact not found")
    atts = (
        db.query(VendorContactAttachment)
        .options(selectinload(VendorContactAttachment.uploaded_by))
        .filter(VendorContactAttachment.vendor_contact_id == contact_id)
        .order_by(VendorContactAttachment.created_at.desc())
        .all()
    )
    return attachment_service.attachment_list_response(request, kind="vendor_contact", entity_id=contact_id, rows=atts)


@router.post("/api/vendor-contacts/{contact_id}/attachments")
async def upload_vendor_contact_attachment(
    contact_id: int,
    file: UploadFile = File(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Upload a file and attach it to a vendor contact."""
    contact = db.get(VendorContact, contact_id)
    if not contact:
        raise HTTPException(404, "Vendor contact not found")
    att = await attachment_service.store_and_attach(
        db,
        model=VendorContactAttachment,
        fk_field="vendor_contact_id",
        entity_label="VendorContacts",
        entity_id=contact_id,
        file=file,
        user=user,
    )
    return attachment_service.serialize(att)


@router.delete("/api/vendor-contact-attachments/{att_id}")
async def delete_vendor_contact_attachment(
    att_id: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Delete a vendor contact attachment (best-effort cloud delete + DB row
    removal)."""
    att = db.get(VendorContactAttachment, att_id)
    if not att:
        raise HTTPException(404, "Attachment not found")
    if not db.get(VendorContact, att.vendor_contact_id):
        raise HTTPException(404, "Attachment not found")
    return await attachment_service.remove_attachment(db, att, user)
