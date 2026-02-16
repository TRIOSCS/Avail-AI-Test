"""Admin API — User management, system config, health, data import."""

import csv
import io
import logging
import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..database import get_db
from ..dependencies import require_admin, require_settings_access
from ..models import ApiSource, User, Company, CustomerSite, SiteContact, VendorCard, VendorContact
from ..services.admin_service import (
    list_users, update_user, get_all_config, set_config_value,
    get_scoring_weights, get_system_health, VALID_ROLES,
)
from ..services.credential_service import encrypt_value, decrypt_value, mask_value

router = APIRouter(tags=["admin"])
log = logging.getLogger(__name__)


# ── Schemas ──────────────────────────────────────────────────────────

class CreateUserRequest(BaseModel):
    name: str
    email: str
    role: str = "buyer"


class UserUpdateRequest(BaseModel):
    name: str | None = None
    role: str | None = None
    is_active: bool | None = None


class ConfigUpdateRequest(BaseModel):
    value: str = Field(..., min_length=1, max_length=500)


# ── User Management (admin only) ─────────────────────────────────────

@router.get("/api/admin/users")
def api_list_users(user: User = Depends(require_admin), db: Session = Depends(get_db)):
    return list_users(db)


@router.post("/api/admin/users")
def api_create_user(
    body: CreateUserRequest,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if body.role not in VALID_ROLES:
        raise HTTPException(400, f"Role must be one of: {', '.join(VALID_ROLES)}")
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
def api_update_user(
    user_id: int,
    body: UserUpdateRequest,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    result = update_user(db, user_id, body.model_dump(exclude_none=False), user)
    if "error" in result:
        raise HTTPException(result.get("status", 400), result["error"])
    return result


@router.delete("/api/admin/users/{user_id}")
def api_delete_user(
    user_id: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    target = db.get(User, user_id)
    if not target:
        raise HTTPException(404, "User not found")
    if target.id == user.id:
        raise HTTPException(400, "Cannot delete yourself")
    db.delete(target)
    db.commit()
    return {"status": "deleted"}


# ── System Config (admin for writes, settings_access for reads) ──────

@router.get("/api/admin/config")
def api_get_config(
    user: User = Depends(require_settings_access),
    db: Session = Depends(get_db),
):
    return get_all_config(db)


@router.put("/api/admin/config/{key}")
def api_set_config(
    key: str,
    body: ConfigUpdateRequest,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    result = set_config_value(db, key, body.value, user.email)
    if "error" in result:
        raise HTTPException(result.get("status", 400), result["error"])
    return result


# ── System Health (settings_access) ──────────────────────────────────

@router.get("/api/admin/health")
def api_health(
    user: User = Depends(require_settings_access),
    db: Session = Depends(get_db),
):
    return get_system_health(db)


# ── Credential Management (admin only) ────────────────────────────────

@router.get("/api/admin/sources/{source_id}/credentials")
def api_get_credentials(
    source_id: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Return masked credential values for a source."""
    src = db.get(ApiSource, source_id)
    if not src:
        raise HTTPException(404, "Source not found")
    result = {}
    for var_name in (src.env_vars or []):
        encrypted = (src.credentials or {}).get(var_name)
        if encrypted:
            try:
                plain = decrypt_value(encrypted)
                result[var_name] = {"status": "set", "masked": mask_value(plain), "source": "db"}
            except Exception:
                result[var_name] = {"status": "error", "masked": "", "source": "db"}
        elif os.getenv(var_name):
            result[var_name] = {"status": "set", "masked": mask_value(os.getenv(var_name)), "source": "env"}
        else:
            result[var_name] = {"status": "empty", "masked": "", "source": "none"}
    return {"source_id": src.id, "source_name": src.name, "credentials": result}


@router.put("/api/admin/sources/{source_id}/credentials")
def api_set_credentials(
    source_id: int,
    body: dict,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Set credential values for a source. Body: {VAR_NAME: "plaintext_value", ...}"""
    src = db.get(ApiSource, source_id)
    if not src:
        raise HTTPException(404, "Source not found")
    valid_vars = set(src.env_vars or [])
    creds = dict(src.credentials or {})
    updated = []
    for var_name, value in body.items():
        if var_name not in valid_vars:
            continue
        value = (value or "").strip()
        if value:
            creds[var_name] = encrypt_value(value)
            updated.append(var_name)
        else:
            creds.pop(var_name, None)
            updated.append(var_name)
    src.credentials = creds
    db.commit()
    log.info(f"Credentials updated for {src.name} by {user.email}: {updated}")
    return {"status": "ok", "updated": updated}


@router.delete("/api/admin/sources/{source_id}/credentials/{var_name}")
def api_delete_credential(
    source_id: int,
    var_name: str,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Remove a single credential from a source."""
    src = db.get(ApiSource, source_id)
    if not src:
        raise HTTPException(404, "Source not found")
    creds = dict(src.credentials or {})
    removed = creds.pop(var_name, None)
    src.credentials = creds
    db.commit()
    log.info(f"Credential {var_name} removed from {src.name} by {user.email}")
    return {"status": "removed" if removed else "not_found"}


# ── Data Import (admin only) ─────────────────────────────────────────

@router.post("/api/admin/import/customers")
async def import_customers(
    file: UploadFile = File(...),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Import customers from CSV. Expected columns: company_name, site_name,
    contact_name, contact_email, contact_phone, contact_title,
    address_line1, city, state, zip, country, payment_terms, shipping_terms"""
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

    seen_companies = {}
    seen_sites = {}

    for row in rows:
        company_name = (row.get("company_name") or "").strip()
        if not company_name:
            continue

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
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Import vendors from CSV. Expected columns: vendor_name, domain, website,
    contact_name, contact_email, contact_phone, contact_title"""
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
                    full_name=contact_name or None,
                    email=contact_email.lower() or None,
                    phone=(row.get("contact_phone") or "").strip() or None,
                    title=(row.get("contact_title") or "").strip() or None,
                    source="csv_import",
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
