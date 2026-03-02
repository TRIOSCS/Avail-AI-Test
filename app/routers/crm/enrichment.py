from fastapi import APIRouter, Depends, HTTPException, Request
from loguru import logger
from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session

from ...config import settings
from ...database import get_db
from ...dependencies import is_admin as _is_admin
from ...dependencies import require_admin, require_buyer, require_user
from ...models import Company, CustomerSite, SyncLog, User, VendorCard, VendorContact
from ...rate_limit import limiter
from ...schemas.crm import AddContactsToVendor, AddContactToSite, CustomerImportRow, EnrichDomainRequest
from ...services.credential_service import get_credential_cached

router = APIRouter()


# ── Enrichment (shared for vendors + customers) ─────────────────────────


@router.post("/api/enrich/company/{company_id}")
async def enrich_company(
    company_id: int,
    payload: EnrichDomainRequest = EnrichDomainRequest(),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Enrich a customer company with external data."""
    if not get_credential_cached("explorium_enrichment", "EXPLORIUM_API_KEY") and not get_credential_cached(
        "anthropic_ai", "ANTHROPIC_API_KEY"
    ):
        raise HTTPException(
            503,
            "No enrichment providers configured — set EXPLORIUM_API_KEY or ANTHROPIC_API_KEY in .env",
        )
    from ...enrichment_service import apply_enrichment_to_company, enrich_entity

    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")
    domain = company.domain or company.website or ""
    if domain:
        domain = domain.replace("https://", "").replace("http://", "").replace("www.", "").split("/")[0]
    if payload.domain:
        domain = payload.domain
    if not domain:
        raise HTTPException(400, "No domain available — set company website or domain first")
    enrichment = await enrich_entity(domain, company.name)
    updated = apply_enrichment_to_company(company, enrichment)

    # Also trigger customer enrichment waterfall for contact discovery
    waterfall_result = None
    if settings.customer_enrichment_enabled:
        try:
            from ...services.customer_enrichment_service import enrich_customer_account

            waterfall_result = await enrich_customer_account(company_id, db)
        except Exception as e:
            logger.warning("Customer waterfall enrichment error: %s", e)

    db.commit()
    result = {"ok": True, "updated_fields": updated, "enrichment": enrichment}
    if waterfall_result:
        result["customer_enrichment"] = waterfall_result
    return result


@router.post("/api/enrich/vendor/{card_id}")
async def enrich_vendor_card(
    card_id: int,
    payload: EnrichDomainRequest = EnrichDomainRequest(),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Enrich a vendor card with external data."""
    if not get_credential_cached("explorium_enrichment", "EXPLORIUM_API_KEY") and not get_credential_cached(
        "anthropic_ai", "ANTHROPIC_API_KEY"
    ):
        raise HTTPException(
            503,
            "No enrichment providers configured — set EXPLORIUM_API_KEY or ANTHROPIC_API_KEY in .env",
        )
    from ...enrichment_service import apply_enrichment_to_vendor, enrich_entity

    card = db.get(VendorCard, card_id)
    if not card:
        raise HTTPException(404, "Vendor card not found")
    domain = card.domain or card.website or ""
    if domain:
        domain = domain.replace("https://", "").replace("http://", "").replace("www.", "").split("/")[0]
    if payload.domain:
        domain = payload.domain
    if not domain:
        raise HTTPException(400, "No domain available — set vendor website or domain first")
    enrichment = await enrich_entity(domain, card.display_name)
    updated = apply_enrichment_to_vendor(card, enrichment)
    db.commit()
    return {"ok": True, "updated_fields": updated, "enrichment": enrichment}


@router.get("/api/suggested-contacts")
async def get_suggested_contacts(
    domain: str = "",
    name: str = "",
    title: str = "",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Find suggested contacts at a company from enrichment providers."""
    if not get_credential_cached("explorium_enrichment", "EXPLORIUM_API_KEY") and not get_credential_cached(
        "anthropic_ai", "ANTHROPIC_API_KEY"
    ):
        raise HTTPException(
            503,
            "No enrichment providers configured — set EXPLORIUM_API_KEY or ANTHROPIC_API_KEY in .env",
        )
    from ...enrichment_service import find_suggested_contacts

    if not domain:
        raise HTTPException(400, "domain parameter is required")
    domain = domain.replace("https://", "").replace("http://", "").replace("www.", "").split("/")[0]
    contacts = await find_suggested_contacts(domain, name, title)
    return {"domain": domain, "contacts": contacts, "count": len(contacts)}


@router.post("/api/suggested-contacts/add-to-vendor")
async def add_suggested_to_vendor(
    payload: AddContactsToVendor,
    user: User = Depends(require_buyer),
    db: Session = Depends(get_db),
):
    """Add selected suggested contacts to a vendor card."""
    card = db.get(VendorCard, payload.vendor_card_id)
    if not card:
        raise HTTPException(404, "Vendor card not found")
    added = 0
    for c in payload.contacts:
        existing = db.query(VendorContact).filter_by(vendor_card_id=payload.vendor_card_id, email=c.email).first()
        if existing:
            continue
        vc = VendorContact(
            vendor_card_id=payload.vendor_card_id,
            full_name=c.full_name,
            title=c.title,
            email=c.email,
            phone=c.phone,
            linkedin_url=c.linkedin_url,
            contact_type="individual",
            source=c.source,
            label=c.label,
            confidence=70,
        )
        db.add(vc)
        if c.email not in (card.emails or []):
            card.emails = (card.emails or []) + [c.email]
        added += 1
    db.commit()
    return {"ok": True, "added": added}


@router.post("/api/suggested-contacts/add-to-site")
async def add_suggested_to_site(
    payload: AddContactToSite,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Set a suggested contact as the primary contact on a customer site."""
    site = db.get(CustomerSite, payload.site_id)
    if not site:
        raise HTTPException(404, "Customer site not found")
    c = payload.contact
    if c.full_name:
        site.contact_name = c.full_name
    if c.email:
        site.contact_email = c.email
    if c.phone:
        site.contact_phone = c.phone
    if c.title:
        site.contact_title = c.title
    if c.linkedin_url:
        site.contact_linkedin = c.linkedin_url
    db.commit()
    return {"ok": True}


@router.get("/api/admin/sync-logs")
async def get_sync_logs(
    source: str = "",
    limit: int = 20,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """View recent sync log entries."""
    if not _is_admin(user):
        raise HTTPException(403, "Admin only")
    q = db.query(SyncLog).order_by(SyncLog.started_at.desc())
    if source:
        q = q.filter(SyncLog.source == source)
    logs = q.limit(limit).all()
    return [
        {
            "id": entry.id,
            "source": entry.source,
            "status": entry.status,
            "started_at": entry.started_at.isoformat() if entry.started_at else None,
            "finished_at": entry.finished_at.isoformat() if entry.finished_at else None,
            "duration_seconds": entry.duration_seconds,
            "row_counts": entry.row_counts,
            "errors": entry.errors,
        }
        for entry in logs
    ]


# ── Users (simple list for dropdowns) ────────────────────────────────────


@router.get("/api/users/list")
async def list_users_simple(user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Simple user list for owner dropdowns."""
    users = db.query(User).order_by(User.name).all()
    return [
        {
            "id": u.id,
            "name": u.name or u.email.split("@")[0],
            "email": u.email,
            "role": u.role,
        }
        for u in users
    ]


# ── Customer Import ──────────────────────────────────────────────────────


@router.post("/api/customers/import")
@limiter.limit("5/minute")
async def import_customers(
    request: Request,
    data: list[CustomerImportRow],
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    # Admin-only; User table bounded by org size (~20 users)
    users_map = {u.email.lower(): u for u in db.query(User).all()}
    created_companies = 0
    created_sites = 0
    errors = []
    for i, row in enumerate(data):
        try:
            company_name = row.company_name
            site_name = row.site_name
            company = (
                db.query(Company)
                .filter(
                    sqlfunc.lower(Company.name) == company_name.lower(),
                )
                .first()
            )
            if not company:
                company = Company(name=company_name)
                db.add(company)
                db.flush()
                created_companies += 1
            site = (
                db.query(CustomerSite)
                .filter(
                    CustomerSite.company_id == company.id,
                    sqlfunc.lower(CustomerSite.site_name) == site_name.lower(),
                )
                .first()
            )
            if not site:
                site = CustomerSite(company_id=company.id, site_name=site_name)
                db.add(site)
                created_sites += 1
            owner_email = (row.owner_email or "").strip().lower()
            if owner_email and owner_email in users_map:
                site.owner_id = users_map[owner_email].id
            for field in [
                "contact_name",
                "contact_email",
                "contact_phone",
                "contact_title",
                "payment_terms",
                "shipping_terms",
                "city",
                "state",
                "zip",
                "country",
                "notes",
            ]:
                val = getattr(row, field, None)
                if val:
                    setattr(site, field, val.strip() if isinstance(val, str) else val)
            if row.address:
                site.address_line1 = row.address.strip()
        except Exception as e:
            errors.append(f"Row {i + 1}: {str(e)}")
    db.commit()
    return {
        "created_companies": created_companies,
        "created_sites": created_sites,
        "errors": errors[:20],
    }
