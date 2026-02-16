"""Admin API — User management & data import. Admin-only endpoints."""

import csv
import io
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..dependencies import require_user
from ..models import User, Company, CustomerSite, SiteContact, VendorCard, VendorContact

router = APIRouter(tags=["admin"])
log = logging.getLogger(__name__)


def _require_admin(user: User):
    if user.email.lower() not in settings.admin_emails:
        raise HTTPException(403, "Admin access required")


# ── User Management ──────────────────────────────────────────────────

class CreateUserRequest(BaseModel):
    name: str
    email: str
    role: str = "buyer"


class UpdateUserRequest(BaseModel):
    name: str | None = None
    role: str | None = None


@router.get("/api/admin/users")
def list_users(
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    _require_admin(user)
    users = db.query(User).order_by(User.name).all()
    return [{
        "id": u.id,
        "name": u.name,
        "email": u.email,
        "role": u.role,
        "m365_connected": u.m365_connected,
        "created_at": u.created_at.isoformat() if u.created_at else None,
    } for u in users]


@router.post("/api/admin/users")
def create_user(
    body: CreateUserRequest,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    _require_admin(user)
    if body.role not in ("buyer", "sales"):
        raise HTTPException(400, "Role must be 'buyer' or 'sales'")
    existing = db.query(User).filter(User.email == body.email.lower().strip()).first()
    if existing:
        raise HTTPException(409, "User with this email already exists")
    new_user = User(
        name=body.name.strip(),
        email=body.email.lower().strip(),
        role=body.role,
    )
    db.add(new_user)
    db.commit()
    return {"id": new_user.id, "name": new_user.name, "email": new_user.email, "role": new_user.role}


@router.put("/api/admin/users/{user_id}")
def update_user(
    user_id: int,
    body: UpdateUserRequest,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    _require_admin(user)
    target = db.get(User, user_id)
    if not target:
        raise HTTPException(404, "User not found")
    if body.name is not None:
        target.name = body.name.strip()
    if body.role is not None:
        if body.role not in ("buyer", "sales"):
            raise HTTPException(400, "Role must be 'buyer' or 'sales'")
        target.role = body.role
    db.commit()
    return {"id": target.id, "name": target.name, "email": target.email, "role": target.role}


@router.delete("/api/admin/users/{user_id}")
def delete_user(
    user_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    _require_admin(user)
    target = db.get(User, user_id)
    if not target:
        raise HTTPException(404, "User not found")
    if target.id == user.id:
        raise HTTPException(400, "Cannot delete yourself")
    db.delete(target)
    db.commit()
    return {"status": "deleted"}


# ── Data Import ──────────────────────────────────────────────────────

@router.post("/api/admin/import/customers")
async def import_customers(
    file: UploadFile = File(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Import customers from CSV. Expected columns: company_name, site_name,
    contact_name, contact_email, contact_phone, contact_title,
    address_line1, city, state, zip, country, payment_terms, shipping_terms"""
    _require_admin(user)
    content = await file.read()
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("latin-1")

    reader = csv.DictReader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        raise HTTPException(400, "No data rows found")

    companies_created = 0
    sites_created = 0
    contacts_created = 0

    # Group by company_name
    seen_companies = {}
    seen_sites = {}

    for row in rows:
        company_name = (row.get("company_name") or "").strip()
        if not company_name:
            continue

        # Find or create company
        key = company_name.lower()
        if key not in seen_companies:
            company = db.query(Company).filter(
                Company.name.ilike(company_name)
            ).first()
            if not company:
                company = Company(name=company_name)
                db.add(company)
                db.flush()
                companies_created += 1
            seen_companies[key] = company

        company = seen_companies[key]

        # Find or create site
        site_name = (row.get("site_name") or company_name).strip()
        site_key = f"{key}|{site_name.lower()}"
        if site_key not in seen_sites:
            site = db.query(CustomerSite).filter(
                CustomerSite.company_id == company.id,
                CustomerSite.site_name.ilike(site_name),
            ).first()
            if not site:
                site = CustomerSite(
                    company_id=company.id,
                    site_name=site_name,
                    owner_id=user.id,
                    address_line1=row.get("address_line1", "").strip() or None,
                    city=row.get("city", "").strip() or None,
                    state=row.get("state", "").strip() or None,
                    zip=row.get("zip", "").strip() or None,
                    country=row.get("country", "").strip() or None,
                    payment_terms=row.get("payment_terms", "").strip() or None,
                    shipping_terms=row.get("shipping_terms", "").strip() or None,
                )
                db.add(site)
                db.flush()
                sites_created += 1
            seen_sites[site_key] = site

        site = seen_sites[site_key]

        # Create contact if provided
        contact_name = (row.get("contact_name") or "").strip()
        contact_email = (row.get("contact_email") or "").strip()
        if contact_name or contact_email:
            existing_contact = None
            if contact_email:
                existing_contact = db.query(SiteContact).filter(
                    SiteContact.customer_site_id == site.id,
                    SiteContact.email == contact_email.lower(),
                ).first()
            if not existing_contact:
                sc = SiteContact(
                    customer_site_id=site.id,
                    full_name=contact_name or contact_email or "Unknown",
                    email=contact_email.lower() or None,
                    phone=(row.get("contact_phone") or "").strip() or None,
                    title=(row.get("contact_title") or "").strip() or None,
                )
                db.add(sc)
                contacts_created += 1

    db.commit()
    return {
        "status": "ok",
        "companies_created": companies_created,
        "sites_created": sites_created,
        "contacts_created": contacts_created,
        "rows_processed": len(rows),
    }


@router.post("/api/admin/import/vendors")
async def import_vendors(
    file: UploadFile = File(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Import vendors from CSV. Expected columns: vendor_name, domain, website,
    contact_name, contact_email, contact_phone, contact_title"""
    _require_admin(user)
    content = await file.read()
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("latin-1")

    reader = csv.DictReader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        raise HTTPException(400, "No data rows found")

    vendors_created = 0
    contacts_created = 0
    seen_vendors = {}

    for row in rows:
        vendor_name = (row.get("vendor_name") or "").strip()
        if not vendor_name:
            continue

        normalized = vendor_name.lower().strip()
        if normalized not in seen_vendors:
            vc = db.query(VendorCard).filter(
                VendorCard.normalized_name == normalized
            ).first()
            if not vc:
                domain = (row.get("domain") or "").strip() or None
                website = (row.get("website") or "").strip() or None
                vc = VendorCard(
                    normalized_name=normalized,
                    display_name=vendor_name,
                    domain=domain,
                    website=website,
                )
                db.add(vc)
                db.flush()
                vendors_created += 1
            seen_vendors[normalized] = vc

        vc = seen_vendors[normalized]

        # Create vendor contact if provided
        contact_name = (row.get("contact_name") or "").strip()
        contact_email = (row.get("contact_email") or "").strip()
        if contact_name or contact_email:
            existing = None
            if contact_email:
                existing = db.query(VendorContact).filter(
                    VendorContact.vendor_card_id == vc.id,
                    VendorContact.email == contact_email.lower(),
                ).first()
            if not existing:
                vcon = VendorContact(
                    vendor_card_id=vc.id,
                    name=contact_name or None,
                    email=contact_email.lower() or None,
                    phone=(row.get("contact_phone") or "").strip() or None,
                    title=(row.get("contact_title") or "").strip() or None,
                )
                db.add(vcon)
                contacts_created += 1

    db.commit()
    return {
        "status": "ok",
        "vendors_created": vendors_created,
        "contacts_created": contacts_created,
        "rows_processed": len(rows),
    }
