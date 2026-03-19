import asyncio

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from ...cache.decorators import invalidate_prefix
from ...config import settings
from ...database import get_db
from ...dependencies import is_admin as _is_admin
from ...dependencies import require_user
from ...models import Company, CustomerSite, Requisition, SiteContact, User
from ...schemas.crm import SiteContactCreate, SiteContactUpdate, SiteCreate, SiteUpdate
from ...schemas.v13_features import SiteContactNoteLog
from ...utils.async_helpers import safe_background_task
from ...utils.phone_utils import format_phone_e164

router = APIRouter()

# ── Customer Sites ───────────────────────────────────────────────────────


@router.post("/api/companies/{company_id}/sites")
async def add_site(
    company_id: int,
    payload: SiteCreate,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")
    site = CustomerSite(company_id=company_id, **payload.model_dump())
    db.add(site)
    db.commit()
    invalidate_prefix("companies_typeahead")
    invalidate_prefix("company_detail")

    # Trigger customer enrichment waterfall in the background for the parent company
    if settings.customer_enrichment_enabled and (company.domain or company.website):

        async def _bg_enrich(cid):
            from ...database import SessionLocal

            s = SessionLocal()
            try:
                from ...services.customer_enrichment_service import enrich_customer_account

                await enrich_customer_account(cid, s, force=False)
                s.commit()
            except Exception as e:
                logger.warning("Site-create auto-enrich error for company %d: %s", cid, e)
                s.rollback()
            finally:
                s.close()

        await safe_background_task(_bg_enrich(company_id), task_name="enrich_site_bg")

    return {"id": site.id, "site_name": site.site_name}


@router.put("/api/sites/{site_id}")
async def update_site(
    site_id: int,
    payload: SiteUpdate,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    site = db.get(CustomerSite, site_id)
    if not site:
        raise HTTPException(404, "Site not found")

    updates = payload.model_dump(exclude_unset=True)

    # Admin-only ownership guard
    if "owner_id" in updates:
        new_owner = updates["owner_id"]
        caller_is_admin = _is_admin(user)

        if site.owner_id is not None and not caller_is_admin:
            # Non-admin cannot reassign an owned site
            raise HTTPException(403, "Only admins can reassign owned sites")
        if new_owner is None and not caller_is_admin:
            # Non-admin cannot unassign a site
            raise HTTPException(403, "Only admins can unassign sites")

    for field, value in updates.items():
        setattr(site, field, value)
    db.commit()
    invalidate_prefix("company_detail")
    return {"ok": True}


@router.get("/api/sites/{site_id}")
async def get_site(site_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    site = db.get(
        CustomerSite,
        site_id,
        options=[joinedload(CustomerSite.company), joinedload(CustomerSite.owner)],
    )
    if not site:
        raise HTTPException(404, "Site not found")

    loop = asyncio.get_running_loop()

    def _q_reqs():
        return (
            db.query(Requisition)
            .filter(
                Requisition.customer_site_id == site_id,
            )
            .order_by(Requisition.created_at.desc())
            .limit(20)
            .all()
        )

    def _q_contacts():
        return (
            db.query(SiteContact)
            .filter(
                SiteContact.customer_site_id == site_id,
            )
            .order_by(SiteContact.is_primary.desc(), SiteContact.full_name)
            .limit(500)
            .all()
        )

    reqs, contacts = await asyncio.gather(
        loop.run_in_executor(None, _q_reqs),
        loop.run_in_executor(None, _q_contacts),
    )
    return {
        "id": site.id,
        "company_id": site.company_id,
        "company_name": site.company.name if site.company else None,
        "company_domain": site.company.domain if site.company else None,
        "company_website": site.company.website if site.company else None,
        "site_name": site.site_name,
        "owner_id": site.owner_id,
        "owner_name": site.owner.name if site.owner else None,
        "contact_name": site.contact_name,
        "contact_email": site.contact_email,
        "contact_phone": site.contact_phone,
        "contact_title": site.contact_title,
        "contact_linkedin": site.contact_linkedin,
        "address_line1": site.address_line1,
        "address_line2": site.address_line2,
        "city": site.city,
        "state": site.state,
        "zip": site.zip,
        "country": site.country,
        "payment_terms": site.payment_terms,
        "shipping_terms": site.shipping_terms,
        "site_type": site.site_type,
        "timezone": site.timezone,
        "receiving_hours": site.receiving_hours,
        "carrier_account": site.carrier_account,
        "notes": site.notes,
        "contacts": [
            {
                "id": c.id,
                "full_name": c.full_name,
                "title": c.title,
                "email": c.email,
                "phone": c.phone,
                "notes": c.notes,
                "is_primary": c.is_primary,
                "is_active": c.is_active,
                "contact_status": c.contact_status,
                "phone_verified": c.phone_verified or False,
                "email_verified": c.email_verified or False,
                "email_verification_status": c.email_verification_status,
                "enrichment_source": c.enrichment_source,
                "contact_role": c.contact_role,
                "linkedin_url": c.linkedin_url,
            }
            for c in contacts
        ],
        "recent_reqs": [
            {
                "id": r.id,
                "name": r.name,
                "status": r.status,
                "requirement_count": len(r.requirements),
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in reqs
        ],
    }


# ── Site Contacts ─────────────────────────────────────────────────────────


@router.get("/api/customer-contacts")
async def list_customer_contacts(
    include_archived: bool = False,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """All customer contacts across all sites, for the unified Contacts view."""
    q = (
        db.query(SiteContact)
        .join(CustomerSite, SiteContact.customer_site_id == CustomerSite.id)
        .join(Company, CustomerSite.company_id == Company.id)
        .filter(Company.is_active == True)  # noqa: E712
    )
    if not include_archived:
        q = q.filter(SiteContact.is_active == True)  # noqa: E712
    contacts = (
        q.options(
            joinedload(SiteContact.customer_site).joinedload(CustomerSite.company),
        )
        .order_by(SiteContact.full_name)
        .limit(5000)
        .all()
    )
    return [
        {
            "id": c.id,
            "full_name": c.full_name,
            "title": c.title,
            "email": c.email,
            "phone": c.phone,
            "notes": c.notes,
            "is_primary": c.is_primary,
            "is_active": c.is_active,
            "contact_status": c.contact_status or "new",
            "company_id": c.customer_site.company_id if c.customer_site else None,
            "company_name": c.customer_site.company.name if c.customer_site and c.customer_site.company else None,
            "site_id": c.customer_site_id,
            "customer_site_id": c.customer_site_id,
            "site_contact_id": c.id,
            "site_name": c.customer_site.site_name if c.customer_site else None,
            "contact_type": "customer",
            "created_at": c.created_at.isoformat() if c.created_at else None,
        }
        for c in contacts
    ]


@router.get("/api/sites/{site_id}/contacts")
async def list_site_contacts(
    site_id: int,
    include_archived: bool = False,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    site = db.get(CustomerSite, site_id)
    if not site:
        raise HTTPException(404, "Site not found")
    q = db.query(SiteContact).filter(
        SiteContact.customer_site_id == site_id,
    )
    if not include_archived:
        q = q.filter(SiteContact.is_active == True)  # noqa: E712
    contacts = q.order_by(SiteContact.is_primary.desc(), SiteContact.full_name).limit(500).all()
    return [
        {
            "id": c.id,
            "full_name": c.full_name,
            "title": c.title,
            "email": c.email,
            "phone": c.phone,
            "notes": c.notes,
            "is_primary": c.is_primary,
            "is_active": c.is_active,
            "contact_status": c.contact_status or "new",
        }
        for c in contacts
    ]


@router.post("/api/sites/{site_id}/contacts")
async def create_site_contact(
    site_id: int,
    payload: SiteContactCreate,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    site = db.get(CustomerSite, site_id)
    if not site:
        raise HTTPException(404, "Site not found")
    # Dedup guard: if email provided, check for existing contact on same site
    if payload.email:
        existing = (
            db.query(SiteContact)
            .filter(
                SiteContact.customer_site_id == site_id,
                func.lower(SiteContact.email) == payload.email.strip().lower(),
            )
            .first()
        )
        if existing:
            return {"id": existing.id, "full_name": existing.full_name}
    if payload.is_primary:
        db.query(SiteContact).filter(
            SiteContact.customer_site_id == site_id,
            SiteContact.is_primary == True,  # noqa: E712
        ).update({"is_primary": False})
    data = payload.model_dump()
    if data.get("phone"):
        data["phone"] = format_phone_e164(data["phone"]) or data["phone"]
    contact = SiteContact(customer_site_id=site_id, **data)
    db.add(contact)
    db.commit()
    return {"id": contact.id, "full_name": contact.full_name}


@router.put("/api/sites/{site_id}/contacts/{contact_id}")
async def update_site_contact(
    site_id: int,
    contact_id: int,
    payload: SiteContactUpdate,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    contact = db.get(SiteContact, contact_id)
    if not contact or contact.customer_site_id != site_id:
        raise HTTPException(404, "Contact not found")
    updates = payload.model_dump(exclude_unset=True)
    if updates.get("phone"):
        updates["phone"] = format_phone_e164(updates["phone"]) or updates["phone"]
    if updates.get("is_primary"):
        db.query(SiteContact).filter(
            SiteContact.customer_site_id == site_id,
            SiteContact.is_primary == True,  # noqa: E712
            SiteContact.id != contact_id,
        ).update({"is_primary": False})
    for field, value in updates.items():
        setattr(contact, field, value)
    db.commit()
    return {"ok": True}


@router.delete("/api/sites/{site_id}/contacts/{contact_id}")
async def delete_site_contact(
    site_id: int,
    contact_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    contact = db.get(SiteContact, contact_id)
    if not contact or contact.customer_site_id != site_id:
        raise HTTPException(404, "Contact not found")
    db.delete(contact)
    db.commit()
    return {"ok": True}


# ── Site Contact Notes ───────────────────────────────────────────────────


@router.post("/api/sites/{site_id}/contacts/{contact_id}/notes")
async def log_contact_note(
    site_id: int,
    contact_id: int,
    payload: SiteContactNoteLog,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Log a timestamped note against a site contact."""
    site = db.get(CustomerSite, site_id)
    if not site:
        raise HTTPException(404, "Site not found")
    contact = db.get(SiteContact, contact_id)
    if not contact or contact.customer_site_id != site_id:
        raise HTTPException(404, "Contact not found")

    from app.services.activity_service import log_site_contact_note

    record = log_site_contact_note(
        user_id=user.id,
        site_contact_id=contact_id,
        customer_site_id=site_id,
        company_id=site.company_id,
        notes=payload.notes,
        db=db,
    )
    db.commit()
    return {"status": "logged", "activity_id": record.id}


@router.get("/api/sites/{site_id}/contacts/{contact_id}/notes")
async def get_contact_notes(
    site_id: int,
    contact_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Get note history for a site contact."""
    site = db.get(CustomerSite, site_id)
    if not site:
        raise HTTPException(404, "Site not found")
    contact = db.get(SiteContact, contact_id)
    if not contact or contact.customer_site_id != site_id:
        raise HTTPException(404, "Contact not found")

    from app.services.activity_service import get_site_contact_notes

    notes = get_site_contact_notes(contact_id, db)
    return [
        {
            "id": n.id,
            "notes": n.notes,
            "user_name": n.user.name if n.user else None,
            "created_at": n.created_at.isoformat() if n.created_at else None,
        }
        for n in notes
    ]
