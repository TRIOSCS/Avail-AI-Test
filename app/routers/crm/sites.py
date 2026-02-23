import asyncio

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session, joinedload

from ...database import get_db
from ...dependencies import require_buyer, require_user
from ...models import Company, CustomerSite, Requisition, SiteContact, User
from ...schemas.crm import SiteContactCreate, SiteContactUpdate, SiteCreate, SiteUpdate

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
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(site, field, value)
    db.commit()
    return {"ok": True}


@router.get("/api/sites/{site_id}")
async def get_site(
    site_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    site = db.get(
        CustomerSite,
        site_id,
        options=[joinedload(CustomerSite.company), joinedload(CustomerSite.owner)],
    )
    if not site:
        raise HTTPException(404, "Site not found")

    loop = asyncio.get_running_loop()

    def _q_reqs():
        return db.query(Requisition).filter(
            Requisition.customer_site_id == site_id,
        ).order_by(Requisition.created_at.desc()).limit(20).all()

    def _q_contacts():
        return db.query(SiteContact).filter(
            SiteContact.customer_site_id == site_id,
        ).order_by(SiteContact.is_primary.desc(), SiteContact.full_name).all()

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


@router.get("/api/sites/{site_id}/contacts")
async def list_site_contacts(
    site_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    site = db.get(CustomerSite, site_id)
    if not site:
        raise HTTPException(404, "Site not found")
    contacts = (
        db.query(SiteContact)
        .filter(
            SiteContact.customer_site_id == site_id,
        )
        .order_by(SiteContact.is_primary.desc(), SiteContact.full_name)
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
    if payload.is_primary:
        db.query(SiteContact).filter(
            SiteContact.customer_site_id == site_id,
            SiteContact.is_primary == True,  # noqa: E712
        ).update({"is_primary": False})
    contact = SiteContact(customer_site_id=site_id, **payload.model_dump())
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
