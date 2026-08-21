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

from collections.abc import Callable
from dataclasses import dataclass

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
# Per-kind entity gates + endpoint spec (shared by list/upload/delete below)
# ---------------------------------------------------------------------------


def _company_access(db: Session, user: User, company_id: int) -> bool:
    """True if the user may manage the company (user_can_access_company)."""
    return user_can_access_company(db, user, company_id) is not None


def _site_contact_access(db: Session, user: User, contact_id: int) -> bool:
    """True if the contact exists and the user may manage its owning company (contact →
    CustomerSite → Company chain)."""
    contact = db.get(SiteContact, contact_id)
    if not contact:
        return False
    site = db.get(CustomerSite, contact.customer_site_id)
    return bool(site and user_can_access_company(db, user, site.company_id))


def _material_access(db: Session, user: User, card_id: int) -> bool:
    """Material cards are a shared catalog — existence + require_user is sufficient."""
    return db.get(MaterialCard, card_id) is not None


def _vendor_access(db: Session, user: User, vendor_id: int) -> bool:
    """Any authenticated user may access any existing vendor."""
    return db_get_vendor_card(db, vendor_id) is not None


def _vendor_contact_access(db: Session, user: User, contact_id: int) -> bool:
    """Any authenticated user may access any existing vendor contact."""
    return db.get(VendorContact, contact_id) is not None


@dataclass(frozen=True)
class _AttachmentSpec:
    """Everything kind-specific about one entity's attachment endpoint triplet."""

    model: type
    fk_field: str
    entity_label: str
    not_found: str  # 404 detail when the entity is missing / not accessible
    access: Callable[[Session, User, int], bool]


_ATTACH_SPECS: dict[str, _AttachmentSpec] = {
    "company": _AttachmentSpec(CompanyAttachment, "company_id", "Companies", "Company not found", _company_access),
    "contact": _AttachmentSpec(
        SiteContactAttachment, "site_contact_id", "Contacts", "Contact not found", _site_contact_access
    ),
    "material": _AttachmentSpec(
        MaterialCardAttachment, "material_card_id", "Materials", "Material card not found", _material_access
    ),
    "vendor_card": _AttachmentSpec(
        VendorCardAttachment, "vendor_card_id", "Vendors", "Vendor not found", _vendor_access
    ),
    "vendor_contact": _AttachmentSpec(
        VendorContactAttachment,
        "vendor_contact_id",
        "VendorContacts",
        "Vendor contact not found",
        _vendor_contact_access,
    ),
}


def _list_attachments(kind: str, entity_id: int, request: Request, user: User, db: Session):
    """Shared list body: gate on the entity, query newest-first, render/serialize."""
    spec = _ATTACH_SPECS[kind]
    if not spec.access(db, user, entity_id):
        raise HTTPException(404, spec.not_found)
    atts = (
        db.query(spec.model)
        .options(selectinload(spec.model.uploaded_by))
        .filter(getattr(spec.model, spec.fk_field) == entity_id)
        .order_by(spec.model.created_at.desc())
        .all()
    )
    return attachment_service.attachment_list_response(request, kind=kind, entity_id=entity_id, rows=atts)


async def _upload_attachment(kind: str, entity_id: int, file: UploadFile, user: User, db: Session):
    """Shared upload body: gate on the entity, store + attach, serialize."""
    spec = _ATTACH_SPECS[kind]
    if not spec.access(db, user, entity_id):
        raise HTTPException(404, spec.not_found)
    att = await attachment_service.store_and_attach(
        db,
        model=spec.model,
        fk_field=spec.fk_field,
        entity_label=spec.entity_label,
        entity_id=entity_id,
        file=file,
        user=user,
    )
    return attachment_service.serialize(att)


async def _delete_attachment(kind: str, att_id: int, user: User, db: Session):
    """Shared delete body: gate on the owning entity, best-effort cloud delete + row
    removal."""
    spec = _ATTACH_SPECS[kind]
    att = db.get(spec.model, att_id)
    if not att:
        raise HTTPException(404, "Attachment not found")
    if not spec.access(db, user, getattr(att, spec.fk_field)):
        raise HTTPException(404, "Attachment not found")
    return await attachment_service.remove_attachment(db, att, user)


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
        if not _site_contact_access(db, user, att.site_contact_id):
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
    return _list_attachments("company", company_id, request, user, db)


@router.post("/api/companies/{company_id}/attachments")
async def upload_company_attachment(
    company_id: int,
    file: UploadFile = File(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Upload a file and attach it to a company."""
    return await _upload_attachment("company", company_id, file, user, db)


@router.delete("/api/company-attachments/{att_id}")
async def delete_company_attachment(
    att_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Delete a company attachment (best-effort cloud delete + DB row removal)."""
    return await _delete_attachment("company", att_id, user, db)


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
    return _list_attachments("contact", contact_id, request, user, db)


@router.post("/api/contacts/{contact_id}/attachments")
async def upload_contact_attachment(
    contact_id: int,
    file: UploadFile = File(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Upload a file and attach it to a site contact."""
    return await _upload_attachment("contact", contact_id, file, user, db)


@router.delete("/api/contact-attachments/{att_id}")
async def delete_contact_attachment(
    att_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Delete a contact attachment (best-effort cloud delete + DB row removal)."""
    return await _delete_attachment("contact", att_id, user, db)


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
    return _list_attachments("material", card_id, request, user, db)


@router.post("/api/material-cards/{card_id}/attachments")
async def upload_material_card_attachment(
    card_id: int,
    file: UploadFile = File(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Upload a file and attach it to a material card."""
    return await _upload_attachment("material", card_id, file, user, db)


@router.delete("/api/material-card-attachments/{att_id}")
async def delete_material_card_attachment(
    att_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Delete a material card attachment (best-effort cloud delete + DB row removal)."""
    return await _delete_attachment("material", att_id, user, db)


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
    return _list_attachments("vendor_card", vendor_id, request, user, db)


@router.post("/api/vendors/{vendor_id}/attachments")
async def upload_vendor_card_attachment(
    vendor_id: int,
    file: UploadFile = File(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Upload a file and attach it to a vendor card."""
    return await _upload_attachment("vendor_card", vendor_id, file, user, db)


@router.delete("/api/vendor-attachments/{att_id}")
async def delete_vendor_card_attachment(
    att_id: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Delete a vendor card attachment (best-effort cloud delete + DB row removal)."""
    return await _delete_attachment("vendor_card", att_id, user, db)


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
    return _list_attachments("vendor_contact", contact_id, request, user, db)


@router.post("/api/vendor-contacts/{contact_id}/attachments")
async def upload_vendor_contact_attachment(
    contact_id: int,
    file: UploadFile = File(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Upload a file and attach it to a vendor contact."""
    return await _upload_attachment("vendor_contact", contact_id, file, user, db)


@router.delete("/api/vendor-contact-attachments/{att_id}")
async def delete_vendor_contact_attachment(
    att_id: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Delete a vendor contact attachment (best-effort cloud delete + DB row
    removal)."""
    return await _delete_attachment("vendor_contact", att_id, user, db)
