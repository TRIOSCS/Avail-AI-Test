"""
routers/crm.py — CRM Routes (Companies, Sites, Offers, Quotes, Dashboard)

Customer relationship management endpoints: company/site hierarchy, vendor
offers against requirements, customer-facing quotes with margin calc, pricing
history, enrichment, Acctivate ERP sync, and customer import.

Business Rules:
- Companies → CustomerSites parent/child hierarchy
- Offers are vendor quotes against requirements
- Quotes are customer-facing, built from selected offers
- After quote "won" → handoff to Acctivate/QuickBooks
- Quote numbers: Q-YYYY-NNNN (auto-incrementing per year)

Called by: main.py (router mount)
Depends on: models, dependencies, vendor_utils, config, enrichment_service,
            acctivate_sync
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from loguru import logger
from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..dependencies import is_admin as _is_admin, require_buyer, require_user
from ..models import (
    BuyPlan,
    Company,
    CustomerSite,
    InventorySnapshot,
    Offer,
    OfferAttachment,
    Quote,
    Requirement,
    Requisition,
    SiteContact,
    SyncLog,
    User,
    VendorCard,
    VendorContact,
)
from ..vendor_utils import normalize_vendor_name
from ..schemas.crm import (
    AddContactsToVendor,
    AddContactToSite,
    CompanyCreate,
    CompanyUpdate,
    EnrichDomainRequest,
    OfferCreate,
    OfferUpdate,
    QuoteCreate,
    QuoteReopen,
    QuoteResult,
    QuoteUpdate,
    SiteContactCreate,
    SiteContactUpdate,
    SiteCreate,
    SiteUpdate,
)

router = APIRouter()


# ── Helpers ──────────────────────────────────────────────────────────────


def next_quote_number(db: Session) -> str:
    """Generate next sequential quote number: Q-YYYY-NNNN."""
    year = datetime.now(timezone.utc).year
    prefix = f"Q-{year}-"
    last = (
        db.query(Quote)
        .filter(Quote.quote_number.like(f"{prefix}%"))
        .order_by(Quote.id.desc())
        .first()
    )
    if last:
        try:
            seq = int(last.quote_number.split("-")[-1]) + 1
        except ValueError:
            seq = 1
    else:
        seq = 1
    return f"{prefix}{seq:04d}"


def get_last_quoted_price(mpn: str, db: Session) -> dict | None:
    """Find most recent sell price for an MPN across all quotes."""
    quotes = (
        db.query(Quote)
        .filter(
            Quote.status.in_(["sent", "won", "lost"]),
        )
        .order_by(Quote.sent_at.desc().nullslast(), Quote.created_at.desc())
        .limit(100)
        .all()
    )
    mpn_upper = mpn.upper().strip()
    for q in quotes:
        for item in q.line_items or []:
            if (item.get("mpn") or "").upper().strip() == mpn_upper:
                return {
                    "sell_price": item.get("sell_price"),
                    "margin_pct": item.get("margin_pct"),
                    "quote_number": q.quote_number,
                    "date": (q.sent_at or q.created_at).isoformat()
                    if (q.sent_at or q.created_at)
                    else None,
                    "result": q.result,
                }
    return None


def quote_to_dict(q: Quote) -> dict:
    """Serialize a Quote to API response dict."""
    return {
        "id": q.id,
        "requisition_id": q.requisition_id,
        "customer_site_id": q.customer_site_id,
        "customer_name": (
            f"{q.customer_site.company.name} — {q.customer_site.site_name}"
            if q.customer_site and q.customer_site.company
            else ""
        ),
        "contact_name": q.customer_site.contact_name if q.customer_site else None,
        "contact_email": q.customer_site.contact_email if q.customer_site else None,
        "quote_number": q.quote_number,
        "revision": q.revision,
        "line_items": q.line_items or [],
        "subtotal": float(q.subtotal) if q.subtotal else None,
        "total_cost": float(q.total_cost) if q.total_cost else None,
        "total_margin_pct": float(q.total_margin_pct) if q.total_margin_pct else None,
        "payment_terms": q.payment_terms,
        "shipping_terms": q.shipping_terms,
        "validity_days": q.validity_days,
        "notes": q.notes,
        "status": q.status,
        "sent_at": q.sent_at.isoformat() if q.sent_at else None,
        "result": q.result,
        "result_reason": q.result_reason,
        "result_notes": q.result_notes,
        "result_at": q.result_at.isoformat() if q.result_at else None,
        "won_revenue": float(q.won_revenue) if q.won_revenue else None,
        "created_by": q.created_by.name if q.created_by else None,
        "created_at": q.created_at.isoformat() if q.created_at else None,
        "updated_at": q.updated_at.isoformat() if q.updated_at else None,
    }


# ── Companies ────────────────────────────────────────────────────────────


@router.get("/api/companies")
async def list_companies(
    search: str = "",
    owner_id: int = 0,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    query = db.query(Company).filter(Company.is_active == True)  # noqa: E712
    if search.strip():
        safe = search.strip().replace("%", r"\%").replace("_", r"\_")
        query = query.filter(Company.name.ilike(f"%{safe}%"))
    companies = query.order_by(Company.name).limit(500).all()

    # Pre-fetch open requisition counts per site (avoids N+1 query per site)
    company_ids = [c.id for c in companies]
    site_open_counts: dict[int, int] = {}
    if company_ids:
        count_rows = (
            db.query(
                Requisition.customer_site_id,
                sqlfunc.count(Requisition.id),
            )
            .filter(
                Requisition.status.notin_(["archived", "won", "lost"]),
            )
            .join(CustomerSite)
            .filter(CustomerSite.company_id.in_(company_ids))
            .group_by(Requisition.customer_site_id)
            .all()
        )
        site_open_counts = {row[0]: row[1] for row in count_rows}

    result = []
    for c in companies:
        sites = []
        for s in c.sites:
            if not s.is_active:
                continue
            if owner_id and s.owner_id != owner_id:
                continue
            open_count = site_open_counts.get(s.id, 0)
            sites.append(
                {
                    "id": s.id,
                    "site_name": s.site_name,
                    "owner_id": s.owner_id,
                    "owner_name": s.owner.name if s.owner else None,
                    "contact_name": s.contact_name,
                    "contact_email": s.contact_email,
                    "contact_phone": s.contact_phone,
                    "contact_title": s.contact_title,
                    "payment_terms": s.payment_terms,
                    "shipping_terms": s.shipping_terms,
                    "city": s.city,
                    "state": s.state,
                    "open_reqs": open_count,
                    "notes": s.notes,
                }
            )
        if owner_id and not sites:
            continue
        result.append(
            {
                "id": c.id,
                "name": c.name,
                "website": c.website,
                "industry": c.industry,
                "notes": c.notes,
                "domain": c.domain,
                "linkedin_url": c.linkedin_url,
                "legal_name": c.legal_name,
                "employee_size": c.employee_size,
                "hq_city": c.hq_city,
                "hq_state": c.hq_state,
                "hq_country": c.hq_country,
                "last_enriched_at": c.last_enriched_at.isoformat()
                if c.last_enriched_at
                else None,
                "enrichment_source": c.enrichment_source,
                "site_count": len(sites),
                "sites": sites,
            }
        )
    return result


@router.post("/api/companies")
async def create_company(
    payload: CompanyCreate,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    from ..enrichment_service import normalize_company_input

    clean_name, clean_domain = await normalize_company_input(
        payload.name, payload.domain or ""
    )
    # Extract domain from website if no explicit domain
    if not clean_domain and payload.website:
        clean_domain = (
            payload.website.replace("https://", "")
            .replace("http://", "")
            .replace("www.", "")
            .split("/")[0]
            .lower()
        )
    company = Company(
        name=clean_name,
        website=payload.website,
        industry=payload.industry,
        notes=payload.notes,
        domain=clean_domain or payload.domain,
        linkedin_url=payload.linkedin_url,
    )
    db.add(company)
    db.commit()

    # Auto-enrich if domain is available
    enrich_triggered = False
    domain = company.domain or ""
    if domain and (
        settings.clay_api_key
        or settings.explorium_api_key
        or settings.anthropic_api_key
    ):
        import asyncio
        from ..enrichment_service import enrich_entity, apply_enrichment_to_company

        async def _enrich_company_bg(cid, d, n):
            from ..database import SessionLocal

            try:
                enrichment = await enrich_entity(d, n)
                if not enrichment:
                    return
                s = SessionLocal()
                try:
                    c = s.get(Company, cid)
                    if c:
                        apply_enrichment_to_company(c, enrichment)
                        s.commit()
                finally:
                    s.close()
            except Exception:
                logger.exception("Background enrichment failed for company %d", cid)

        asyncio.create_task(_enrich_company_bg(company.id, domain, company.name))
        enrich_triggered = True

    return {
        "id": company.id,
        "name": company.name,
        "enrich_triggered": enrich_triggered,
    }


@router.put("/api/companies/{company_id}")
async def update_company(
    company_id: int,
    payload: CompanyUpdate,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(company, field, value)
    db.commit()
    return {"ok": True}


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
        raise HTTPException(404)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(site, field, value)
    db.commit()
    return {"ok": True}


@router.get("/api/sites/{site_id}")
async def get_site(
    site_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    site = db.get(CustomerSite, site_id)
    if not site:
        raise HTTPException(404)
    reqs = (
        db.query(Requisition)
        .filter(
            Requisition.customer_site_id == site_id,
        )
        .order_by(Requisition.created_at.desc())
        .limit(20)
        .all()
    )
    contacts = (
        db.query(SiteContact)
        .filter(
            SiteContact.customer_site_id == site_id,
        )
        .order_by(SiteContact.is_primary.desc(), SiteContact.full_name)
        .all()
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
        raise HTTPException(404)
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
        raise HTTPException(404)
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
        raise HTTPException(404)
    db.delete(contact)
    db.commit()
    return {"ok": True}


# ── Enrichment (shared for vendors + customers) ─────────────────────────


@router.post("/api/enrich/company/{company_id}")
async def enrich_company(
    company_id: int,
    payload: EnrichDomainRequest = EnrichDomainRequest(),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Enrich a customer company with external data."""
    if (
        not settings.clay_api_key
        and not settings.explorium_api_key
        and not settings.anthropic_api_key
    ):
        raise HTTPException(
            503,
            "No enrichment providers configured — set CLAY_API_KEY, EXPLORIUM_API_KEY, or ANTHROPIC_API_KEY in .env",
        )
    from ..enrichment_service import apply_enrichment_to_company, enrich_entity

    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")
    domain = company.domain or company.website or ""
    if domain:
        domain = (
            domain.replace("https://", "")
            .replace("http://", "")
            .replace("www.", "")
            .split("/")[0]
        )
    if payload.domain:
        domain = payload.domain
    if not domain:
        raise HTTPException(
            400, "No domain available — set company website or domain first"
        )
    enrichment = await enrich_entity(domain, company.name)
    updated = apply_enrichment_to_company(company, enrichment)
    db.commit()
    return {"ok": True, "updated_fields": updated, "enrichment": enrichment}


@router.post("/api/enrich/vendor/{card_id}")
async def enrich_vendor_card(
    card_id: int,
    payload: EnrichDomainRequest = EnrichDomainRequest(),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Enrich a vendor card with external data."""
    if (
        not settings.clay_api_key
        and not settings.explorium_api_key
        and not settings.anthropic_api_key
    ):
        raise HTTPException(
            503,
            "No enrichment providers configured — set CLAY_API_KEY, EXPLORIUM_API_KEY, or ANTHROPIC_API_KEY in .env",
        )
    from ..enrichment_service import apply_enrichment_to_vendor, enrich_entity

    card = db.get(VendorCard, card_id)
    if not card:
        raise HTTPException(404, "Vendor card not found")
    domain = card.domain or card.website or ""
    if domain:
        domain = (
            domain.replace("https://", "")
            .replace("http://", "")
            .replace("www.", "")
            .split("/")[0]
        )
    if payload.domain:
        domain = payload.domain
    if not domain:
        raise HTTPException(
            400, "No domain available — set vendor website or domain first"
        )
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
    if (
        not settings.clay_api_key
        and not settings.explorium_api_key
        and not settings.anthropic_api_key
    ):
        raise HTTPException(
            503,
            "No enrichment providers configured — set CLAY_API_KEY, EXPLORIUM_API_KEY, or ANTHROPIC_API_KEY in .env",
        )
    from ..enrichment_service import find_suggested_contacts

    if not domain:
        raise HTTPException(400, "domain parameter is required")
    domain = (
        domain.replace("https://", "")
        .replace("http://", "")
        .replace("www.", "")
        .split("/")[0]
    )
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
        existing = (
            db.query(VendorContact)
            .filter_by(vendor_card_id=payload.vendor_card_id, email=c.email)
            .first()
        )
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


# ── Acctivate Sync ───────────────────────────────────────────────────────


@router.post("/api/admin/acctivate/discover")
async def acctivate_discover_schema(
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Run schema discovery against Acctivate SQL Server."""
    if not _is_admin(user):
        raise HTTPException(403, "Admin only")
    if not settings.acctivate_host:
        raise HTTPException(400, "ACCTIVATE_HOST not configured")
    from ..acctivate_sync import discover_schema

    try:
        result = discover_schema()
        return {"ok": True, "schema": result}
    except Exception as e:
        raise HTTPException(500, f"Discovery failed: {e}")


@router.post("/api/admin/acctivate/sync")
async def acctivate_run_sync(
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Manually trigger an Acctivate sync run."""
    if not _is_admin(user):
        raise HTTPException(403, "Admin only")
    if not settings.acctivate_host:
        raise HTTPException(400, "ACCTIVATE_HOST not configured")
    from ..acctivate_sync import run_sync

    result = run_sync(db)
    return result


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


@router.get("/api/inventory")
async def get_inventory(
    mpn: str = "",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Check current inventory from Acctivate snapshot."""
    q = db.query(InventorySnapshot).filter(InventorySnapshot.qty_on_hand > 0)
    if mpn:
        q = q.filter(InventorySnapshot.product_id.ilike(f"%{mpn}%"))
    items = q.order_by(InventorySnapshot.product_id).limit(500).all()
    return [
        {
            "product_id": i.product_id,
            "warehouse_id": i.warehouse_id,
            "qty_on_hand": i.qty_on_hand,
            "synced_at": i.synced_at.isoformat() if i.synced_at else None,
        }
        for i in items
    ]


# ── Users (simple list for dropdowns) ────────────────────────────────────


@router.get("/api/users/list")
async def list_users_simple(
    user: User = Depends(require_user), db: Session = Depends(get_db)
):
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
async def import_customers(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    data = await request.json()
    if not isinstance(data, list):
        raise HTTPException(400, "Expected JSON array")
    users_map = {u.email.lower(): u for u in db.query(User).all()}
    created_companies = 0
    created_sites = 0
    errors = []
    for i, row in enumerate(data):
        try:
            company_name = (row.get("company_name") or "").strip()
            site_name = (row.get("site_name") or "HQ").strip()
            if not company_name:
                errors.append(f"Row {i + 1}: missing company_name")
                continue
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
            owner_email = (row.get("owner_email") or "").strip().lower()
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
                val = row.get(field)
                if val:
                    setattr(site, field, val.strip() if isinstance(val, str) else val)
            addr = row.get("address")
            if addr:
                site.address_line1 = addr.strip()
        except Exception as e:
            errors.append(f"Row {i + 1}: {str(e)}")
    db.commit()
    return {
        "created_companies": created_companies,
        "created_sites": created_sites,
        "errors": errors[:20],
    }


# ── Offers ───────────────────────────────────────────────────────────────


@router.get("/api/requisitions/{req_id}/offers")
async def list_offers(
    req_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    from ..dependencies import get_req_for_user

    req = get_req_for_user(db, user, req_id)
    if not req:
        raise HTTPException(404)
    offers = (
        db.query(Offer)
        .filter(Offer.requisition_id == req_id)
        .order_by(
            Offer.requirement_id,
            Offer.unit_price,
        )
        .all()
    )
    # Detect unseen offers before marking as viewed
    latest_offer_at = max((o.created_at for o in offers), default=None)
    has_new = bool(
        latest_offer_at
        and (not req.offers_viewed_at or latest_offer_at > req.offers_viewed_at)
    )
    # Mark as viewed if the requisition owner is viewing
    if offers and user.id == req.created_by:
        req.offers_viewed_at = datetime.now(timezone.utc)
        db.commit()
    groups: dict[int, list] = {}
    for o in offers:
        key = o.requirement_id or 0
        if key not in groups:
            groups[key] = []
        atts = [
            {
                "id": a.id,
                "file_name": a.file_name,
                "onedrive_url": a.onedrive_url,
                "thumbnail_url": a.thumbnail_url,
                "content_type": a.content_type,
            }
            for a in (o.attachments or [])
        ]
        groups[key].append(
            {
                "id": o.id,
                "requirement_id": o.requirement_id,
                "vendor_name": o.vendor_name,
                "vendor_card_id": o.vendor_card_id,
                "mpn": o.mpn,
                "manufacturer": o.manufacturer,
                "qty_available": o.qty_available,
                "unit_price": float(o.unit_price) if o.unit_price else None,
                "lead_time": o.lead_time,
                "date_code": o.date_code,
                "condition": o.condition,
                "packaging": o.packaging,
                "firmware": o.firmware,
                "hardware_code": o.hardware_code,
                "moq": o.moq,
                "source": o.source,
                "status": o.status,
                "notes": o.notes,
                "entered_by": o.entered_by.name if o.entered_by else None,
                "created_at": o.created_at.isoformat() if o.created_at else None,
                "attachments": atts,
            }
        )
    result = []
    for r in req.requirements:
        target = float(r.target_price) if r.target_price else None
        last_q = get_last_quoted_price(r.primary_mpn, db)
        result.append(
            {
                "requirement_id": r.id,
                "mpn": r.primary_mpn,
                "target_qty": r.target_qty,
                "target_price": target,
                "last_quoted": last_q,
                "offers": groups.get(r.id, []),
            }
        )
    return {
        "has_new_offers": has_new,
        "latest_offer_at": latest_offer_at.isoformat() if latest_offer_at else None,
        "groups": result,
    }


@router.post("/api/requisitions/{req_id}/offers")
async def create_offer(
    req_id: int,
    payload: OfferCreate,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    from ..dependencies import get_req_for_user

    req = get_req_for_user(db, user, req_id)
    if not req:
        raise HTTPException(404)
    norm_name = normalize_vendor_name(payload.vendor_name)
    card = db.query(VendorCard).filter(VendorCard.normalized_name == norm_name).first()
    if not card:
        domain = ""
        if payload.vendor_website:
            domain = (
                payload.vendor_website.replace("https://", "")
                .replace("http://", "")
                .replace("www.", "")
                .split("/")[0]
                .lower()
            )
        card = VendorCard(
            normalized_name=norm_name,
            display_name=payload.vendor_name,
            domain=domain or None,
            emails=[],
            phones=[],
        )
        db.add(card)
        db.flush()
        if domain and (
            settings.clay_api_key
            or settings.explorium_api_key
            or settings.anthropic_api_key
        ):
            import asyncio
            from ..routers.vendors import _background_enrich_vendor

            asyncio.create_task(
                _background_enrich_vendor(card.id, domain, card.display_name)
            )
    offer = Offer(
        requisition_id=req_id,
        requirement_id=payload.requirement_id,
        vendor_card_id=card.id,
        vendor_name=card.display_name,
        mpn=payload.mpn,
        manufacturer=payload.manufacturer,
        qty_available=payload.qty_available,
        unit_price=payload.unit_price,
        lead_time=payload.lead_time,
        date_code=payload.date_code,
        condition=payload.condition,
        packaging=payload.packaging,
        firmware=payload.firmware,
        hardware_code=payload.hardware_code,
        moq=payload.moq,
        source=payload.source,
        vendor_response_id=payload.vendor_response_id,
        entered_by_id=user.id,
        notes=payload.notes,
        status=payload.status,
    )
    db.add(offer)
    old_status = req.status
    if req.status in ("active", "sourcing"):
        req.status = "offers"
    db.commit()
    return {
        "id": offer.id,
        "vendor_name": offer.vendor_name,
        "mpn": offer.mpn,
        "req_status": req.status,
        "status_changed": req.status != old_status,
    }


@router.put("/api/offers/{offer_id}")
async def update_offer(
    offer_id: int,
    payload: OfferUpdate,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    offer = db.get(Offer, offer_id)
    if not offer:
        raise HTTPException(404)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(offer, field, value)
    db.commit()
    return {"ok": True}


@router.delete("/api/offers/{offer_id}")
async def delete_offer(
    offer_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    offer = db.get(Offer, offer_id)
    if not offer:
        raise HTTPException(404)
    db.delete(offer)
    db.commit()
    return {"ok": True}


# ── Offer Attachments (OneDrive) ─────────────────────────────────────────


@router.post("/api/offers/{offer_id}/attachments")
async def upload_offer_attachment(
    offer_id: int,
    file: UploadFile = File(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Upload a file to OneDrive and attach it to an offer."""
    offer = db.get(Offer, offer_id)
    if not offer:
        raise HTTPException(404)
    content = await file.read()
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(400, "File too large (max 10 MB)")
    # Upload to OneDrive: AvailAI/Offers/{req_id}/{filename}
    from ..utils.graph_client import GraphClient

    if not user.access_token:
        raise HTTPException(401, "Microsoft account not connected — please re-login")
    GraphClient(user.access_token)
    safe_name = file.filename.replace("/", "_").replace("\\", "_")
    drive_path = (
        f"/me/drive/root:/AvailAI/Offers/{offer.requisition_id}/{safe_name}:/content"
    )
    import httpx

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.put(
            f"https://graph.microsoft.com/v1.0{drive_path}",
            content=content,
            headers={
                "Authorization": f"Bearer {user.access_token}",
                "Content-Type": file.content_type or "application/octet-stream",
            },
        )
    if resp.status_code not in (200, 201):
        logger.error(f"OneDrive upload failed: {resp.status_code} {resp.text[:300]}")
        raise HTTPException(502, "Failed to upload to OneDrive")
    result = resp.json()
    att = OfferAttachment(
        offer_id=offer_id,
        file_name=safe_name,
        onedrive_item_id=result.get("id"),
        onedrive_url=result.get("webUrl"),
        content_type=file.content_type,
        size_bytes=len(content),
        uploaded_by_id=user.id,
    )
    db.add(att)
    db.commit()
    return {
        "id": att.id,
        "file_name": att.file_name,
        "onedrive_url": att.onedrive_url,
        "content_type": att.content_type,
    }


@router.post("/api/offers/{offer_id}/attachments/onedrive")
async def attach_from_onedrive(
    offer_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Attach an existing OneDrive file to an offer by item ID."""
    offer = db.get(Offer, offer_id)
    if not offer:
        raise HTTPException(404)
    body = await request.json()
    item_id = body.get("item_id")
    if not item_id:
        raise HTTPException(400, "item_id required")
    from ..utils.graph_client import GraphClient

    if not user.access_token:
        raise HTTPException(401, "Microsoft account not connected")
    gc = GraphClient(user.access_token)
    item = await gc.get_json(f"/me/drive/items/{item_id}")
    if "error" in item:
        raise HTTPException(404, "OneDrive item not found")
    att = OfferAttachment(
        offer_id=offer_id,
        file_name=item.get("name", "file"),
        onedrive_item_id=item_id,
        onedrive_url=item.get("webUrl"),
        content_type=item.get("file", {}).get("mimeType"),
        size_bytes=item.get("size"),
        uploaded_by_id=user.id,
    )
    db.add(att)
    db.commit()
    return {
        "id": att.id,
        "file_name": att.file_name,
        "onedrive_url": att.onedrive_url,
        "content_type": att.content_type,
    }


@router.delete("/api/offer-attachments/{att_id}")
async def delete_offer_attachment(
    att_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    att = db.get(OfferAttachment, att_id)
    if not att:
        raise HTTPException(404)
    # Delete from OneDrive if we have the item ID
    if att.onedrive_item_id and user.access_token:
        from ..utils.graph_client import GraphClient

        GraphClient(user.access_token)
        try:
            import httpx

            async with httpx.AsyncClient(timeout=15) as client:
                await client.delete(
                    f"https://graph.microsoft.com/v1.0/me/drive/items/{att.onedrive_item_id}",
                    headers={"Authorization": f"Bearer {user.access_token}"},
                )
        except Exception:
            logger.warning(f"Failed to delete OneDrive item {att.onedrive_item_id}")
    db.delete(att)
    db.commit()
    return {"ok": True}


@router.get("/api/onedrive/browse")
async def browse_onedrive(
    path: str = "",
    user: User = Depends(require_user),
):
    """Browse user's OneDrive files for the picker."""
    if not user.access_token:
        raise HTTPException(401, "Microsoft account not connected")
    from ..utils.graph_client import GraphClient

    gc = GraphClient(user.access_token)
    if path:
        data = await gc.get_json(
            f"/me/drive/root:/{path}:/children",
            params={
                "$top": "50",
                "$select": "id,name,size,file,folder,webUrl,lastModifiedDateTime",
            },
        )
    else:
        data = await gc.get_json(
            "/me/drive/root/children",
            params={
                "$top": "50",
                "$select": "id,name,size,file,folder,webUrl,lastModifiedDateTime",
            },
        )
    if "error" in data:
        raise HTTPException(502, "Failed to browse OneDrive")
    items = data.get("value", [])
    return [
        {
            "id": i["id"],
            "name": i["name"],
            "is_folder": "folder" in i,
            "size": i.get("size"),
            "mime_type": i.get("file", {}).get("mimeType"),
            "web_url": i.get("webUrl"),
            "modified_at": i.get("lastModifiedDateTime"),
        }
        for i in items
    ]


# ── Quotes ───────────────────────────────────────────────────────────────


@router.get("/api/requisitions/{req_id}/quote")
async def get_quote(
    req_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    from ..dependencies import get_req_for_user

    req = get_req_for_user(db, user, req_id)
    if not req:
        raise HTTPException(404)
    quote = (
        db.query(Quote)
        .filter(
            Quote.requisition_id == req_id,
        )
        .order_by(Quote.revision.desc())
        .first()
    )
    if not quote:
        return None
    return quote_to_dict(quote)


@router.post("/api/requisitions/{req_id}/quote")
async def create_quote(
    req_id: int,
    payload: QuoteCreate,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    from ..dependencies import get_req_for_user

    req = get_req_for_user(db, user, req_id)
    if not req:
        raise HTTPException(404)
    if not req.customer_site_id:
        raise HTTPException(
            400, "Requisition must be linked to a customer site before quoting"
        )
    offer_ids = payload.offer_ids
    line_items = payload.line_items
    if offer_ids and not line_items:
        offers = db.query(Offer).filter(Offer.id.in_(offer_ids)).all()
        line_items = []
        for o in offers:
            target = None
            last_q_price = None
            if o.requirement:
                target = (
                    float(o.requirement.target_price)
                    if o.requirement.target_price
                    else None
                )
                lq = get_last_quoted_price(o.mpn, db)
                last_q_price = lq.get("sell_price") if lq else None
            cost = float(o.unit_price) if o.unit_price else 0
            line_items.append(
                {
                    "mpn": o.mpn,
                    "manufacturer": o.manufacturer,
                    "qty": o.qty_available or 0,
                    "cost_price": cost,
                    "sell_price": cost,
                    "margin_pct": 0,
                    "lead_time": o.lead_time,
                    "condition": o.condition,
                    "offer_id": o.id,
                    "target_price": target,
                    "last_quoted_price": last_q_price,
                }
            )
    site = db.get(CustomerSite, req.customer_site_id)
    quote = Quote(
        requisition_id=req_id,
        customer_site_id=req.customer_site_id,
        quote_number=next_quote_number(db),
        line_items=line_items,
        payment_terms=site.payment_terms if site else None,
        shipping_terms=site.shipping_terms if site else None,
        created_by_id=user.id,
    )
    db.add(quote)
    old_status = req.status
    if req.status in ("active", "sourcing", "offers"):
        req.status = "quoting"
    db.commit()
    result = quote_to_dict(quote)
    result["req_status"] = req.status
    result["status_changed"] = req.status != old_status
    return result


@router.put("/api/quotes/{quote_id}")
async def update_quote(
    quote_id: int,
    payload: QuoteUpdate,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    quote = db.get(Quote, quote_id)
    if not quote:
        raise HTTPException(404)
    updates = payload.model_dump(exclude_unset=True)
    if "line_items" in updates:
        quote.line_items = updates.pop("line_items")
        total_sell = sum(
            (item.get("qty") or 0) * (item.get("sell_price") or 0)
            for item in (quote.line_items or [])
        )
        total_cost = sum(
            (item.get("qty") or 0) * (item.get("cost_price") or 0)
            for item in (quote.line_items or [])
        )
        quote.subtotal = total_sell
        quote.total_cost = total_cost
        quote.total_margin_pct = (
            round((total_sell - total_cost) / total_sell * 100, 2)
            if total_sell > 0
            else 0
        )
    for field, value in updates.items():
        setattr(quote, field, value)
    db.commit()
    return quote_to_dict(quote)


@router.post("/api/quotes/{quote_id}/send")
async def send_quote(
    quote_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    quote = db.get(Quote, quote_id)
    if not quote:
        raise HTTPException(404)
    quote.status = "sent"
    quote.sent_at = datetime.now(timezone.utc)
    req = db.get(Requisition, quote.requisition_id)
    old_status = req.status if req else None
    if req and req.status not in ("won", "lost", "archived"):
        req.status = "quoted"
    db.commit()
    return {
        "ok": True,
        "status": "sent",
        "req_status": req.status if req else None,
        "status_changed": req and req.status != old_status,
    }


@router.post("/api/quotes/{quote_id}/result")
async def quote_result(
    quote_id: int,
    payload: QuoteResult,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    quote = db.get(Quote, quote_id)
    if not quote:
        raise HTTPException(404)
    quote.result = payload.result
    quote.result_reason = payload.reason
    quote.result_notes = payload.notes
    quote.result_at = datetime.now(timezone.utc)
    quote.status = payload.result
    if payload.result == "won":
        quote.won_revenue = quote.subtotal
    req = db.get(Requisition, quote.requisition_id)
    if req:
        req.status = payload.result
    db.commit()
    return {
        "ok": True,
        "status": payload.result,
        "req_status": req.status if req else None,
        "status_changed": True,
    }


@router.post("/api/quotes/{quote_id}/revise")
async def revise_quote(
    quote_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    old = db.get(Quote, quote_id)
    if not old:
        raise HTTPException(404)
    old.status = "revised"
    old_number = old.quote_number
    old.quote_number = f"{old_number}-R{old.revision}"
    new_quote = Quote(
        requisition_id=old.requisition_id,
        customer_site_id=old.customer_site_id,
        quote_number=old_number,
        revision=old.revision + 1,
        line_items=old.line_items,
        payment_terms=old.payment_terms,
        shipping_terms=old.shipping_terms,
        validity_days=old.validity_days,
        notes=old.notes,
        created_by_id=user.id,
    )
    db.add(new_quote)
    db.commit()
    return quote_to_dict(new_quote)


@router.post("/api/quotes/{quote_id}/reopen")
async def reopen_quote(
    quote_id: int,
    payload: QuoteReopen,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    quote = db.get(Quote, quote_id)
    if not quote:
        raise HTTPException(404)
    req = db.get(Requisition, quote.requisition_id)
    if req:
        req.status = "reopened"
    if payload.revise:
        quote.status = "revised"
        old_number = quote.quote_number
        quote.quote_number = f"{old_number}-R{quote.revision}"
        new_quote = Quote(
            requisition_id=quote.requisition_id,
            customer_site_id=quote.customer_site_id,
            quote_number=old_number,
            revision=quote.revision + 1,
            line_items=quote.line_items,
            payment_terms=quote.payment_terms,
            shipping_terms=quote.shipping_terms,
            validity_days=quote.validity_days,
            notes=quote.notes,
            created_by_id=user.id,
        )
        db.add(new_quote)
        db.commit()
        return quote_to_dict(new_quote)
    else:
        quote.status = "sent"
        quote.result = None
        quote.result_reason = None
        quote.result_notes = None
        quote.result_at = None
        db.commit()
        return quote_to_dict(quote)


# ── Buy Plans ────────────────────────────────────────────────────────────


def _buyplan_to_dict(bp: BuyPlan) -> dict:
    return {
        "id": bp.id,
        "requisition_id": bp.requisition_id,
        "requisition_name": bp.requisition.name if bp.requisition else None,
        "quote_id": bp.quote_id,
        "status": bp.status,
        "line_items": bp.line_items or [],
        "manager_notes": bp.manager_notes,
        "rejection_reason": bp.rejection_reason,
        "submitted_by": bp.submitted_by.name if bp.submitted_by else None,
        "submitted_by_id": bp.submitted_by_id,
        "approved_by": bp.approved_by.name if bp.approved_by else None,
        "approved_by_id": bp.approved_by_id,
        "submitted_at": bp.submitted_at.isoformat() if bp.submitted_at else None,
        "approved_at": bp.approved_at.isoformat() if bp.approved_at else None,
        "rejected_at": bp.rejected_at.isoformat() if bp.rejected_at else None,
    }


@router.post("/api/quotes/{quote_id}/buy-plan")
async def submit_buy_plan(
    quote_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Submit a buy plan when marking a quote as Won."""
    quote = db.get(Quote, quote_id)
    if not quote:
        raise HTTPException(404)
    body = await request.json()
    offer_ids = body.get("offer_ids", [])
    if not offer_ids:
        raise HTTPException(400, "At least one offer must be selected")

    # Build buy plan line items from selected offers
    offers = db.query(Offer).filter(Offer.id.in_(offer_ids)).all()
    line_items = []
    for o in offers:
        line_items.append(
            {
                "offer_id": o.id,
                "mpn": o.mpn,
                "vendor_name": o.vendor_name,
                "manufacturer": o.manufacturer,
                "qty": o.qty_available or 0,
                "cost_price": float(o.unit_price) if o.unit_price else 0,
                "lead_time": o.lead_time,
                "condition": o.condition,
                "entered_by_id": o.entered_by_id,
                "po_number": None,
                "po_sent_at": None,
                "po_recipient": None,
                "po_verified": False,
            }
        )

    import secrets

    plan = BuyPlan(
        requisition_id=quote.requisition_id,
        quote_id=quote_id,
        status="pending_approval",
        line_items=line_items,
        submitted_by_id=user.id,
        approval_token=secrets.token_urlsafe(32),
    )
    db.add(plan)

    # Mark quote as won
    quote.result = "won"
    quote.result_at = datetime.now(timezone.utc)
    quote.status = "won"
    quote.won_revenue = quote.subtotal
    req = db.get(Requisition, quote.requisition_id)
    if req:
        req.status = "won"
    db.commit()

    # Send notifications asynchronously (uses its own DB session to avoid
    # request-scoped session closing before background task completes)
    import asyncio
    from ..services.buyplan_service import notify_buyplan_submitted

    async def _bg_notify_submitted(plan_id: int):
        from ..database import SessionLocal

        bg_db = SessionLocal()
        try:
            bg_plan = bg_db.get(BuyPlan, plan_id)
            if bg_plan:
                await notify_buyplan_submitted(bg_plan, bg_db)
        except Exception:
            logger.exception("Background notify_buyplan_submitted failed")
        finally:
            bg_db.close()

    asyncio.create_task(_bg_notify_submitted(plan.id))

    return {
        "ok": True,
        "buy_plan_id": plan.id,
        "status": "pending_approval",
        "req_status": req.status if req else None,
        "status_changed": True,
    }


@router.get("/api/buy-plans")
async def list_buy_plans(
    status: str = "",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """List buy plans. Admins see all, sales see own, buyers see their offers."""
    query = db.query(BuyPlan).order_by(BuyPlan.created_at.desc())
    if status:
        query = query.filter(BuyPlan.status == status)
    if not _is_admin(user):
        if user.role == "sales":
            query = query.filter(BuyPlan.submitted_by_id == user.id)
        # Buyers see all (they need to check which plans have their offers)
    plans = query.limit(200).all()
    return [_buyplan_to_dict(p) for p in plans]


@router.get("/api/buy-plans/{plan_id}")
async def get_buy_plan(
    plan_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    plan = db.get(BuyPlan, plan_id)
    if not plan:
        raise HTTPException(404)
    return _buyplan_to_dict(plan)


@router.put("/api/buy-plans/{plan_id}/approve")
async def approve_buy_plan(
    plan_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Manager approves the buy plan (admin only)."""
    if not _is_admin(user):
        raise HTTPException(403, "Admin approval required")
    plan = db.get(BuyPlan, plan_id)
    if not plan:
        raise HTTPException(404)
    if plan.status != "pending_approval":
        raise HTTPException(400, f"Cannot approve plan in status: {plan.status}")

    body = await request.json()
    if "line_items" in body:
        plan.line_items = body["line_items"]
    if "manager_notes" in body:
        plan.manager_notes = body["manager_notes"]

    plan.status = "approved"
    plan.approved_by_id = user.id
    plan.approved_at = datetime.now(timezone.utc)
    db.commit()

    import asyncio
    from ..services.buyplan_service import notify_buyplan_approved

    async def _bg_notify_approved(plan_id: int):
        from ..database import SessionLocal

        bg_db = SessionLocal()
        try:
            bg_plan = bg_db.get(BuyPlan, plan_id)
            if bg_plan:
                await notify_buyplan_approved(bg_plan, bg_db)
        except Exception:
            logger.exception("Background notify_buyplan_approved failed")
        finally:
            bg_db.close()

    asyncio.create_task(_bg_notify_approved(plan.id))

    return {"ok": True, "status": "approved"}


@router.put("/api/buy-plans/{plan_id}/reject")
async def reject_buy_plan(
    plan_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Manager rejects the buy plan (admin only)."""
    if not _is_admin(user):
        raise HTTPException(403, "Admin rejection required")
    plan = db.get(BuyPlan, plan_id)
    if not plan:
        raise HTTPException(404)
    if plan.status != "pending_approval":
        raise HTTPException(400, f"Cannot reject plan in status: {plan.status}")

    body = await request.json()
    plan.rejection_reason = body.get("reason", "")
    plan.status = "rejected"
    plan.approved_by_id = user.id  # reuse field for who acted
    plan.rejected_at = datetime.now(timezone.utc)
    db.commit()

    import asyncio
    from ..services.buyplan_service import notify_buyplan_rejected

    async def _bg_notify_rejected(plan_id: int):
        from ..database import SessionLocal

        bg_db = SessionLocal()
        try:
            bg_plan = bg_db.get(BuyPlan, plan_id)
            if bg_plan:
                await notify_buyplan_rejected(bg_plan, bg_db)
        except Exception:
            logger.exception("Background notify_buyplan_rejected failed")
        finally:
            bg_db.close()

    asyncio.create_task(_bg_notify_rejected(plan.id))

    return {"ok": True, "status": "rejected"}


@router.put("/api/buy-plans/{plan_id}/po")
async def enter_po_number(
    plan_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Buyer enters PO number for a line item."""
    plan = db.get(BuyPlan, plan_id)
    if not plan:
        raise HTTPException(404)
    if plan.status not in ("approved", "po_entered"):
        raise HTTPException(400, f"Cannot enter PO for plan in status: {plan.status}")

    body = await request.json()
    line_index = body.get("line_index")
    po_number = body.get("po_number", "").strip()
    if line_index is None or not po_number:
        raise HTTPException(400, "line_index and po_number required")
    if line_index < 0 or line_index >= len(plan.line_items or []):
        raise HTTPException(400, "Invalid line_index")

    plan.line_items[line_index]["po_number"] = po_number
    plan.status = "po_entered"

    from sqlalchemy.orm.attributes import flag_modified

    flag_modified(plan, "line_items")
    db.commit()

    # Trigger PO verification in background (own session for safety)
    import asyncio
    from ..services.buyplan_service import verify_po_sent

    async def _bg_verify_po(plan_id: int):
        from ..database import SessionLocal

        bg_db = SessionLocal()
        try:
            bg_plan = bg_db.get(BuyPlan, plan_id)
            if bg_plan:
                await verify_po_sent(bg_plan, bg_db)
        except Exception:
            logger.exception("Background verify_po_sent failed")
        finally:
            bg_db.close()

    asyncio.create_task(_bg_verify_po(plan.id))

    return {"ok": True, "status": "po_entered"}


@router.get("/api/buy-plans/{plan_id}/verify-po")
async def check_po_verification(
    plan_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Check PO verification status — re-scan if needed."""
    plan = db.get(BuyPlan, plan_id)
    if not plan:
        raise HTTPException(404)

    from ..services.buyplan_service import verify_po_sent

    results = await verify_po_sent(plan, db)
    return {
        "plan_id": plan.id,
        "status": plan.status,
        "verifications": results,
        "line_items": plan.line_items,
    }


@router.get("/api/buy-plans/for-quote/{quote_id}")
async def get_buyplan_for_quote(
    quote_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Get the buy plan associated with a quote (if any)."""
    plan = db.query(BuyPlan).filter(BuyPlan.quote_id == quote_id).first()
    if not plan:
        return None
    return _buyplan_to_dict(plan)


# ── Pricing History ──────────────────────────────────────────────────────


@router.get("/api/pricing-history/{mpn}")
async def pricing_history(
    mpn: str, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    mpn_upper = mpn.upper().strip()
    quotes = (
        db.query(Quote)
        .filter(
            Quote.status.in_(["sent", "won", "lost"]),
        )
        .order_by(Quote.sent_at.desc().nullslast(), Quote.created_at.desc())
        .limit(500)
        .all()
    )
    history = []
    for q in quotes:
        for item in q.line_items or []:
            if (item.get("mpn") or "").upper().strip() == mpn_upper:
                site_name = ""
                if q.customer_site:
                    site_name = (
                        f"{q.customer_site.company.name} — {q.customer_site.site_name}"
                        if q.customer_site.company
                        else q.customer_site.site_name
                    )
                history.append(
                    {
                        "date": (q.sent_at or q.created_at).isoformat()
                        if (q.sent_at or q.created_at)
                        else None,
                        "qty": item.get("qty"),
                        "sell_price": item.get("sell_price"),
                        "margin_pct": item.get("margin_pct"),
                        "customer": site_name,
                        "result": q.result,
                        "quote_number": q.quote_number,
                    }
                )
                break
    prices = [h["sell_price"] for h in history if h.get("sell_price")]
    margins = [h["margin_pct"] for h in history if h.get("margin_pct")]
    return {
        "mpn": mpn,
        "history": history[:50],
        "avg_price": round(sum(prices) / len(prices), 4) if prices else None,
        "avg_margin": round(sum(margins) / len(margins), 2) if margins else None,
        "price_range": [min(prices), max(prices)] if prices else None,
    }


# ── Clone Requisition ────────────────────────────────────────────────────


@router.post("/api/requisitions/{req_id}/clone")
async def clone_requisition(
    req_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    from ..dependencies import get_req_for_user

    req = get_req_for_user(db, user, req_id)
    if not req:
        raise HTTPException(404)
    new_req = Requisition(
        name=f"{req.name} (clone)",
        customer_name=req.customer_name,
        customer_site_id=req.customer_site_id,
        status="active",
        cloned_from_id=req.id,
        created_by=user.id,
    )
    db.add(new_req)
    db.flush()
    for r in req.requirements:
        new_r = Requirement(
            requisition_id=new_req.id,
            primary_mpn=r.primary_mpn,
            oem_pn=r.oem_pn,
            brand=r.brand,
            sku=r.sku,
            target_qty=r.target_qty,
            target_price=r.target_price,
            substitutes=r.substitutes,
            notes=r.notes,
        )
        db.add(new_r)
    db.flush()
    # Map old requirement IDs → new for offer cloning
    req_map: dict[int, int] = {}
    for old_r in req.requirements:
        new_r = (
            db.query(Requirement)
            .filter(
                Requirement.requisition_id == new_req.id,
                Requirement.primary_mpn == old_r.primary_mpn,
            )
            .first()
        )
        if new_r:
            req_map[old_r.id] = new_r.id
    for o in req.offers:
        if o.status in ("active", "selected"):
            new_o = Offer(
                requisition_id=new_req.id,
                requirement_id=req_map.get(o.requirement_id),
                vendor_card_id=o.vendor_card_id,
                vendor_name=o.vendor_name,
                mpn=o.mpn,
                manufacturer=o.manufacturer,
                qty_available=o.qty_available,
                unit_price=o.unit_price,
                lead_time=o.lead_time,
                date_code=o.date_code,
                condition=o.condition,
                packaging=o.packaging,
                moq=o.moq,
                source=o.source,
                entered_by_id=user.id,
                notes=f"Reference from REQ-{req.id:03d}",
                status="reference",
            )
            db.add(new_o)
    db.commit()
    return {"id": new_req.id, "name": new_req.name}
